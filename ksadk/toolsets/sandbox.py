from __future__ import annotations

import logging
import math
import os
import shlex
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from ksadk.runtime_context import get_current_tool_execution_context_or_default
from ksadk.sandbox import (
    LocalProcessSandboxBackend,
    SandboxError,
    SandboxInputFile,
    create_sandbox_backend,
)
from ksadk.sandbox.registry import GLOBAL_SANDBOX_REGISTRY
from ksadk.tools.gateway import ToolPolicy, check_command_policy, default_tool_gateway
from ksadk.tools.result_budget import budget_text_fields
from ksadk.toolsets._langchain import as_tool
from ksadk.toolsets.workspace import resolve_workspace_path, workspace_relative, workspace_root

logger = logging.getLogger(__name__)


_SANDBOX_TOOL_POLICIES = {
    "sandbox_status": ToolPolicy(risk_level="low"),
    "run_command": ToolPolicy(risk_level="high", side_effects=("sandbox_command_execution",)),
    "run_code": ToolPolicy(risk_level="high", side_effects=("sandbox_code_execution",)),
    "browser_inspect_workspace": ToolPolicy(
        risk_level="low", side_effects=("workspace_screenshot_write",)
    ),
}


def _gateway():
    return default_tool_gateway(_SANDBOX_TOOL_POLICIES)


