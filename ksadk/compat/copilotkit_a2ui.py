"""Compatibility bridge for the pinned CopilotKit and AG-UI A2UI packages.

The currently pinned public packages disagree on the A2UI factory signature.
More importantly, the released LangGraph adapter moves an async model invocation
to a worker thread and calls it with ``asyncio.run``.  Async providers such as
``ChatOpenAI`` retain event-loop-bound clients, so that path can wait forever.

This module keeps CopilotKit's middleware lifecycle and dynamic-tool dispatch,
but owns the small framework glue that invokes the A2UI sub-agent on the active
event loop.  The A2UI wire format, prompt construction, and validation are still
provided by the official AG-UI toolkit.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from importlib import import_module
from typing import Any

try:
    from copilotkit import CopilotKitMiddleware, copilotkit_lg_middleware
    from langchain.tools import ToolRuntime, tool
    from langchain_core.messages import SystemMessage
except ImportError as exc:  # The module itself belongs to the optional AG-UI feature.
    raise ImportError(
        "CopilotKit A2UI requires ksadk[agui] with copilotkit and ag-ui-langgraph installed."
    ) from exc

logger = logging.getLogger(__name__)

_DEFAULT_GENERATION_TIMEOUT_SECONDS = 20.0

# The upstream toolkit's generic default prompt is intentionally exhaustive.
# It is also large enough to exceed the Hosted UI model gateway's reliable tool
# latency. This compact contract covers the v0.9 basic catalog used by ksadk-web
# while leaving applications free to pass their own guidelines explicitly.
_KSADK_HOSTED_UI_GUIDELINES = {
    "generation_guidelines": """\
