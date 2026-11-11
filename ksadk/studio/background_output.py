"""Read public process output through an issued, owning-run reference.

The API never accepts a PID, path or session identity and never starts/stops a
process. The canonical tool receipt remains a launcher/query RPC receipt. The
read entry (backend) is resolved from the canonical launch receipt via the
BackgroundCommandLocator — from durable authorized sources, never from the HTTP
request, the current environment, or a model input.
"""

from __future__ import annotations

import re
from dataclasses import asdict

from ksadk.harness.tool_display import builtin_tool_display, safe_tool_display
from ksadk.runtime_context import tool_execution_scope
from ksadk.sandbox import SandboxError
from ksadk.studio.errors import not_found
from ksadk.studio.native_background_locator import BackgroundCommandLocator
from ksadk.studio.run_documents import _run_studio, document_root
from ksadk.toolsets.sandbox import make_background_session


async def read_background_output(studio, run_id: str, task_id: str) -> dict:
    studio = _run_studio(studio, run_id)
    studio._require_direct_run(run_id)
    run = studio.event_store.get(run_id)
    if not re.fullmatch(r"[a-f0-9]{64}", task_id) or run.runtime_type != "harness":
        raise not_found("background command", task_id)
    native_id = run.runtime_handle.get("run_id") or run.id
    declared = False
    for event in studio.event_store.events(run_id):
        canonical = event.data.get("runtimeEvent") or {}
        source = canonical.get("source") or {}
        metadata = source.get("metadata") or {}
        display = safe_tool_display(metadata.get("tool_display")) or {}
        if (canonical.get("item_kind") == "tool_call"
                and source.get("native_run_id") == native_id
                and metadata.get("engine_kind") == "native"
                and metadata.get("tool_name") in {"run_command", "read_command_output", "stop_command"}
                and display.get("background_task_id") == task_id):
            declared = True
            break
    if not declared:
        raise not_found("background command", task_id)
    # Resolve the actually-started backend from the canonical launch receipt via
    # the durable, authorized locator — not from the HTTP request or the current
    # environment. A record without a provable backend is fail-closed.
    locator = BackgroundCommandLocator(studio)
    resolved = await locator.resolve(run_id, task_id)
    backend = resolved.backend if resolved is not None else None
    if backend is None:
        raise not_found("background command", task_id)
    # The host-declared typed E2B connection (target + credential resolver) is
    # held by the Native product composer. Thread it so the HTTP read uses the
    # same authority as launch; without it E2B reads fail closed (unknown).
    composer = getattr(studio, "_native_product_composer", None)
    declaration = getattr(composer, "_background_declaration", None)
    try:
        with tool_execution_scope(session_id=run.session_id):
            session = make_background_session(
                workspace_root=document_root(studio, run),
                state_root=studio.workspace.root / ".harness-tools" / "background",
                backend=backend,
                declaration=declaration,
            )
        output = session.connect_background_command(task_id).read_output()
    except (SandboxError, OSError, ValueError):
        raise not_found("background command", task_id) from None
    return {"taskId": task_id, "display": builtin_tool_display(
        "read_command_output", {}, {"ok": True, "task_id": task_id, **asdict(output)},
    )}