def _dict_result(value: Any, *, tool_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{tool_name} returned {type(value).__name__}, expected dict")
    return dict(value)


def sandbox_backend_name() -> str:
    explicit = os.environ.get("KSADK_SANDBOX_BACKEND", "").strip().lower()
    if explicit:
        return explicit
    if os.environ.get("KSADK_SANDBOX_TEMPLATE_ID") or os.environ.get(
        "KSADK_SKILL_RUNTIME_TEMPLATE_ID"
    ):
        return "e2b"
    return "none"


def _sandbox_execution_backend_name() -> str:
    explicit = os.environ.get("KSADK_SANDBOX_BACKEND", "").strip().lower()
    return explicit or "e2b"


def sandbox_status() -> dict:
    """Report configured AgentEngine sandbox status and boundaries."""

    return _dict_result(
        _gateway().invoke("sandbox_status", _sandbox_status_impl),
        tool_name="sandbox_status",
    )


def _sandbox_status_impl() -> dict[str, Any]:
    backend = sandbox_backend_name()
    timeout = int(
        os.environ.get("KSADK_SANDBOX_TIMEOUT")
        or os.environ.get("KSADK_SKILL_RUNTIME_TIMEOUT")
        or "600"
    )
    idle_ttl = _int_env("KSADK_SANDBOX_IDLE_TTL_SECONDS", 300)
    max_sessions = _int_env("KSADK_SANDBOX_MAX_SESSIONS", 0)
    template_id = (
        os.environ.get("KSADK_SANDBOX_TEMPLATE_ID")
        or os.environ.get("KSADK_SKILL_RUNTIME_TEMPLATE_ID")
        or ""
    )
    isolated = backend not in {"local", "local_process", "pod", "pod_process"}
    latest = GLOBAL_SANDBOX_REGISTRY.latest()
    now = time.time()
    return {
        "ok": True,
        "backend": backend,
        "enabled": backend not in {"", "disabled", "none", "off"},
        "isolated": isolated,
        "template_bound": bool(template_id),
        "template_id": template_id,
        "timeout_seconds": timeout,
        "ttl_seconds": _sandbox_ttl_seconds(),
        "idle_ttl_seconds": idle_ttl,
        "max_sessions": max_sessions,
        "sandbox_id": latest.sandbox_id if latest else "",
        "created_at": latest.created_at if latest else None,
        "last_used_at": latest.last_used_at if latest else None,
        "expires_at": latest.expires_at if latest else None,
        "idle_seconds": int(now - latest.last_used_at) if latest else None,
        "boundary": _sandbox_boundary(backend),
    }


def run_command(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    background: bool = False,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a shell command inside the configured governed sandbox boundary."""

    return _dict_result(
        _gateway().invoke(
            "run_command",
            _run_command_impl,
            command,
            cwd,
            timeout,
            env,
            background,
            approval=approval,
        ),
        tool_name="run_command",
    )


def _run_command_impl(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    background: bool = False,
) -> dict[str, Any]:
    command_text = str(command or "").strip()
    if not command_text:
        return {"ok": False, "error_message": "command is required"}
    policy = _dict_result(check_command_policy(command_text), tool_name="check_command_policy")
    if not policy.get("ok"):
        return policy
    if background:
        return {
            "ok": False,
            "error_type": "background_not_supported",
            "error_message": "background commands are not supported in P0",
        }
    try:
        entry, _created = _sandbox_entry("ksadk-direct")
        from ksadk.harness.tool_output import current_tool_output_observer

        observer = current_tool_output_observer()
        observed = getattr(entry.session, "run_command_observed", None)
        if observer is not None and callable(observed):
            result = observed(command_text, timeout=timeout, env=env, cwd=cwd, on_output=observer)
        else:
            result = entry.session.run_command(command_text, timeout=timeout, env=env, cwd=cwd)
        payload = {
            "ok": True,
            "backend": f"sandbox/{_sandbox_execution_backend_name()}",
            "sandbox_id": entry.sandbox_id,
            "command": command_text,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
        return _dict_result(
            budget_text_fields(
                payload,
                tool_name="run_command",
                fields=("stdout", "stderr"),
                metadata={"tool_use_id": entry.sandbox_id},
            ),
            tool_name="run_command",
        )
    except SandboxError as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}


def run_code(
    code: str,
    language: str = "python",
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a code snippet through the sandbox; this is not a shell replacement."""

    return _dict_result(
        _gateway().invoke(
            "run_code", _run_code_impl, code, language, timeout, env, approval=approval
        ),
        tool_name="run_code",
    )


def _run_code_impl(
    code: str,
    language: str = "python",
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    source = str(code or "")
    if not source:
        return {"ok": False, "error_message": "code is required"}
    backend_name = _sandbox_execution_backend_name()
    if not _sandbox_backend_isolated(backend_name):
        return {
            "ok": False,
            "error_type": "isolated_sandbox_required",
            "error_message": (
                "run_code requires an isolated sandbox backend; local_process and "
                "pod_process are not supported for snippet execution"
            ),
            "backend": backend_name,
            "boundary": _sandbox_boundary(backend_name),
        }
    language_name = _normalize_language(language)
    try:
        suffix, command_prefix = _language_command(language_name)
    except ValueError as exc:
        return {"ok": False, "error_message": str(exc)}

    path = f"/tmp/ksadk-run-code-{uuid4().hex}{suffix}"
    try:
        entry, _created = _sandbox_entry("ksadk-code")
        entry.session.write_file(path, source)
        command = f"{command_prefix} {shlex.quote(path)}"
        result = entry.session.run_command(command, timeout=timeout, env=env)
        payload = {
            "ok": True,
            "backend": f"sandbox/{_sandbox_execution_backend_name()}",
            "sandbox_id": entry.sandbox_id,
            "execution_model": "snippet_runner",
            "boundary": _sandbox_boundary(_sandbox_execution_backend_name()),
            "language": language_name,
            "path": path,
            "command": command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
        return _dict_result(
            budget_text_fields(
                payload,
                tool_name="run_code",
                fields=("stdout", "stderr"),
                metadata={"tool_use_id": entry.sandbox_id},
            ),
            tool_name="run_code",
        )
    except SandboxError as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}


def browser_inspect_workspace(
    path: str,
    actions: list[dict[str, Any]] | None = None,
    wait_ms: int = 1000,
    screenshot_path: str = ".artifacts/browser-check.png",
) -> dict[str, Any]:
    """Open built workspace HTML in a real headless browser and report render/errors.

    Pass a built HTML file or its directory (for example ``app/dist``). Optional
    actions support ``click``, ``fill``, ``press``, ``move``, and ``wait`` without exposing
    arbitrary page script execution. The browser serves only the selected local
    workspace directory and stores its screenshot inside the workspace. ``click``
    and ``press`` accept a bounded ``duration_ms`` to exercise held input.
    Click accepts a left/right/middle ``button``; move takes viewport CSS pixel
    ``x``/``y`` and at most 30 interpolated ``steps``.
    """

    return _dict_result(
        _gateway().invoke(
            "browser_inspect_workspace",
            _browser_inspect_workspace_impl,
            path,
            actions,
            wait_ms,
            screenshot_path,
        ),
        tool_name="browser_inspect_workspace",
    )


def _browser_inspect_workspace_impl(
    path: str,
    actions: list[dict[str, Any]] | None = None,
    wait_ms: int = 1000,
    screenshot_path: str = ".artifacts/browser-check.png",
) -> dict[str, Any]:
    target = resolve_workspace_path(path)
    if target.is_dir():
        document_root = target
        entry = target / "index.html"
    else:
        document_root = target.parent
        entry = target
    if not entry.is_file() or entry.suffix.lower() not in {".html", ".htm"}:
        return {
            "ok": False,
            "error_type": "browser_entry_not_found",
            "error_message": (
                "path must resolve to a built HTML file or directory containing index.html"
            ),
        }
    screenshot = resolve_workspace_path(screenshot_path)
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    bounded_wait = max(0, min(int(wait_ms or 0), 30_000))
    bounded_actions = list(actions or [])[:50]
    try:
        result = _capture_workspace_page(
            document_root=document_root,
            entry_name=entry.name,
            actions=bounded_actions,
            wait_ms=bounded_wait,
            screenshot=screenshot,
        )
    except ModuleNotFoundError:
        return {
            "ok": False,
            "error_type": "browser_runtime_unavailable",
            "error_message": (
                "Playwright is not installed; install the browser runtime "
                "before browser verification"
            ),
        }
    except Exception as exc:  # noqa: BLE001 - surfaced as a bounded tool result
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:2000],
        }
    result["entry_path"] = workspace_relative(entry)
    result["screenshot_path"] = workspace_relative(screenshot)
    return result


class _QuietWorkspaceHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: Any) -> None:
        del args


