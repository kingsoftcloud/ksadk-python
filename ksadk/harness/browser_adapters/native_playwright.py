"""Scoped, persistent Playwright sessions behind an explicitly injected host policy.

This adapter is process-local: references reconnect only while this owner lives.
Browser network interception supplements the host sandbox/egress policy; it is
not an OS network sandbox. Injected browsers must be host-owned and trusted.
No provider, Studio or Native RunStore state is mutated here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import re
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

from pydantic import ValidationError

from ..native.browser_contract import (
    BrowserAction,
    BrowserControl,
    BrowserImage,
    BrowserObservation,
    BrowserRecovery,
    BrowserSessionError,
    BrowserSessionRef,
    browser_observation_digest,
)
from ..native.model import ModelImageContent
from ..native.ports import ToolDefinition, ToolRequest, ToolResult
from ..native.state import RunScope
from .native_browser_tool import browser_tool_definition
from .native_dom_snapshot import DOM_CAPTURE, BrowserElementRefs

NetworkPolicy = Callable[[BrowserSessionRef, str], bool | Awaitable[bool]]
ArtifactSink = Callable[[BrowserSessionRef, bytes, str], str | Awaitable[str]]


def _safe_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https", "ws", "wss"}:
            return "about:blank" if url == "about:blank" else "[opaque-url]"
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
    except ValueError:
        return "[invalid-url]"


def _redact(text: str) -> str:
    text = re.sub(r"(?i)\b(?:https?|wss?)://[^\s<>\"']+", lambda match: _safe_url(match[0]), text)
    text = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)\b(authorization|cookie|api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text,
    )
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[redacted]", text)


@dataclass
class _Session:
    ref: BrowserSessionRef
    context: Any
    page: Any = None
    action_seq: int = 0
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    results: dict[str, tuple[str, BrowserObservation]] = field(default_factory=dict)
    console: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    error_locations: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    logs_truncated: bool = False
    closed: bool = False
    navigation_redirect: str | None = None
    last_response_status: int | None = None
    idle_task: asyncio.Task | None = None
    cdp: Any = None
    element_refs: BrowserElementRefs = field(default_factory=BrowserElementRefs)


class _ObservationCaptureFailure(Exception):
    """Page screenshot failed before any artifact callback could run."""


class _ActionDispatchFailure(Exception):
    """Input dispatch did not return a confirmed completion."""


class NativePlaywrightBrowser:
    """Each run gets its own context/page; repeated actions retain that page.

    ``network_policy(ref, url)`` must explicitly return True on every request.
    Redirect locations and WebSocket connections are checked separately.
    Default policy denies network access. The artifact callback receives masked
    PNG bytes and returns an opaque reference, never a path or download URL.
    """

    def __init__(
        self,
        *,
        scope: RunScope,
        runtime_id: str,
        workspace_revision: str,
        network_policy: NetworkPolicy | None = None,
        artifact_sink: ArtifactSink | None = None,
        browser: Any = None,
        launch_channels: tuple[str | None, ...] = (None,),
        viewport: tuple[int, int] = (1280, 720),
        idle_timeout_seconds: float = 900,
        max_text_chars: int = 12000,
        max_image_bytes: int = 8 * 1024 * 1024,
        max_log_entries: int = 50,
        max_cached_images: int = 8,
        max_actions: int = 512,
        mask_selectors: tuple[str, ...] = (),
    ) -> None:
        if not runtime_id or not workspace_revision or idle_timeout_seconds <= 0:
            raise ValueError("invalid browser configuration")
        if not launch_channels or len(set(launch_channels)) != len(launch_channels):
            raise ValueError("invalid browser launch channels")
        if any(channel not in {None, "chrome"} for channel in launch_channels):
            raise ValueError("unsupported browser launch channel")
        if (
            any(not isinstance(size, int) or not 1 <= size <= 4096 for size in viewport)
            or len(viewport) != 2
        ):
            raise ValueError("invalid viewport")
        if (
            min(max_text_chars, max_image_bytes, max_log_entries, max_cached_images, max_actions)
            <= 0
        ):
            raise ValueError("invalid observation limits")
        self.scope = RunScope.model_validate_json(scope.model_dump_json())
        self.runtime_id, self.workspace_revision = runtime_id, workspace_revision
        self._policy, self._sink = network_policy, artifact_sink
        self._max_cached_images, self._max_actions = max_cached_images, max_actions
        self._browser, self._owned_browser, self._playwright = browser, browser is None, None
        self._launch_channels = launch_channels
        self._browser_channel = "injected" if browser is not None else "unavailable"
        self._viewport, self._idle_timeout = viewport, idle_timeout_seconds
        self._max_text, self._max_image, self._max_logs = (
            max_text_chars,
            max_image_bytes,
            max_log_entries,
        )
        self._masks = (
            'input[type="password"]',
            '[autocomplete="current-password"]',
            '[autocomplete="new-password"]',
            *mask_selectors,
        )
        self._sessions: dict[str, _Session] = {}
        self._open_results: dict[tuple[str, str], BrowserSessionRef] = {}
        self._owner_lock = asyncio.Lock()
        self._closed = False

    @property
    def browser_channel(self) -> str:
        return self._browser_channel

    @property
    def browser_version(self) -> str | None:
        return self._browser.version if self._browser is not None else None

    @staticmethod
    def definition() -> ToolDefinition:
        return browser_tool_definition()

    async def _ensure_browser(self) -> Any:
        if self._closed:
            raise BrowserSessionError("adapter_closed")
        if self._browser is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise BrowserSessionError("browser_dependency_missing") from None
            try:
                self._playwright = await async_playwright().start()
                for channel in self._launch_channels:
                    try:
                        options = {"channel": channel} if channel is not None else {}
                        self._browser = await self._playwright.chromium.launch(
                            headless=True, **options
                        )
                        self._browser_channel = channel or "bundled-chromium"
                        break
                    except Exception:
                        # The host explicitly ordered acceptable local channels.
                        # A missing system Chrome must not disable the bundled
                        # browser's normal DOM/screenshot capabilities.
                        continue
                if self._browser is None:
                    raise BrowserSessionError("browser_unavailable")
            except Exception:
                if self._playwright is not None:
                    await self._playwright.stop()
                    self._playwright = None
                raise BrowserSessionError("browser_unavailable") from None
        if not self._browser.is_connected():
            raise BrowserSessionError("browser_disconnected")
        return self._browser

    async def _allowed(self, ref: BrowserSessionRef, url: str) -> bool:
        if url == "about:blank":
            return True
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https", "ws", "wss"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                return False
            if self._policy is None:
                return False
            result = self._policy(ref, url)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, 5)
            return result is True
        except Exception:
            return False

    def _log(self, session: _Session, bucket: list[str], value: str) -> None:
        if len(bucket) >= self._max_logs:
            session.logs_truncated = True
            return
        clean = _redact(value)
        session.logs_truncated |= len(clean) > 1000
        bucket.append(clean[:1000])

    def _exception_location(self, session: _Session, event: object) -> None:
        """Project only an in-origin script path and one-based source position."""
        if not isinstance(event, dict):
            return
        details = event.get("exceptionDetails")
        if not isinstance(details, dict) or session.page is None:
            return
        raw_url = details.get("url")
        line = details.get("lineNumber")
        column = details.get("columnNumber")
        if not isinstance(raw_url, str) or type(line) is not int or type(column) is not int:
            return
        if line < 0 or column < 0:
            return
        try:
            source = urlsplit(raw_url)
            current = urlsplit(session.page.url)
        except ValueError:
            return
        if (
            source.scheme not in {"http", "https"}
            or source.scheme != current.scheme
            or source.netloc != current.netloc
        ):
            return
        path = re.sub(r"[\x00-\x1f\x7f]", "?", source.path or "/")[:300]
        self._log(session, session.error_locations, f"{path}:{line + 1}:{column + 1}")

    async def _route(self, session: _Session, route: Any) -> None:
        url = route.request.url
        try:
            if route.request.frame.page is not session.page:
                self._log(session, session.failures, "popup_request_denied")
                await route.abort("blockedbyclient")
                return
        except (AttributeError, RuntimeError):
            # Workers may not expose a frame; their requests still pass policy.
            pass
        if session.closed or not await self._allowed(session.ref, url):
            self._log(session, session.failures, "network_denied: " + _safe_url(url))
            await route.abort("blockedbyclient")
            return
        try:
            # Disable automatic redirects: otherwise the fetch can reach a
            # denied origin before the host ever sees its Location header.
            response = await route.fetch(max_redirects=0, max_retries=0, timeout=15000)
            if response.status >= 400 and getattr(route.request, "resource_type", None) in {
                "script",
                "stylesheet",
            }:
                # A 404 is an HTTP response, not a requestfailed event.
                # Keep its local asset path so the repair loop can identify
                # the missing build artifact without relying on console text.
                self._log(
                    session,
                    session.failures,
                    f"asset_http_error:{response.status}: {urlsplit(url).path}",
                )
            location = response.headers.get("location")
            if 300 <= response.status < 400 and location:
                target = urljoin(url, location)
                if not await self._allowed(session.ref, target):
                    self._log(session, session.failures, "redirect_denied: " + _safe_url(target))
                    await route.abort("blockedbyclient")
                    return
                # Chromium can skip route handlers after a fulfilled 302.
                # Never hand it a redirect. A top-level navigation is retried
                # as a fresh policy-checked goto, without replaying the click.
                request = route.request
                if request.is_navigation_request() and request.frame == session.page.main_frame:
                    session.navigation_redirect = target
                else:
                    self._log(
                        session,
                        session.failures,
                        "subresource_redirect_blocked: " + _safe_url(target),
                    )
                await route.abort("aborted")
                return
            await route.fulfill(response=response)
        except Exception:
            self._log(session, session.failures, "network_request_failed: " + _safe_url(url))
            await route.abort("failed")

    async def _socket(self, session: _Session, socket: Any) -> None:
        if not session.closed and await self._allowed(session.ref, socket.url):
            socket.connect_to_server()
        else:
            self._log(session, session.failures, "websocket_denied: " + _safe_url(socket.url))
            await socket.close(code=1008, reason="network_policy")

    async def _new_session(self, ref: BrowserSessionRef) -> BrowserSessionRef:
        browser = await self._ensure_browser()
        context = await browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            service_workers="block",
            accept_downloads=False,
        )
        session = _Session(ref=ref, context=context)
        try:
            if not hasattr(context, "route_web_socket"):
                raise BrowserSessionError("websocket_policy_unavailable")
            await context.route("**/*", lambda route: self._route(session, route))
            await context.route_web_socket("**/*", lambda socket: self._socket(session, socket))
            session.page = await context.new_page()

            async def reject_popup(page: Any) -> None:
                if page is not session.page:
                    self._log(session, session.failures, "popup_denied")
                    await page.close()

            context.on("page", reject_popup)
            session.page.on(
                "console",
                lambda msg: self._log(session, session.console, f"{msg.type}: {msg.text}"),
            )
            session.page.on(
                "pageerror", lambda error: self._log(session, session.errors, str(error))
            )
            if hasattr(context, "new_cdp_session"):
                try:
                    session.cdp = await context.new_cdp_session(session.page)
                    session.cdp.on(
                        "Runtime.exceptionThrown",
                        lambda event: self._exception_location(session, event),
                    )
                    await session.cdp.send("Runtime.enable")
                except Exception:
                    # Non-Chromium or restricted injected browsers still get
                    # normal Playwright pageerror observations.
                    session.cdp = None
            session.page.on(
                "requestfailed",
                lambda request: self._log(
                    session, session.failures, "request_failed: " + _safe_url(request.url)
                ),
            )
            self._sessions[ref.session_id] = session
            session.idle_task = asyncio.create_task(self._expire_idle(session))
            return ref
        except BaseException:
            await context.close()
            raise

    async def open(self, run_id: str, *, operation_key: str | None = None) -> BrowserSessionRef:
        async with self._owner_lock:
            if self._closed:
                raise BrowserSessionError("adapter_closed")
            key = (run_id, operation_key) if operation_key is not None else None
            if key is not None and key in self._open_results:
                return self._open_results[key]
            ref = BrowserSessionRef(
                session_id=uuid.uuid4().hex,
                scope=self.scope,
                run_id=run_id,
                runtime_id=self.runtime_id,
                workspace_revision=self.workspace_revision,
            )
            result = await self._new_session(ref)
            if key is not None:
                self._open_results[key] = result
            return result

    def _validate_ref(self, ref: BrowserSessionRef) -> None:
        if (
            ref.scope != self.scope
            or ref.runtime_id != self.runtime_id
            or ref.workspace_revision != self.workspace_revision
        ):
            raise BrowserSessionError("scope_mismatch")
        if ref.backend != "playwright":
            raise BrowserSessionError("backend_mismatch")

    def _lookup(self, ref: BrowserSessionRef) -> _Session:
        self._validate_ref(ref)
        session = self._sessions.get(ref.session_id)
        if session is None:
            raise BrowserSessionError("session_lost")
        if ref != session.ref:
            raise BrowserSessionError("stale_session_ref")
        return session

    async def _live(self, session: _Session) -> bool:
        if (
            session.closed
            or self._closed
            or not self._browser.is_connected()
            or session.page.is_closed()
        ):
            return False
        if time.monotonic() - session.last_used > self._idle_timeout:
            await self._close_session(session)
            return False
        return True

    async def _close_session(self, session: _Session) -> None:
        session.closed = True
        if session.idle_task is not None and session.idle_task is not asyncio.current_task():
            session.idle_task.cancel()
        await session.element_refs.clear()
        await session.context.close()

    async def _expire_idle(self, session: _Session) -> None:
        try:
            while not session.closed:
                remaining = self._idle_timeout - (time.monotonic() - session.last_used)
                await asyncio.sleep(max(0, remaining))
                async with session.lock:
                    if time.monotonic() - session.last_used >= self._idle_timeout:
                        await self._close_session(session)
                        return
        except Exception:
            session.closed = True
            self._log(session, session.errors, "idle_cleanup_failed")

    def _incomplete(
        self, session: _Session, action: BrowserAction, code: str
    ) -> BrowserObservation:
        return BrowserObservation(
            session_ref=session.ref,
            action_id=action.action_id,
            action_seq=session.action_seq,
            status="uncertain",
            error_code=code,
            complete=False,
            retryability="requires_reconcile",
            url=_safe_url(session.page.url),
            status_code=session.last_response_status,
            viewport=self._viewport,
            console=tuple(session.console),
            page_errors=tuple(session.errors),
            page_error_locations=tuple(session.error_locations),
            network_failures=tuple(session.failures),
            logs_truncated=session.logs_truncated,
        )

    def _read_only_failure(
        self, session: _Session, action: BrowserAction, code: str
    ) -> BrowserObservation:
        """A screenshot failure cannot have dispatched a page mutation."""
        return self._incomplete(session, action, code).model_copy(
            update={"status": "error", "complete": True, "retryability": "safe"}
        )

    async def _capture_dom(self, page: Any) -> dict:
        """One page task samples the DOM channels; input values stay excluded."""
        try:
            meta = await page.evaluate("() => (" + DOM_CAPTURE + ")().meta")
        except Exception as exc:
            # These reads happen after an input method has returned. Retrying
            # observation is safe; replaying the input is not.
            raise _ObservationCaptureFailure from exc
        if not isinstance(meta, dict):
            raise _ObservationCaptureFailure
        return meta

    async def _observe(self, session: _Session, action: BrowserAction) -> BrowserObservation:
        page = session.page
        before = await self._capture_dom(page)
        try:
            data = await page.screenshot(
                type="png",
                full_page=False,
                timeout=max(1, min(4000, action.timeout_ms // 3)),
                mask=[page.locator(selector) for selector in self._masks],
            )
        except Exception as exc:
            raise _ObservationCaptureFailure from exc
        # Async UI work can finish while screenshotting. Return the latest
        # single DOM sample and disclose detected drift, rather than pairing
        # pre-screenshot text with a later frame as though both were current.
        # This observation does not wait for stability or repeat an input.
        try:
            meta, element_refs = await session.element_refs.capture(page)
        except Exception as exc:
            raise _ObservationCaptureFailure from exc
        title = _redact(str(meta.get("title") or ""))[:500]
        text = _redact(str(meta.get("text") or ""))
        controls = []
        raw_controls = meta.get("controls", [])
        for index, value in enumerate(raw_controls[:40] if isinstance(raw_controls, list) else []):
            if not isinstance(value, dict) or not isinstance(value.get("tag"), str):
                continue
            selector = value.get("selector")
            if not isinstance(selector, str) or not selector or len(selector) > 512:
                selector = None
            controls.append(
                BrowserControl(
                    tag=_redact(value["tag"])[:32],
                    role=_redact(str(value.get("role") or ""))[:80],
                    label=_redact(str(value.get("label") or ""))[:160],
                    selector=_redact(selector) if selector is not None else None,
                    element_ref=element_refs[index] if index < len(element_refs) else None,
                    clickable=value.get("clickable") is True,
                    hit_test_reachable=(
                        value["hit_test_reachable"]
                        if type(value.get("hit_test_reachable")) is bool
                        else None
                    ),
                    duplicate_id=value.get("duplicate_id") is True,
                )
            )
        if len(data) > self._max_image or data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
            raise BrowserSessionError("screenshot_invalid_or_oversize")
        width, height = struct.unpack(">II", data[16:24])
        content_ref = "sha256:" + hashlib.sha256(data).hexdigest()
        if self._sink is not None:
            content_ref = self._sink(session.ref, data, "image/png")
            if inspect.isawaitable(content_ref):
                content_ref = await content_ref
            if (
                not isinstance(content_ref, str)
                or not content_ref.startswith(("artifact:", "sha256:"))
                or "?" in content_ref
            ):
                raise BrowserSessionError("invalid_artifact_ref")
        image = BrowserImage(
            content_ref=content_ref,
            width=width,
            height=height,
            content_block={
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(data).decode("ascii")
                },
            },
        )
        observation = BrowserObservation(
            session_ref=session.ref,
            action_id=action.action_id,
            action_seq=session.action_seq,
            status="ok",
            complete=True,
            retryability="safe",
            url=_safe_url(str(meta.get("url") or "")),
            status_code=session.last_response_status,
            title=title,
            viewport=self._viewport,
            focus=str(meta.get("focus", ""))[:100],
            scroll_x=meta["scroll_x"] if type(meta.get("scroll_x")) is int else None,
            scroll_y=meta["scroll_y"] if type(meta.get("scroll_y")) is int else None,
            lifecycle=str(meta.get("lifecycle", "unknown"))[:32],
            pointer_lock_active=(
                meta["pointer_lock_active"]
                if type(meta.get("pointer_lock_active")) is bool
                else None
            ),
            text=text[: self._max_text],
            text_truncated=len(text) > self._max_text,
            dom_changed_during_capture=before != meta,
            controls=tuple(controls),
            console=tuple(session.console),
            page_errors=tuple(session.errors),
            page_error_locations=tuple(session.error_locations),
            network_failures=tuple(session.failures),
            logs_truncated=session.logs_truncated,
            screenshot=image,
        )
        session.console.clear()
        session.errors.clear()
        session.error_locations.clear()
        session.failures.clear()
        session.logs_truncated = False
        return observation

    async def _selector_error(
        self, session: _Session, action: BrowserAction, code: str = "selector_not_visible"
    ) -> BrowserObservation:
        # The locator visibility check runs before click/fill/press. Failure at
        # this boundary proves this action was not dispatched, so the model can
        # repair its selector without replaying an uncertain side effect.
        try:
            observation = await self._observe(session, action)
            return observation.model_copy(
                update={
                    "status": "error",
                    "error_code": code,
                    "complete": True,
                    "retryability": "safe",
                }
            )
        except Exception:
            return BrowserObservation(
                session_ref=session.ref,
                action_id=action.action_id,
                action_seq=session.action_seq,
                status="error",
                error_code=code,
                complete=True,
                retryability="safe",
                url=_safe_url(session.page.url),
                viewport=self._viewport,
            )

    async def _navigate(self, session: _Session, url: str, timeout_ms: int) -> None:
        await session.element_refs.clear()
        for _hop in range(10):
            session.navigation_redirect = None
            if not await self._allowed(session.ref, url):
                raise BrowserSessionError("network_denied")
            try:
                response = await session.page.goto(
                    url, wait_until="domcontentloaded", timeout=timeout_ms
                )
                session.last_response_status = response.status if response is not None else None
            except Exception:
                if session.navigation_redirect is None:
                    raise
            if session.navigation_redirect is None:
                return
            url = session.navigation_redirect
        raise BrowserSessionError("redirect_limit_exceeded")

    async def _perform(self, session: _Session, action: BrowserAction) -> BrowserObservation:
        page = session.page
        if action.kind == "close":
            await self._close_session(session)
            return BrowserObservation(
                session_ref=session.ref,
                action_id=action.action_id,
                action_seq=session.action_seq,
                status="ok",
                complete=True,
                retryability="safe",
                viewport=self._viewport,
                lifecycle="closed",
            )
        if action.kind == "navigate":
            await self._navigate(session, action.url, action.timeout_ms)
        else:
            session.navigation_redirect = None
            locator = None
            if action.kind in {"click", "fill", "press"}:
                if action.element_ref is not None:
                    locator = await session.element_refs.resolve(action.element_ref)
                    if locator is None:
                        return await self._selector_error(session, action, "element_ref_stale")
                else:
                    locator = page.locator(action.selector)
                try:
                    timeout = min(3000, max(1, action.timeout_ms // 3))
                    if action.element_ref is not None:
                        await locator.wait_for_element_state("visible", timeout=timeout)
                    else:
                        await locator.wait_for(state="visible", timeout=timeout)
                except Exception:
                    return await self._selector_error(session, action)
            try:
                if action.kind == "click":
                    # Playwright's trial checks actionability without dispatching
                    # the click. An overlay or disabled control can therefore
                    # return a safe repairable error instead of an uncertain
                    # side effect after the real click times out.
                    try:
                        await locator.click(
                            trial=True,
                            timeout=min(3000, max(1, action.timeout_ms // 3)),
                            button=action.button or "left",
                        )
                    except Exception:
                        return await self._selector_error(
                            session, action, "selector_not_actionable"
                        )
                    await locator.click(
                        timeout=max(1, min(10000, action.timeout_ms // 2)),
                        delay=action.duration_ms or 0,
                        button=action.button or "left",
                    )
                elif action.kind == "fill":
                    await locator.fill(
                        action.value, timeout=max(1, min(10000, action.timeout_ms // 2))
                    )
                elif action.kind == "press":
                    await locator.press(
                        action.key,
                        timeout=max(1, min(10000, action.timeout_ms // 2)),
                        delay=action.duration_ms or 0,
                    )
                elif action.kind == "move":
                    if action.x >= self._viewport[0] or action.y >= self._viewport[1]:
                        return self._read_only_failure(session, action, "position_outside_viewport")
                    await page.mouse.move(action.x, action.y, steps=action.steps or 1)
                elif action.kind == "scroll":
                    if action.x is not None:
                        if action.x >= self._viewport[0] or action.y >= self._viewport[1]:
                            return self._read_only_failure(
                                session, action, "position_outside_viewport"
                            )
                        await page.mouse.move(action.x, action.y)
                    # Wheel can target an inner scroll container. Observe the
                    # actual result; dispatch completion is not settled motion.
                    await page.mouse.wheel(action.delta_x, action.delta_y)
            except Exception as exc:
                if session.navigation_redirect is None:
                    raise _ActionDispatchFailure from exc
            if session.navigation_redirect is not None:
                await self._navigate(session, session.navigation_redirect, action.timeout_ms)
            elif action.kind == "click":
                # A click can dispatch an async fetch whose DOM result is not
                # visible when Playwright's click returns. Give that request a
                # bounded chance to settle before capturing the observation;
                # an idle timeout cannot justify replaying the committed click.
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=min(1500, max(1, action.timeout_ms // 4))
                    )
                except Exception:
                    pass
        return await self._observe(session, action)

    async def act(self, ref: BrowserSessionRef, action: BrowserAction) -> BrowserObservation:
        session = self._lookup(ref)
        action = BrowserAction.model_validate_json(action.model_dump_json())
        try:
            return await asyncio.wait_for(self._act(ref, action), action.timeout_ms / 1000)
        except asyncio.TimeoutError:
            if action.action_id in session.results:
                return self._incomplete(session, action, "action_timed_out")
            raise BrowserSessionError("action_not_dispatched_timeout") from None

    async def _act(self, ref: BrowserSessionRef, action: BrowserAction) -> BrowserObservation:
        session = self._lookup(ref)
        # Revalidate data from model_copy callers as well as parsed tool requests.
        action = BrowserAction.model_validate_json(action.model_dump_json())
        fingerprint = hashlib.sha256(action.model_dump_json().encode()).hexdigest()
        async with session.lock:
            if ref != session.ref:
                raise BrowserSessionError("stale_session_ref")
            prior = session.results.get(action.action_id)
            if prior is not None and prior[0] != fingerprint:
                raise BrowserSessionError("action_conflict")
            if action.kind != "close":
                if not await self._live(session):
                    raise BrowserSessionError("session_lost")
                for index, url in enumerate((session.page.url, action.url)):
                    if url is not None and not await self._allowed(ref, url):
                        if index == 0:
                            await self._close_session(session)
                        raise BrowserSessionError("network_denied")
            if prior is not None:
                return BrowserObservation.model_validate_json(prior[1].model_dump_json())
            if len(session.results) >= self._max_actions and action.kind != "close":
                raise BrowserSessionError("action_capacity_reached")
            session.action_seq += 1
            # Cache uncertainty before yielding to the driver. Cancellation and
            # timeouts must never make the same click silently dispatch twice.
            session.results[action.action_id] = (
                fingerprint,
                self._incomplete(session, action, "action_in_flight"),
            )
            try:
                result = await self._perform(session, action)
            except asyncio.CancelledError:
                session.results[action.action_id] = (
                    fingerprint,
                    self._incomplete(session, action, "action_canceled"),
                )
                raise
            except _ObservationCaptureFailure:
                # A failed capture has not reached the artifact sink. Observe
                # again without replaying the already completed input action.
                try:
                    result = await self._observe(session, action)
                except _ObservationCaptureFailure:
                    if action.kind == "screenshot":
                        result = self._read_only_failure(session, action, "observation_failed")
                    else:
                        result = self._incomplete(
                            session, action, "observation_capture_failed_after_action"
                        )
                except Exception:
                    result = self._incomplete(
                        session, action, "observation_sink_failed_or_timed_out"
                    )
            except _ActionDispatchFailure:
                result = self._incomplete(session, action, "action_dispatch_failed_or_timed_out")
            except Exception as error:
                code = (
                    str(error)
                    if isinstance(error, BrowserSessionError)
                    else "action_failed_or_timed_out"
                )
                result = self._incomplete(session, action, code)
            finally:
                session.last_used = time.monotonic()
            session.results[action.action_id] = (fingerprint, result)
            self._trim_images(session)
            return BrowserObservation.model_validate_json(result.model_dump_json())

    def _trim_images(self, session: _Session) -> None:
        images = [
            key for key, (_, result) in session.results.items() if result.screenshot is not None
        ]
        for key in images[: -self._max_cached_images]:
            fingerprint, old = session.results[key]
            # Retain action identity forever within this session: dropping an
            # image must not make an old click eligible to execute again.
            tombstone = old.model_copy(
                update={
                    "screenshot": None,
                    "complete": False,
                    "status": "uncertain",
                    "error_code": "response_cache_evicted",
                    "retryability": "requires_reconcile",
                }
            )
            session.results[key] = (fingerprint, tombstone)

    async def recover(self, ref: BrowserSessionRef) -> BrowserRecovery:
        self._validate_ref(ref)
        session = self._sessions.get(ref.session_id)
        if session is None:
            return BrowserRecovery(status="lost", session_ref=ref, reason="owner_unavailable")
        if session.ref != ref:
            raise BrowserSessionError("stale_session_ref")
        async with session.lock:
            live = await self._live(session)
            return BrowserRecovery(
                status="reconnected" if live else "lost",
                session_ref=ref,
                last_action_seq=session.action_seq,
                reason=None if live else "page_unavailable",
            )

    async def recover_sessions(
        self, refs: tuple[BrowserSessionRef, ...]
    ) -> tuple[BrowserRecovery, ...]:
        # Validate the complete host-owned batch before probing/expiring pages.
        for ref in refs:
            self._validate_ref(ref)
            session = self._sessions.get(ref.session_id)
            if session is not None and session.ref != ref:
                raise BrowserSessionError("stale_session_ref")
        return tuple([await self.recover(ref) for ref in refs])

    async def restart(self, ref: BrowserSessionRef) -> BrowserRecovery:
        self._validate_ref(ref)
        async with self._owner_lock:
            existing = self._sessions.get(ref.session_id)
            if existing is not None:
                if existing.ref != ref:
                    raise BrowserSessionError("stale_session_ref")
                async with existing.lock:
                    await self._close_session(existing)
            replacement = ref.model_copy(
                update={"generation": ref.generation + 1, "lease_epoch": ref.lease_epoch + 1}
            )
            result = await self._new_session(replacement)
            return BrowserRecovery(status="restarted", session_ref=result, last_action_seq=0)

    async def close(self, ref: BrowserSessionRef) -> None:
        session = self._lookup(ref)
        async with session.lock:
            await self._close_session(session)

    async def aclose(self) -> None:
        async with self._owner_lock:
            self._closed = True
            for session in self._sessions.values():
                async with session.lock:
                    await self._close_session(session)
            if self._owned_browser and self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()

    async def execute(self, request: ToolRequest) -> ToolResult:
        try:
            if (
                request.scope != self.scope
                or request.call.name != "native_browser"
                or request.definition != self.definition()
            ):
                raise BrowserSessionError("scope_or_tool_mismatch")
            arguments = dict(request.call.arguments)
            kind = arguments.pop("action", None)
            if kind == "open":
                if arguments:
                    raise BrowserSessionError("invalid_arguments")
                ref = await self.open(request.run_id, operation_key=request.operation_key)
                return ToolResult(
                    status="ok", content={"session_ref": ref.model_dump(mode="json")},
                    browser_session_ref=ref,
                )
            session_id = arguments.pop("session_id", None)
            generation = arguments.pop("generation", None)
            session = self._sessions.get(session_id) if isinstance(session_id, str) else None
            if (
                session is None
                or session.ref.run_id != request.run_id
                or type(generation) is not int
                or session.ref.generation != generation
            ):
                raise BrowserSessionError("session_scope_or_generation_mismatch")
            action = BrowserAction(action_id=request.operation_key, kind=kind, **arguments)
            observation = await self.act(session.ref, action)
            blocks = (
                (ModelImageContent.model_validate(observation.screenshot.content_block),)
                if observation.screenshot is not None
                else ()
            )
            return ToolResult(
                status=observation.status,
                progress_observation_digest=browser_observation_digest(observation),
                content=observation.model_dump(
                    mode="json", exclude={"screenshot": {"content_block"}}
                ),
                content_blocks=blocks,
                browser_session_ref=observation.session_ref,
                browser_session_closed=(
                    kind == "close" and observation.status == "ok"
                    and observation.lifecycle == "closed"
                ),
            )
        except (BrowserSessionError, ValidationError, TypeError, ValueError) as error:
            code = str(error) if isinstance(error, BrowserSessionError) else "invalid_arguments"
            return ToolResult(status="error", content={"error_code": code})