Return exactly one render_a2ui tool call. Use A2UI v0.9 flat components: every
component has an id and a component field. The root id is root. Use only Text,
Column, Row, Card, List, Button, ChoicePicker, Divider, TextField, CheckBox,
or Slider. Put properties directly on each component, never under type or
properties. Use child for one component id and children for an array of ids.
For example: [{\"id\":\"root\",\"component\":\"Card\",\"child\":\"content\"},
{\"id\":\"content\",\"component\":\"Column\",\"children\":[\"title\"]},
{\"id\":\"title\",\"component\":\"Text\",\"variant\":\"h2\",\"text\":\"Status\"}].
Use only facts present in the conversation and tool results. Do not return
Markdown or prose instead of the tool call.""",
    "design_guidelines": """\
Create one compact, readable surface. Use hierarchy and spacing through Card,
Column, Row, Text, Divider, and ChoicePicker instead of decorative elements.
For status, summarize real values; for risks, show severity and owner; for
choices, use ChoicePicker with a bound data value.""",
}


def _a2ui_toolkit_dependencies() -> tuple[Any, ...]:
    """Resolve the optional toolkit, which currently publishes no type metadata."""
    try:
        toolkit = import_module("ag_ui_a2ui_toolkit")
    except ImportError as exc:
        raise ImportError(
            "CopilotKit A2UI requires ksadk[agui] with ag-ui-a2ui-toolkit installed."
        ) from exc
    names = (
        "RENDER_A2UI_TOOL_DEF",
        "resolve_a2ui_tool_params",
        "prepare_a2ui_request",
        "build_a2ui_envelope",
        "validate_a2ui_components",
        "augment_prompt_with_validation_errors",
        "wrap_error_envelope",
        "MAX_A2UI_ATTEMPTS",
    )
    values = tuple(getattr(toolkit, name, None) for name in names)
    if any(value is None for value in values):
        raise RuntimeError("ag-ui-a2ui-toolkit is missing a required A2UI integration symbol")
    return values


(
    _RENDER_A2UI_TOOL_DEF,
    _resolve_a2ui_tool_params,
    _prepare_a2ui_request,
    _build_a2ui_envelope,
    _validate_a2ui_components,
    _augment_prompt_with_validation_errors,
    _wrap_error_envelope,
    _MAX_A2UI_ATTEMPTS,
) = _a2ui_toolkit_dependencies()


def _generation_timeout_seconds() -> float:
    """Read the operator-owned A2UI deadline, with a conservative default."""
    raw = os.getenv("KSADK_A2UI_GENERATION_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_GENERATION_TIMEOUT_SECONDS
    try:
        return max(1.0, min(float(raw), 120.0))
    except ValueError:
        return _DEFAULT_GENERATION_TIMEOUT_SECONDS


def _tool_call_args(response: Any) -> dict[str, Any] | None:
    for call in getattr(response, "tool_calls", None) or []:
        if not isinstance(call, dict):
            continue
        if call.get("name") != _RENDER_A2UI_TOOL_DEF["function"]["name"]:
            continue
        args = call.get("args")
        return args if isinstance(args, dict) else {}
    return None


async def _invoke_a2ui_subagent(
    model_with_tool: Any,
    *,
    prompt: str,
    messages: list[Any],
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Ask the structured-output model on the same loop as the parent run."""

    async def invoke() -> Any:
        ainvoke = getattr(model_with_tool, "ainvoke", None)
        if callable(ainvoke):
            result = ainvoke([SystemMessage(content=prompt), *messages])
            return await result if inspect.isawaitable(result) else result
        invoke_sync = getattr(model_with_tool, "invoke", None)
        if not callable(invoke_sync):
            raise TypeError("A2UI model must expose ainvoke() or invoke()")
        return await asyncio.to_thread(invoke_sync, [SystemMessage(content=prompt), *messages])

    response = await asyncio.wait_for(invoke(), timeout=timeout_seconds)
    return _tool_call_args(response)


def _attempt_callback(callback: Any, record: dict[str, Any]) -> None:
    if not callable(callback):
        return
    try:
        callback(record)
    except Exception:
        # Diagnostics must not prevent a usable UI surface from being returned.
        logger.debug("A2UI attempt callback failed", exc_info=True)


async def _generate_a2ui_envelope(
    *,
    config: dict[str, Any],
    state: dict[str, Any],
    messages: list[Any],
    intent: str,
    target_surface_id: str | None,
    changes: str | None,
) -> str:
    """Run the official validate/retry contract without crossing event loops."""

    prep = _prepare_a2ui_request(
        intent=intent,
        target_surface_id=target_surface_id,
        changes=changes,
        messages=messages,
        state=state,
        guidelines=config["guidelines"],
    )
    if prep.get("error"):
        return str(_wrap_error_envelope(prep["error"]))

    model = config["model"]
    model_with_tool = model.bind_tools(
        [_RENDER_A2UI_TOOL_DEF],
        tool_choice=_RENDER_A2UI_TOOL_DEF["function"]["name"],
    )
    recovery = config.get("recovery") or {}
    max_attempts = recovery.get("maxAttempts", _MAX_A2UI_ATTEMPTS)
    try:
        max_attempts = max(1, min(int(max_attempts), 5))
    except (TypeError, ValueError):
        max_attempts = _MAX_A2UI_ATTEMPTS

    timeout_seconds = _generation_timeout_seconds()
    last_errors: list[dict[str, str]] = []
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        prompt = _augment_prompt_with_validation_errors(prep["prompt"], last_errors)
        try:
            args = await _invoke_a2ui_subagent(
                model_with_tool,
                prompt=prompt,
                messages=messages,
                timeout_seconds=timeout_seconds,
            )
        except asyncio.TimeoutError:
            record: dict[str, Any] = {
                "attempt": attempt,
                "ok": False,
                "errors": [
                    {
                        "code": "a2ui_generation_timeout",
                        "path": "model",
                        "message": f"A2UI generation exceeded {timeout_seconds:g} seconds",
                    }
                ],
            }
            attempts.append(record)
            _attempt_callback(config.get("on_a2ui_attempt"), record)
            # Retrying an already-cancelled request usually piles up more work;
            # return a visible error instead of leaving the chat in limbo.
            return str(_wrap_error_envelope(record["errors"][0]["message"]))
        except Exception as exc:
            record = {
                "attempt": attempt,
                "ok": False,
                "errors": [
                    {
                        "code": "a2ui_generation_error",
                        "path": "model",
                        "message": str(exc) or type(exc).__name__,
                    }
                ],
            }
            attempts.append(record)
            _attempt_callback(config.get("on_a2ui_attempt"), record)
            return str(_wrap_error_envelope(record["errors"][0]["message"]))

        if not args:
            record = {
                "attempt": attempt,
                "ok": False,
                "errors": [
                    {
                        "code": "empty_components",
                        "path": "components",
                        "message": "Sub-agent did not call render_a2ui",
                    }
                ],
            }
        else:
            components = args.get("components")
            data = args.get("data")
            validation = _validate_a2ui_components(
                components=components if isinstance(components, list) else [],
                data=data if isinstance(data, dict) else {},
                catalog=config.get("catalog"),
            )
            record = {
                "attempt": attempt,
                "ok": bool(validation["valid"]),
                "errors": list(validation["errors"]),
            }
        attempts.append(record)
        _attempt_callback(config.get("on_a2ui_attempt"), record)
        if record["ok"]:
            return str(
                _build_a2ui_envelope(
                    args=args,
                    is_update=prep["is_update"],
                    target_surface_id=target_surface_id,
                    prior=prep["prior"],
                    default_surface_id=config["default_surface_id"],
                    default_catalog_id=config["default_catalog_id"],
                )
            )
        last_errors = record["errors"]

    return str(
        _wrap_error_envelope(f"Failed to generate valid A2UI after {max_attempts} attempt(s)")
    )


def build_ksadk_a2ui_tool(params: dict[str, Any]) -> Any:
    """Build the dynamic LangChain A2UI tool used by the CopilotKit shim."""

    resolved_params = dict(params)
    resolved_params.setdefault("guidelines", _KSADK_HOSTED_UI_GUIDELINES)
    config = _resolve_a2ui_tool_params(resolved_params)

    @tool(config["tool_name"], description=config["tool_description"])
    async def generate_a2ui(
        runtime: ToolRuntime[Any],
        intent: str = "create",
        target_surface_id: str | None = None,
        changes: str | None = None,
    ) -> str:
        state = runtime.state if isinstance(runtime.state, dict) else {}
        messages = list(state.get("messages") or [])[:-1]
        return await _generate_a2ui_envelope(
            config=config,
            state=state,
            messages=messages,
            intent=intent,
            target_surface_id=target_surface_id,
            changes=changes,
        )

    return generate_a2ui


class KsadkCopilotKitMiddleware(CopilotKitMiddleware):
    """Official lifecycle middleware with KSADK's safe A2UI tool executor."""

    def _maybe_build_a2ui_tool(self, request: Any) -> Any | None:
        state = request.state or {}
        if not self._a2ui_inject_decision(state):
            return None

        resolved = self._resolve_a2ui_catalog(state)
        _component_schema, catalog_id = resolved if resolved else (None, None)
        params: dict[str, Any] = {"model": request.model}
        if catalog_id:
            params["default_catalog_id"] = catalog_id

        tool = build_ksadk_a2ui_tool(params)
        existing_names = {getattr(item, "name", None) for item in (request.tools or [])}
        if tool.name in existing_names:
            return None

        thread_key = (
            copilotkit_lg_middleware._current_thread_id()
            or copilotkit_lg_middleware._DEFAULT_THREAD_KEY
        )
        copilotkit_lg_middleware._a2ui_tools_by_thread[thread_key] = tool
        return tool


__all__ = ["KsadkCopilotKitMiddleware", "build_ksadk_a2ui_tool"]
