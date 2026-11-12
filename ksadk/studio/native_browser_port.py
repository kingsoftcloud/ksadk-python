"""Run-scoped browser continuity for a locked Studio Native browser tool.

The Build still grants ``browser_inspect_workspace`` and the ordinary Native
StrictToolExecutor still validates its schema and authorization. This port
changes only the execution owner: one browser page survives successive calls
from the same Run, while each Run gets a separate page and local file server.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
import unicodedata
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from ksadk.harness.browser_adapters.native_playwright import NativePlaywrightBrowser
from ksadk.harness.native.browser_contract import (
    BrowserAction,
    BrowserObservation,
    BrowserRecovery,
    BrowserSessionError,
    BrowserSessionRef,
    browser_observation_digest,
)
from ksadk.harness.native.model import ModelImageContent
from ksadk.harness.native.ports import ToolRequest, ToolResult
from ksadk.harness.native.state import RunScope

_LAUNCH_LABELS = frozenset({"start", "start game", "new game", "play", "开始"})
_CHINESE_LAUNCH = re.compile(
    r"^(?:新(?:的|建)?|开始(?:新(?:的|建)?)?|进入)(?:游戏|世界)$|"
    r"^进入(?!设置|帮助|选项|商店|社区|账号|登录|房间|服务器)"
    r"[\u4e00-\u9fff]{1,10}(?:\s*[▶→])?$"
)
_HEADLESS_POINTER_LOCK_DENIAL = "The root document of this element is not valid for pointer lock."
_VIEWPORT = (1280, 720)
_SAFE_INPUT_ERRORS = frozenset(
    {
        "workspace_path_invalid",
        "workspace_path_outside_scope",
        "browser_entry_not_found",
        "screenshot_path_invalid",
        "browser_actions_invalid",
        "browser_action_invalid",
        "browser_wait_invalid",
        "browser_action_time_budget_exceeded",
        "browser_served_file_limit_exceeded",
        "browser_served_revision_unavailable",
    }
)


def browser_inspect_parameters() -> dict[str, object]:
    """Expose the Studio browser action protocol to the model and executor.

    The legacy builtin accepts an opaque list of dictionaries. Native owns a
    narrower protocol; projecting its actual variants as JSON Schema lets the
    model choose valid actions and rejects malformed batches before approval.
    """

    fields: dict[str, dict[str, object]] = {
        "selector": {"type": "string", "minLength": 1},
        "element_ref": {"type": "string", "minLength": 1, "maxLength": 64},
        "value": {"type": "string"},
        "key": {"type": "string", "minLength": 1},
        "button": {"enum": ["left", "right", "middle"]},
        "duration_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
        "x": {"type": "integer", "minimum": 0, "maximum": _VIEWPORT[0] - 1},
        "y": {"type": "integer", "minimum": 0, "maximum": _VIEWPORT[1] - 1},
        "steps": {"type": "integer", "minimum": 1, "maximum": 30},
        "delta_x": {"type": "integer", "minimum": -10000, "maximum": 10000},
        "delta_y": {"type": "integer", "minimum": -10000, "maximum": 10000},
        "ms": {"type": "integer", "minimum": 0, "maximum": 10000},
    }

    def variant(
        kind: str, *, required: tuple[str, ...] = (), optional: tuple[str, ...] = ()
    ) -> dict:
        return {
            "type": "object",
            "properties": {
                "type": {"const": kind},
                **{name: fields[name] for name in (*required, *optional)},
            },
            "required": ["type", *required],
            "additionalProperties": False,
            **({"oneOf": [{"required": ["selector"]}, {"required": ["element_ref"]}]}
               if kind in {"click", "fill"} else
               {"not": {"required": ["selector", "element_ref"]}} if kind == "press" else {}),
            **({"dependentRequired": {"x": ["y"], "y": ["x"]}} if kind == "scroll" else {}),
        }

    return {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "actions": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "oneOf": [
                        variant(
                            "click", optional=("selector", "element_ref", "button", "duration_ms")
                        ),
                        variant("fill", required=("value",), optional=("selector", "element_ref")),
                        variant(
                            "press", required=("key",),
                            optional=("selector", "element_ref", "duration_ms"),
                        ),
                        variant("move", required=("x", "y"), optional=("steps",)),
                        variant("scroll", required=("delta_x", "delta_y"), optional=("x", "y")),
                        variant("reload"),
                        variant("wait", optional=("ms",)),
                    ]
                },
            },
            "wait_ms": {"type": "integer", "minimum": 0, "maximum": 30000},
            "screenshot_path": {"type": "string", "minLength": 1},
        },
        "required": ["path"],
        "additionalProperties": False,
    }


def _safe_input_error(error: Exception) -> str:
    code = str(error)
    # Pydantic's exception includes the rejected input, which may contain
    # page text or credentials. Only host-authored fixed error codes escape.
    return code if code in _SAFE_INPUT_ERRORS else "browser_action_invalid"


def browser_environment_warning(value: str) -> bool:
    """Keep a known headless permission denial visible but non-blocking."""
    return value.strip() in {
        _HEADLESS_POINTER_LOCK_DENIAL,
        f"error: {_HEADLESS_POINTER_LOCK_DENIAL}",
    }


def _is_launch_label(value: str) -> bool:
    label = value.strip()
    for _ in range(4):
        if not label or unicodedata.category(label[0])[0] not in {"P", "S"}:
            break
        label = label[1:].lstrip()
    label = " ".join(label.split()).casefold()
    return label in _LAUNCH_LABELS or bool(_CHINESE_LAUNCH.fullmatch(label))


def _unique_launch_selector(observation: BrowserObservation) -> str | None:
    """Choose only one visible, id-addressable launch control for local smoke."""
    matches = [
        control.selector
        for control in observation.controls
        if control.selector and control.clickable and _is_launch_label(control.label)
    ]
    return matches[0] if len(matches) == 1 else None


def _duplicate_launch_id(observation: BrowserObservation) -> bool:
    return any(
        control.clickable and control.duplicate_id and _is_launch_label(control.label)
        for control in observation.controls
    )


def _served_revision(
    root: Path, generated_screenshots: dict[Path, tuple[int, int]] | None = None
) -> str:
    """Invalidate an open page after local edits, ignoring tool screenshots.

    This metadata stamp is only a reload decision. The independent verifier
    performs a fresh navigation and does not treat it as trusted provenance.
    """
    digest = hashlib.sha256(b"studio.browser-served-files/v1\0")
    count = 0
    try:
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                name
                for name in dirs
                if name not in {".artifacts", ".git", "node_modules", "__pycache__"}
                and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(files):
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    continue
                info = path.stat()
                # A browser capture is an observation, not an edit to the
                # served app. Only ignore the exact bytes this port wrote;
                # replacing the file later must invalidate the open page.
                if generated_screenshots is not None and generated_screenshots.get(path) == (
                    info.st_size,
                    info.st_mtime_ns,
                ):
                    continue
                relative = path.relative_to(root).as_posix()
                digest.update(f"{relative}\0{info.st_size}\0{info.st_mtime_ns}\0".encode())
                count += 1
                if count > 4096:
                    raise ValueError("browser_served_file_limit_exceeded")
    except OSError as exc:
        raise ValueError("browser_served_revision_unavailable") from exc
    return digest.hexdigest()


class _QuietScopedHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_head(self):
        # SimpleHTTPRequestHandler follows symlinks. Reject the actual resolved
        # target instead of translating it to a sentinel path that could exist.
        target = Path(self.translate_path(self.path)).resolve()
        root = Path(self.directory).resolve()
        if not target.is_relative_to(root):
            self.send_error(403, "outside bound workspace")
            return None
        # The base handler selects an index *after* translating a directory.
        # Check that actual file too; checking the directory alone can let a
        # nested index symlink escape an otherwise valid local server root.
        if target.is_dir():
            for name in getattr(self, "index_pages", ("index.html", "index.htm")):
                index = target / name
                if index.is_file():
                    if not index.resolve().is_relative_to(root):
                        self.send_error(403, "outside bound workspace")
                        return None
                    break
        return super().send_head()


@dataclass
class _RunPage:
    entry: Path
    server: ThreadingHTTPServer
    thread: threading.Thread
    ref: BrowserSessionRef
    source_revision: str

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/{quote(self.entry.name, safe='')}"


class StudioNativeBrowserPort:
    """Keep a page per Run without widening the Build's tool or network grant."""

    def __init__(
        self,
        *,
        scope: RunScope,
        root: Path,
        runtime_id: str,
        evidence_root: Path | None = None,
        reobserve_after_loss: bool = False,
    ) -> None:
        self._scope = scope.model_copy(deep=True)
        if type(reobserve_after_loss) is not bool:
            raise ValueError("browser reobservation policy must be a boolean")
        self._reobserve_after_loss = reobserve_after_loss
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._evidence_root = evidence_root.resolve() if evidence_root is not None else None
        if self._evidence_root is not None:
            if self._evidence_root.is_relative_to(self._root):
                raise ValueError("browser_verification_evidence_must_be_outside_workspace")
            self._evidence_root.mkdir(parents=True, exist_ok=True)
        self._pages: dict[str, _RunPage] = {}
        self._lost_refs: dict[str, BrowserSessionRef] = {}
        self._screenshots: dict[str, Path] = {}
        self._generated_screenshots: dict[Path, tuple[int, int]] = {}
        self._owner_lock = asyncio.Lock()
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        self._browser = NativePlaywrightBrowser(
            scope=scope,
            runtime_id=runtime_id,
            workspace_revision=hashlib.sha256(str(self._root).encode()).hexdigest(),
            network_policy=self._allow_local_request,
            artifact_sink=self._save_screenshot,
            # Playwright's bundled Chromium rejects pointer lock in WebGL on
            # this desktop, preventing first-person games from being tested.
            # Prefer installed Chrome; retain bundled Chromium for hosts
            # without it and expose the chosen channel in every observation.
            launch_channels=("chrome", None),
            viewport=_VIEWPORT,
        )

    def _allow_local_request(self, ref: BrowserSessionRef, url: str) -> bool:
        page = self._pages.get(ref.run_id)
        if page is None or page.ref != ref:
            return False
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and port == page.server.server_port
            and not parsed.username
            and not parsed.password
        )

    def _save_screenshot(self, ref: BrowserSessionRef, payload: bytes, _mime: str) -> str:
        target = self._screenshots.get(ref.run_id)
        if target is None or self._pages.get(ref.run_id, None) is None:
            raise BrowserSessionError("screenshot_target_unavailable")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        info = target.stat()
        self._generated_screenshots[target] = (info.st_size, info.st_mtime_ns)
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _path(self, value: object, *, directory: bool = False) -> Path:
        if not isinstance(value, str) or not value or "\\" in value:
            raise ValueError("workspace_path_invalid")
        relative = PurePosixPath(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("workspace_path_invalid")
        target = (self._root / relative.as_posix()).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError("workspace_path_outside_scope")
        if directory:
            if not target.is_dir():
                raise ValueError("browser_entry_not_found")
            target = (target / "index.html").resolve()
            if not target.is_relative_to(self._root):
                raise ValueError("workspace_path_outside_scope")
        if not target.is_file() or target.suffix.lower() not in {".html", ".htm"}:
            raise ValueError("browser_entry_not_found")
        return target

    def _screenshot_path(self, value: object) -> Path:
        if not isinstance(value, str) or not value or "\\" in value:
            raise ValueError("screenshot_path_invalid")
        relative = PurePosixPath(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("screenshot_path_invalid")
        target = (self._root / relative.as_posix()).resolve()
        if not target.is_relative_to(self._root) or target.suffix.lower() != ".png":
            raise ValueError("screenshot_path_invalid")
        return target

    async def _close_page(self, run_id: str) -> None:
        page = self._pages.pop(run_id, None)
        self._screenshots.pop(run_id, None)
        if page is None:
            return
        try:
            await self._browser.close(page.ref)
        except BrowserSessionError:
            pass
        page.server.shutdown()
        page.server.server_close()
        page.thread.join(timeout=2)

    async def _open_page(
        self, run_id: str, entry: Path, operation_key: str, source_revision: str,
        prior_ref: BrowserSessionRef | None = None,
    ) -> _RunPage:
        handler = partial(_QuietScopedHandler, directory=str(entry.parent))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ref = (
                (await self._browser.restart(prior_ref)).session_ref
                if prior_ref is not None
                else await self._browser.open(run_id, operation_key=operation_key)
            )
            page = _RunPage(
                entry=entry,
                server=server,
                thread=thread,
                ref=ref,
                source_revision=source_revision,
            )
            self._pages[run_id] = page
            return page
        except BaseException:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            raise

    async def recover_sessions(
        self, refs: tuple[BrowserSessionRef, ...]
    ) -> tuple[BrowserRecovery, ...]:
        """Read ownership facts; never launch a replacement or replay actions."""
        async with self._owner_lock:
            if self._closed:
                raise BrowserSessionError("browser_owner_closed")
            reports = []
            for ref in refs:
                if ref.scope != self._scope:
                    raise BrowserSessionError("scope_mismatch")
                async with self._run_locks.setdefault(ref.run_id, asyncio.Lock()):
                    report = await self._browser.recover(ref)
                    page = self._pages.get(ref.run_id)
                    if report.status == "reconnected" and (page is None or page.ref != ref):
                        report = BrowserRecovery(
                            status="lost", session_ref=ref, reason="page_binding_unavailable"
                        )
                    if report.status == "lost" and self._reobserve_after_loss:
                        report = report.model_copy(update={"reobserve_available": True})
                    reports.append(report)
            # A later invalid ref must not leave a partially admitted recovery.
            for report in reports:
                if report.status == "lost":
                    self._lost_refs[report.session_ref.run_id] = report.session_ref
            return tuple(reports)

    async def _act(
        self,
        page: _RunPage,
        *,
        operation_key: str,
        index: int,
        kind: str,
        selector: str | None = None,
        element_ref: str | None = None,
        value: str | None = None,
        key: str | None = None,
        button: str | None = None,
        x: int | None = None,
        y: int | None = None,
        steps: int | None = None,
        delta_x: int | None = None,
        delta_y: int | None = None,
        duration_ms: int | None = None,
    ) -> BrowserObservation:
        action = BrowserAction(
            action_id=f"{operation_key}:{index}",
            kind=kind,
            url=page.url if kind == "navigate" else None,
            selector=selector,
            element_ref=element_ref,
            value=value,
            key=key,
            button=button,
            x=x,
            y=y,
            steps=steps,
            delta_x=delta_x,
            delta_y=delta_y,
            duration_ms=duration_ms,
            timeout_ms=30000 if kind in {"navigate", "click", "fill", "press", "move"} else 20000,
        )
        return await self._browser.act(page.ref, action)

    async def execute(self, request: ToolRequest) -> ToolResult:
        if request.scope != self._scope or request.call.name != "browser_inspect_workspace":
            return ToolResult(status="error", content={"code": "scope_or_tool_mismatch"})
        arguments = request.call.arguments
        try:
            raw_path = arguments.get("path")
            target = self._root / raw_path if isinstance(raw_path, str) else self._root
            entry = self._path(raw_path, directory=target.is_dir())
            screenshot = self._screenshot_path(
                arguments.get("screenshot_path", ".artifacts/browser-check.png")
            )
            raw_actions = arguments.get("actions") or []
            if not isinstance(raw_actions, list) or len(raw_actions) > 20:
                raise ValueError("browser_actions_invalid")
            # Validate the whole batch before opening a page or dispatching an
            # earlier action. A malformed later action cannot leave a partial
            # side effect disguised as a simple input error.
            explicit_wait = 0
            for raw in raw_actions:
                if not isinstance(raw, dict):
                    raise ValueError("browser_action_invalid")
                kind = raw.get("type")
                if kind not in {"click", "fill", "press", "move", "scroll", "reload", "wait"}:
                    raise ValueError("browser_action_invalid")
                allowed = {
                    "click": {"type", "selector", "element_ref", "button", "duration_ms"},
                    "fill": {"type", "selector", "element_ref", "value"},
                    "press": {"type", "selector", "element_ref", "key", "duration_ms"},
                    "move": {"type", "x", "y", "steps"},
                    "scroll": {"type", "delta_x", "delta_y", "x", "y"},
                    "reload": {"type"},
                    "wait": {"type", "ms"},
                }[kind]
                if set(raw) - allowed:
                    raise ValueError("browser_action_invalid")
                if kind in {"click", "fill", "press"}:
                    targets = set(raw) & {"selector", "element_ref"}
                    if len(targets) > 1 or (kind != "press" and not targets):
                        raise ValueError("browser_action_invalid")
                    if any(not isinstance(raw[name], str) or not raw[name] for name in targets):
                        raise ValueError("browser_action_invalid")
                    if "element_ref" in targets and len(raw["element_ref"]) > 64:
                        raise ValueError("browser_action_invalid")
                if kind == "press" and (
                    not isinstance(raw.get("key"), str)
                    or not raw["key"]
                    or (
                        "selector" in raw
                        and (not isinstance(raw["selector"], str) or not raw["selector"])
                    )
                ):
                    raise ValueError("browser_action_invalid")
                if kind == "fill" and not isinstance(raw.get("value"), str):
                    raise ValueError("browser_action_invalid")
                if kind == "click" and raw.get("button", "left") not in {"left", "right", "middle"}:
                    raise ValueError("browser_action_invalid")
                if kind == "move" and (
                    type(raw.get("x")) is not int
                    or type(raw.get("y")) is not int
                    or not 0 <= raw["x"] < _VIEWPORT[0]
                    or not 0 <= raw["y"] < _VIEWPORT[1]
                    or type(raw.get("steps", 1)) is not int
                    or not 1 <= raw.get("steps", 1) <= 30
                ):
                    raise ValueError("browser_action_invalid")
                if kind == "scroll" and (
                    any(type(raw.get(name)) is not int or not -10000 <= raw[name] <= 10000
                        for name in ("delta_x", "delta_y"))
                    or ("x" in raw) != ("y" in raw)
                    or ("x" in raw and (
                        type(raw["x"]) is not int or type(raw["y"]) is not int
                        or not 0 <= raw["x"] < _VIEWPORT[0]
                        or not 0 <= raw["y"] < _VIEWPORT[1]
                    ))
                ):
                    raise ValueError("browser_action_invalid")
                if kind == "wait":
                    ms = raw.get("ms", 0)
                    if type(ms) is not int or not 0 <= ms <= 10000:
                        raise ValueError("browser_action_invalid")
                    explicit_wait += ms
                if "duration_ms" in raw:
                    duration = raw["duration_ms"]
                    if (
                        kind not in {"click", "press"}
                        or type(duration) is not int
                        or not 0 <= duration <= 5000
                    ):
                        raise ValueError("browser_action_invalid")
                    explicit_wait += duration
            wait_ms = arguments.get("wait_ms", 1000)
            if type(wait_ms) is not int or not 0 <= wait_ms <= 30000:
                raise ValueError("browser_wait_invalid")
            if wait_ms + explicit_wait > 30000:
                raise ValueError("browser_action_time_budget_exceeded")
        except (TypeError, ValueError) as exc:
            return ToolResult(status="error", content={"code": _safe_input_error(exc)})
        async with self._owner_lock:
            if self._closed:
                return ToolResult(status="error", content={"code": "browser_owner_closed"})
            run_lock = self._run_locks.setdefault(request.run_id, asyncio.Lock())
        async with run_lock:
            if self._closed:
                return ToolResult(status="error", content={"code": "browser_owner_closed"})
            prior_ref = self._lost_refs.get(request.run_id)
            if prior_ref is not None and raw_actions != [{"type": "reload"}]:
                return ToolResult(
                    status="error", content={"code": "browser_session_lost",
                                              "required_actions": [{"type": "reload"}]},
                    browser_session_ref=prior_ref,
                )
            try:
                source_revision = _served_revision(entry.parent, self._generated_screenshots)
            except ValueError as exc:
                return ToolResult(status="error", content={"code": _safe_input_error(exc)})
            page = self._pages.get(request.run_id)
            navigation_reason = (
                "session_restarted"
                if prior_ref is not None
                else "initial_open"
                if page is None
                else "entry_changed"
                if page.entry != entry
                else "source_changed"
                if page.source_revision != source_revision
                else "none"
            )
            first = page is None or page.entry != entry
            if page is not None and page.entry != entry:
                await self._close_page(request.run_id)
            if first:
                try:
                    page = await self._open_page(
                        request.run_id, entry, request.operation_key, source_revision, prior_ref
                    )
                except BrowserSessionError as exc:
                    # No workspace navigation or user input has been dispatched.
                    # Preserve the startup diagnosis instead of turning it into
                    # an unknown effect outcome in the outer tool executor.
                    return ToolResult(status="error", content={"code": str(exc)})
                if prior_ref is not None:
                    self._lost_refs.pop(request.run_id)
                    # The initial navigation is the explicitly requested reload.
                    # No old click/fill/press batch can accompany it.
                    raw_actions = []
            assert page is not None
            changed = page.source_revision != source_revision
            self._screenshots[request.run_id] = screenshot
            results: list[dict[str, object]] = []
            try:
                observation = await self._act(
                    page,
                    operation_key=request.operation_key,
                    index=0,
                    kind="navigate" if first or changed else "screenshot",
                )
                observations = [observation]
                if observation.status != "ok":
                    return self._result(observations, entry, screenshot, results, navigation_reason)
                page.source_revision = source_revision
                if wait_ms:
                    await asyncio.sleep(wait_ms / 1000)
                for index, raw in enumerate(raw_actions):
                    if not isinstance(raw, dict):
                        raise ValueError("browser_action_invalid")
                    kind = raw.get("type")
                    if kind == "wait":
                        ms = raw.get("ms", 0)
                        if type(ms) is not int or not 0 <= ms <= 10000:
                            raise ValueError("browser_action_invalid")
                        await asyncio.sleep(ms / 1000)
                        results.append({"index": index, "type": kind, "ok": True})
                        continue
                    if kind not in {"click", "fill", "press", "move", "scroll", "reload"}:
                        raise ValueError("browser_action_invalid")
                    observation = await self._act(
                        page,
                        operation_key=request.operation_key,
                        index=index + 1,
                        kind="navigate" if kind == "reload" else kind,
                        selector=(
                            str(raw.get("selector") or "body")
                            if kind in {"click", "fill", "press"} and "element_ref" not in raw
                            else None
                        ),
                        element_ref=raw.get("element_ref"),
                        value=str(raw.get("value") or "") if kind == "fill" else None,
                        key=str(raw.get("key") or "") if kind == "press" else None,
                        button=raw.get("button") if kind == "click" else None,
                        x=raw.get("x") if kind in {"move", "scroll"} else None,
                        y=raw.get("y") if kind in {"move", "scroll"} else None,
                        steps=raw.get("steps") if kind == "move" else None,
                        delta_x=raw.get("delta_x") if kind == "scroll" else None,
                        delta_y=raw.get("delta_y") if kind == "scroll" else None,
                        duration_ms=raw.get("duration_ms") if kind in {"click", "press"} else None,
                    )
                    observations.append(observation)
                    results.append({"index": index, "type": kind, "ok": observation.status == "ok"})
                    if observation.status != "ok":
                        return self._result(
                            observations, entry, screenshot, results, navigation_reason
                        )
                # A final observation after a wait or action is the evidence
                # returned to the model and optional vision adapter.
                observation = await self._act(
                    page,
                    operation_key=request.operation_key,
                    index=len(raw_actions) + 1,
                    kind="screenshot",
                )
                observations.append(observation)
                return self._result(observations, entry, screenshot, results, navigation_reason)
            except ValueError as exc:
                return ToolResult(status="error", content={"code": _safe_input_error(exc)})
            except BrowserSessionError as exc:
                return ToolResult(status="error", content={"code": str(exc)})

    async def verify_page(
        self, *, run_id: str, entry_path: str, candidate_id: str
    ) -> BrowserObservation:
        """Host-only cold browser check and bounded launch, with external evidence.

        Only a trusted VerificationPort calls this method. The product composer
        creates that port solely when the Build grants this browser capability.
        This observation can reject a broken page; it cannot certify the full
        user goal or visual quality.
        """
        if self._evidence_root is None:
            raise ValueError("browser_verification_evidence_unavailable")
        entry = self._path(entry_path)
        source_revision = _served_revision(entry.parent)
        async with self._owner_lock:
            if self._closed:
                raise BrowserSessionError("browser_owner_closed")
            run_lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with run_lock:
            if self._closed:
                raise BrowserSessionError("browser_owner_closed")
            # The Agent page may already have localStorage, cookies and JS
            # state from its tool calls. A fresh browser context is required
            # to verify a deliverable that a user will open for the first time.
            verification_run_id = (
                f"{run_id}:host-verify:" + hashlib.sha256(candidate_id.encode()).hexdigest()[:16]
            )
            await self._close_page(verification_run_id)
            try:
                page = await self._open_page(
                    verification_run_id, entry, f"verify:{candidate_id}:open", source_revision
                )
                name = hashlib.sha256(f"{run_id}\0{candidate_id}".encode()).hexdigest() + ".png"
                self._screenshots[verification_run_id] = self._evidence_root / name
                first = await self._act(
                    page,
                    operation_key=f"verify:{candidate_id}",
                    index=0,
                    kind="navigate",
                )
                if first.status != "ok":
                    return first
                # Dynamic applications often leave a loading screen at
                # DOMContentLoaded. Retain errors from every observation.
                await asyncio.sleep(1)
                settled = await self._act(
                    page,
                    operation_key=f"verify:{candidate_id}",
                    index=1,
                    kind="screenshot",
                )
                observations = [first, settled]
                if _duplicate_launch_id(settled):
                    # Duplicate IDs make the actual user action ambiguous.
                    # Perfectly overlapping controls can look like one in a
                    # screenshot, so reject the broken DOM before finalizing.
                    return settled.model_copy(
                        update={
                            "status": "error",
                            "error_code": "browser_launch_control_ambiguous",
                        }
                    )
                selector = _unique_launch_selector(settled)
                if settled.status == "ok" and selector is not None:
                    launched = await self._act(
                        page,
                        operation_key=f"verify:{candidate_id}",
                        index=2,
                        kind="click",
                        selector=selector,
                    )
                    observations.append(launched)
                    if launched.status == "ok":
                        await asyncio.sleep(1)
                        observations.append(
                            await self._act(
                                page,
                                operation_key=f"verify:{candidate_id}",
                                index=3,
                                kind="screenshot",
                            )
                        )
                latest = observations[-1]
                return latest.model_copy(
                    update={
                        "console": tuple(item for row in observations for item in row.console),
                        "page_errors": tuple(
                            item for row in observations for item in row.page_errors
                        ),
                        "page_error_locations": tuple(
                            item for row in observations for item in row.page_error_locations
                        ),
                        "network_failures": tuple(
                            item for row in observations for item in row.network_failures
                        ),
                    }
                )
            finally:
                await self._close_page(verification_run_id)

    def _result(
        self,
        observations: list[BrowserObservation],
        entry: Path,
        screenshot: Path,
        actions: list[dict[str, object]],
        navigation_reason: str,
    ) -> ToolResult:
        observation = observations[-1]
        console = [item for seen in observations for item in seen.console]
        page_errors = [item for seen in observations for item in seen.page_errors]
        page_error_locations = [item for seen in observations for item in seen.page_error_locations]
        network_failures = [item for seen in observations for item in seen.network_failures]
        console_errors = [item for item in console if item.startswith(("error:", "assert:"))]
        environment_warnings = [
            item for item in (*console_errors, *page_errors) if browser_environment_warning(item)
        ]
        console_errors = [item for item in console_errors if not browser_environment_warning(item)]
        page_errors = [item for item in page_errors if not browser_environment_warning(item)]
        errors = (*console_errors, *page_errors, *network_failures)
        rendered = bool(observation.text or observation.controls or observation.screenshot)
        ok = (
            all(seen.status == "ok" for seen in observations)
            and (observation.status_code is None or observation.status_code < 400)
            and rendered
            and not errors
            and all(item["ok"] is True for item in actions)
        )
        content = {
            "ok": ok,
            "browser_channel": self._browser.browser_channel,
            "browser_version": self._browser.browser_version,
            "entry_path": entry.relative_to(self._root).as_posix(),
            "page_navigation_reason": navigation_reason,
            "title": observation.title,
            "status_code": observation.status_code,
            "rendered": rendered,
            "body_text_preview": observation.text[:2000],
            "dom_changed_during_capture": observation.dom_changed_during_capture,
            "scroll_x": observation.scroll_x,
            "scroll_y": observation.scroll_y,
            "console_errors": console_errors[:20],
            "page_errors": page_errors[:20],
            "page_error_locations": page_error_locations[:20],
            "environment_warnings": environment_warnings[:20],
            "request_failures": network_failures[:20],
            "actions_ok": all(item["ok"] is True for item in actions),
            "actions": actions,
            "browser_session_id": observation.session_ref.session_id,
            "browser_action_seq": observation.action_seq,
            "error_code": observation.error_code,
            "pointer_lock_active": observation.pointer_lock_active,
            "controls": [
                {
                    "tag": control.tag,
                    "role": control.role,
                    "label": control.label,
                    "selector": control.selector,
                    "element_ref": control.element_ref,
                    "clickable": control.clickable,
                    "hit_test_reachable": control.hit_test_reachable,
                    "duplicate_id": control.duplicate_id,
                }
                for control in observation.controls[:20]
            ],
        }
        if observation.screenshot is not None:
            content["screenshot_path"] = screenshot.relative_to(self._root).as_posix()
        blocks = (
            (ModelImageContent.model_validate(observation.screenshot.content_block),)
            if observation.screenshot is not None
            else ()
        )
        return ToolResult(
            status="uncertain" if observation.status == "uncertain" else "ok" if ok else "error",
            progress_observation_digest=browser_observation_digest(observation),
            content=content,
            content_blocks=blocks,
            browser_session_ref=observation.session_ref,
        )

    async def aclose(self) -> None:
        async with self._owner_lock:
            self._closed = True
            run_locks = tuple(self._run_locks.items())
        for run_id, lock in run_locks:
            async with lock:
                await self._close_page(run_id)
        await self._browser.aclose()