def _capture_workspace_page(
    *,
    document_root: Path,
    entry_name: str,
    actions: list[dict[str, Any]],
    wait_ms: int,
    screenshot: Path,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    handler = partial(_QuietWorkspaceHandler, directory=str(document_root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    console_errors: list[str] = []
    page_errors: list[str] = []
    request_failures: list[str] = []
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception:
                browser = playwright.chromium.launch(headless=True, channel="chrome")
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 720})
                page.on(
                    "console",
                    lambda message: (
                        console_errors.append(message.text) if message.type == "error" else None
                    ),
                )
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on(
                    "requestfailed",
                    lambda request: request_failures.append(
                        f"{request.method} {request.url}: {request.failure or 'failed'}"
                    ),
                )
                url = f"http://127.0.0.1:{server.server_port}/{entry_name}"
                response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                if wait_ms:
                    page.wait_for_timeout(wait_ms)
                action_results = _perform_browser_actions(page, actions)
                page.screenshot(path=str(screenshot), full_page=True)
                body_text = page.locator("body").inner_text(timeout=5000).strip()
                visual_count = page.locator("canvas, svg, img, video").count()
                canvas_metrics = _canvas_visual_metrics(page)
                app_child_count = page.locator("#app > *").count()
                rendered = bool(body_text or visual_count or app_child_count)
                errors = [*page_errors, *console_errors, *request_failures]
                visual_warnings = [
                    "large canvas appears visually uniform after requested actions"
                    for metrics in canvas_metrics
                    if metrics["viewport_coverage"] >= 0.2 and metrics["visually_uniform"]
                ]
                actions_ok = all(result.get("ok") is True for result in action_results)
                return {
                    "ok": _browser_inspection_succeeded(
                        response_ok=bool(response and response.ok),
                        rendered=rendered,
                        errors=errors,
                        action_results=action_results,
                    ),
                    "status_code": response.status if response else None,
                    "title": page.title(),
                    "rendered": rendered,
                    "body_text_preview": body_text[:2000],
                    "visual_element_count": visual_count,
                    "canvas_metrics": canvas_metrics,
                    "visual_warnings": visual_warnings,
                    "app_child_count": app_child_count,
                    "console_errors": console_errors[:20],
                    "page_errors": page_errors[:20],
                    "request_failures": request_failures[:20],
                    "actions_ok": actions_ok,
                    "actions": action_results,
                }
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _canvas_visual_metrics(page: Any) -> list[dict[str, Any]]:
    """Return bounded pixel evidence for visible canvases.

    DOM presence and an error-free console do not prove that a WebGL scene
    rendered: a completely blank canvas satisfies both.  Sampling the actual
    Playwright screenshot keeps this diagnostic independent of canvas APIs and
    works for 2D and WebGL content alike.  It is evidence, not a universal pass
    criterion, because a deliberately solid-colour canvas can be valid.
    """

    viewport = page.viewport_size or {"width": 1280, "height": 720}
    viewport_area = max(1, int(viewport["width"]) * int(viewport["height"]))
    metrics: list[dict[str, Any]] = []
    canvases = page.locator("canvas")
    for index in range(min(canvases.count(), 3)):
        canvas = canvases.nth(index)
        box = canvas.bounding_box()
        if not box or box["width"] < 8 or box["height"] < 8:
            continue
        try:
            screenshot = _isolated_canvas_screenshot(page, canvas)
            variation = _image_variation_metrics(screenshot)
        except Exception as exc:  # noqa: BLE001 - diagnostic must not hide page result
            metrics.append({"index": index, "error": str(exc)[:500]})
            continue
        metrics.append(
            {
                "index": index,
                "width": round(float(box["width"]), 1),
                "height": round(float(box["height"]), 1),
                "viewport_coverage": round(
                    float(box["width"] * box["height"]) / viewport_area,
                    4,
                ),
                **variation,
            }
        )
    return metrics


def _isolated_canvas_screenshot(page: Any, canvas: Any) -> bytes:
    """Capture canvas pixels without HUD/DOM overlays inside its bounding box."""

    token = page.evaluate(
        """canvas => {
          const token = `ksadk-${Date.now()}-${Math.random()}`;
          for (const element of document.body.querySelectorAll('*')) {
            if (element === canvas || element.contains(canvas)) continue;
            element.dataset.ksadkVisualToken = token;
            element.dataset.ksadkVisualVisibility = element.style.visibility || '';
            element.style.visibility = 'hidden';
          }
          return token;
        }""",
        canvas.element_handle(),
    )
    try:
        return canvas.screenshot(timeout=5000)
    finally:
        page.evaluate(
            """token => {
              for (const element of document.querySelectorAll(
                `[data-ksadk-visual-token="${token}"]`
              )) {
                element.style.visibility = element.dataset.ksadkVisualVisibility || '';
                delete element.dataset.ksadkVisualToken;
                delete element.dataset.ksadkVisualVisibility;
              }
            }""",
            token,
        )


def _image_variation_metrics(image_bytes: bytes) -> dict[str, Any]:
    from PIL import Image

    with Image.open(BytesIO(image_bytes)) as image:
        sample = image.convert("RGB")
        sample.thumbnail((64, 64))
        quantized = sample.quantize(colors=16)
        histogram = [count for count, _colour in quantized.getcolors() or []]
    total = max(1, sum(histogram))
    dominant_ratio = max(histogram, default=total) / total
    entropy_bits = -sum((count / total) * math.log2(count / total) for count in histogram if count)
    colour_buckets = len(histogram)
    return {
        "colour_buckets": colour_buckets,
        "dominant_ratio": round(dominant_ratio, 4),
        "entropy_bits": round(entropy_bits, 4),
        "visually_uniform": colour_buckets <= 2 or dominant_ratio >= 0.995,
    }


def _browser_inspection_succeeded(
    *,
    response_ok: bool,
    rendered: bool,
    errors: list[str],
    action_results: list[dict[str, Any]],
) -> bool:
    return bool(
        response_ok
        and rendered
        and not errors
        and all(result.get("ok") is True for result in action_results)
    )


def _perform_browser_actions(page: Any, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        kind = str(action.get("type") or "").strip().lower()
        selector = str(action.get("selector") or "body")
        try:
            duration = action.get("duration_ms", 0)
            if (
                type(duration) is not int
                or not 0 <= duration <= 5000
                or (kind not in {"click", "press"} and "duration_ms" in action)
            ):
                raise ValueError("invalid browser action duration_ms")
            if kind == "click":
                button = action.get("button", "left")
                if button not in {"left", "right", "middle"}:
                    raise ValueError("invalid browser click button")
                page.locator(selector).click(timeout=10000, delay=duration, button=button)
            elif kind == "fill":
                page.locator(selector).fill(str(action.get("value") or ""), timeout=5000)
            elif kind == "press":
                page.locator(selector).press(
                    str(action.get("key") or ""), timeout=10000, delay=duration
                )
            elif kind == "move":
                x, y, steps = action.get("x"), action.get("y"), action.get("steps", 1)
                if (
                    type(x) is not int
                    or type(y) is not int
                    or not 0 <= x < 1280
                    or not 0 <= y < 720
                    or type(steps) is not int
                    or not 1 <= steps <= 30
                ):
                    raise ValueError("invalid browser move coordinates")
                page.mouse.move(x, y, steps=steps)
            elif kind == "wait":
                page.wait_for_timeout(max(0, min(int(action.get("ms") or 0), 10_000)))
            else:
                raise ValueError(f"unsupported browser action: {kind or '<missing>'}")
            results.append({"index": index, "type": kind, "ok": True})
        except Exception as exc:  # noqa: BLE001 - each action remains independently auditable
            results.append(
                {
                    "index": index,
                    "type": kind,
                    "ok": False,
                    "error": str(exc)[:1000],
                }
            )
    return results


def _normalize_language(language: str) -> str:
    value = str(language or "python").strip().lower()
    aliases = {
        "py": "python",
        "python3": "python",
        "js": "javascript",
        "node": "javascript",
        "nodejs": "javascript",
        "sh": "bash",
        "shell": "bash",
    }
    return aliases.get(value, value)


def _language_command(language: str) -> tuple[str, str]:
    if language == "python":
        return ".py", "python"
    if language == "javascript":
        return ".js", "node"
    if language == "bash":
        return ".sh", "bash"
    raise ValueError(f"unsupported language: {language}")


def _sandbox_entry(prefix: str):
    backend_name = _sandbox_execution_backend_name()
    # Local Studio commands must see the exact same identity-scoped workspace as
    # the file tools.  The generic sandbox factory intentionally owns a stable
    # session directory, which is correct for isolated backends but previously
    # left ``run_command`` looking at a different, empty directory from
    # ``read_workspace_file``/``write_workspace_file``.
    if backend_name in {"local", "local_process"}:
        backend = LocalProcessSandboxBackend(
            workspace_root=workspace_root(),
            backend_name="local_process",
        )
    else:
        backend = create_sandbox_backend()
    ttl = _sandbox_ttl_seconds()
    idle_ttl = _int_env("KSADK_SANDBOX_IDLE_TTL_SECONDS", 300)
    max_sessions = _int_env("KSADK_SANDBOX_MAX_SESSIONS", 0) or None
    key = _sandbox_session_key(prefix)
    isolated = _sandbox_backend_isolated(backend_name)
    input_files = _workspace_input_files() if isolated else []
    entry, created = GLOBAL_SANDBOX_REGISTRY.get_or_create(
        key=key,
        backend_name=backend_name,
        backend=backend,
        ttl_seconds=ttl,
        idle_ttl_seconds=idle_ttl,
        max_sessions=max_sessions,
        input_files=input_files,
        isolated=isolated,
    )
    logger.info(
        "ksadk.sandbox.%s",
        "create" if created else "reuse",
        extra={
            "sandbox_backend": backend_name,
            "sandbox_id": entry.sandbox_id,
            "isolated": isolated,
            "workspace_input_files": len(input_files) if created else 0,
        },
    )
    return entry, created


def _sandbox_session_key(prefix: str) -> str:
    explicit = os.environ.get("KSADK_SANDBOX_SESSION_ID")
    if explicit:
        return explicit
    context = get_current_tool_execution_context_or_default()
    if context.session_id:
        return f"ksadk-session:{context.session_id}"
    return f"{prefix}-shared"


def _sandbox_backend_isolated(backend_name: str) -> bool:
    return backend_name not in {"local", "local_process", "pod", "pod_process"}


def _workspace_input_files() -> list[SandboxInputFile]:
    root = workspace_root()
    if not root.exists():
        return []
    max_files = _int_env("KSADK_SANDBOX_SYNC_MAX_FILES", 2000)
    max_file_bytes = _int_env("KSADK_SANDBOX_SYNC_MAX_FILE_BYTES", 2 * 1024 * 1024)
    max_total_bytes = _int_env("KSADK_SANDBOX_SYNC_MAX_TOTAL_BYTES", 25 * 1024 * 1024)
    skipped_dirs = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
    files: list[SandboxInputFile] = []
    total_bytes = 0
    for item in sorted(root.rglob("*")):
        if len(files) >= max_files:
            break
        if any(part in skipped_dirs for part in item.relative_to(root).parts):
            continue
        if not item.is_file():
            continue
        size = item.stat().st_size
        if size > max_file_bytes or total_bytes + size > max_total_bytes:
            continue
        relative = item.relative_to(root).as_posix()
        files.append(SandboxInputFile(source=item, target_path=f"/workspace/{relative}"))
        total_bytes += size
    return files


def _sandbox_ttl_seconds() -> int:
    return _int_env("KSADK_SANDBOX_TTL_SECONDS", _int_env("KSADK_SANDBOX_TIMEOUT", 600))


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return int(default)
    try:
        return max(0, int(raw))
    except ValueError:
        return int(default)


def _sandbox_boundary(backend: str) -> str:
    if backend in {"local", "local_process"}:
        return (
            "executes as a local child process; shares local filesystem, env allowlist, and network"
        )
    if backend in {"pod", "pod_process"}:
        return (
            "executes inside the agent pod; shares pod filesystem, env allowlist, "
            "network, and service account"
        )
    return (
        "Sandbox tools execute commands and code only through the configured isolated "
        "sandbox backend; they never expose the host shell."
    )


def get_sandbox_tools() -> list:
    return [
        as_tool(sandbox_status),
        as_tool(run_command),
        as_tool(run_code),
        as_tool(browser_inspect_workspace),
    ]
