"""Isolated one-shot Codex implementation of ``SubagentProvider/v1``.

Each child owns one App Server client, one native thread, and one temporary
``CODEX_HOME``.  The provider deliberately exposes no follow-up or resume
surface: a parent may stream, cancel, interrupt, inspect, and dispose exactly
one bounded read-only turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ksadk.plugins.subagents import (
    ChildHandle,
    SpawnSubagentRequest,
    SubagentEvent,
    SubagentProviderError,
    SubagentResult,
    SubagentStatus,
)

DEFAULT_CODEX_CHILD_PROVIDER_REF = "plugin://io.ksadk.codex-child@1.0.0"
_CAPABILITIES = ("cancel", "interrupt", "streaming")
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _ChildState:
    handle: ChildHandle
    client: Any
    native_thread_id: str
    home: Path
    timeout_seconds: int
    task_text: str
    thread_config: dict[str, Any]
    events: list[SubagentEvent] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task[None] | None = None
    state: str = "accepted"
    reason: str | None = None
    updated_at: datetime = field(default_factory=_now)
    requested_terminal: str | None = None
    result_value: SubagentResult | None = None
    final_text: str = ""
    closed: bool = False


class CodexOneShotSubagentProvider:
    """Run bounded Codex children without sharing process or conversation state."""

    def __init__(
        self,
        *,
        project_dir: str | Path,
        provider_ref: str = DEFAULT_CODEX_CHILD_PROVIDER_REF,
        model: str | None = None,
        base_instructions: str | None = None,
        client_factory: Callable[[Path], Any] | None = None,
    ) -> None:
        self._project_dir = Path(project_dir).resolve()
        self._provider_ref = provider_ref
        self._model = model
        self._base_instructions = base_instructions
        self._client_factory = client_factory
        self._states: dict[str, _ChildState] = {}

    async def describe(self) -> Mapping[str, Any]:
        return {
            "providerRef": self._provider_ref,
            "mode": "one-shot",
            "capabilities": list(_CAPABILITIES),
            "sandbox": "read-only",
            "resumable": False,
        }

    async def available(self) -> bool:
        if self._client_factory is not None:
            return True
        try:
            import openai_codex  # noqa: F401
        except ImportError:
            return False
        return True

    async def spawn(self, request: SpawnSubagentRequest) -> ChildHandle:
        if request.provider_ref != self._provider_ref:
            raise SubagentProviderError(
                "subagent_provider_mismatch",
                "Codex child request does not target this exact provider version",
            )
        if request.policy.background:
            raise SubagentProviderError(
                "codex_child_policy_unsupported",
                "One-shot Codex children do not support detached background execution",
            )
        if request.policy.allowed_tools or request.policy.allowed_permissions:
            raise SubagentProviderError(
                "codex_child_policy_unsupported",
                "Codex child tool and permission allowlists require an enforceable native mapping",
            )

        home = Path(tempfile.mkdtemp(prefix="ksadk-codex-child-"))
        client: Any = None
        try:
            client = self._new_client(home)
            thread_config = self._thread_config()
            native_thread_id = str(await client.start_thread(thread_config))
        except Exception:
            if client is not None:
                await client.close()
            shutil.rmtree(home, ignore_errors=True)
            raise

        handle_id = f"codex-child-{uuid.uuid4().hex}"
        digest = hashlib.sha256(
            json.dumps(
                {
                    "providerRef": self._provider_ref,
                    "mode": "one-shot",
                    "capabilities": _CAPABILITIES,
                    "sandbox": "read-only",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        handle = ChildHandle(
            handle_id=handle_id,
            provider_ref=self._provider_ref,
            parent_session_id=request.parent_session_id,
            parent_run_id=request.parent_run_id,
            child_session_id=native_thread_id,
            child_run_id=f"codex-turn-{uuid.uuid4().hex}",
            depth=request.depth,
            capabilities=_CAPABILITIES,
            capability_digest=f"sha256:{digest}",
            created_at=_now(),
            resumable=False,
        )
        state = _ChildState(
            handle=handle,
            client=client,
            native_thread_id=native_thread_id,
            home=home,
            timeout_seconds=request.policy.timeout_seconds,
            task_text=request.task,
            thread_config=thread_config,
        )
        self._states[handle_id] = state
        await self._append_event(
            state,
            "progress",
            {"state": "accepted"},
            {"threadId": native_thread_id},
        )
        state.task = asyncio.create_task(self._drive(state), name=f"ksadk-{handle_id}")
        return handle

    async def followup(self, handle: ChildHandle, input: Any) -> None:
        del input
        self._state(handle)
        raise SubagentProviderError(
            "codex_child_one_shot",
            "Codex one-shot children do not accept follow-up input",
        )

    async def status(self, handle: ChildHandle) -> SubagentStatus:
        state = self._state(handle)
        return SubagentStatus(
            handle_id=handle.handle_id,
            state=state.state,
            last_seq=len(state.events),
            updated_at=state.updated_at,
            reason=state.reason,
        )

    async def interrupt(self, handle: ChildHandle) -> None:
        await self._stop(self._state(handle), "interrupted")

    async def cancel(self, handle: ChildHandle) -> None:
        await self._stop(self._state(handle), "cancelled")

    def subscribe(self, handle: ChildHandle, *, after_seq: int = 0) -> AsyncIterator[SubagentEvent]:
        if after_seq < 0:
            raise SubagentProviderError(
                "subagent_cursor_invalid", "subagent after_seq cannot be negative"
            )
        state = self._state(handle)
        return self._subscribe(state, after_seq)

    async def result(self, handle: ChildHandle) -> SubagentResult:
        state = self._state(handle)
        if state.task is not None and not state.task.done():
            await asyncio.shield(state.task)
        if state.result_value is None:
            raise SubagentProviderError(
                "codex_child_result_unavailable", "Codex child has no terminal result"
            )
        return state.result_value

    async def dispose(self, handle: ChildHandle) -> None:
        state = self._state(handle)
        if state.state not in _TERMINAL_STATES and state.state != "disposed":
            await self._stop(state, "cancelled")
        if not state.closed:
            state.closed = True
            try:
                await state.client.close()
            finally:
                shutil.rmtree(state.home, ignore_errors=True)
        state.state = "disposed"
        state.updated_at = _now()
        async with state.condition:
            state.condition.notify_all()

    def _new_client(self, home: Path) -> Any:
        if self._client_factory is not None:
            return self._client_factory(home)
        import openai_codex

        from ksadk.codex.client import AsyncCodexClient

        return AsyncCodexClient(openai_codex.CodexConfig(env={"CODEX_HOME": str(home)}))

    def _thread_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "cwd": str(self._project_dir),
            "sandbox_read_only": True,
            "approval_mode": "deny_all",
            "ephemeral": True,
        }
        if self._model:
            config["model"] = self._model
        if self._base_instructions:
            config["base_instructions"] = self._base_instructions
        return config

    def _state(self, handle: ChildHandle) -> _ChildState:
        state = self._states.get(handle.handle_id)
        if state is None or state.handle != handle:
            raise SubagentProviderError(
                "subagent_handle_unknown", "Codex child handle is unknown or was modified"
            )
        return state

    async def _drive(self, state: _ChildState) -> None:
        try:
            await asyncio.wait_for(self._consume_turn(state), timeout=state.timeout_seconds)
        except asyncio.CancelledError:
            terminal = state.requested_terminal or "cancelled"
            await self._finish(state, terminal)
        except asyncio.TimeoutError:
            try:
                await state.client.interrupt_active_turn(state.native_thread_id)
            finally:
                await self._finish(
                    state,
                    "failed",
                    error_code="codex_child_timeout",
                    error_message="Codex child exceeded its parent-owned timeout",
                )
        except Exception as error:
            await self._finish(
                state,
                "failed",
                error_code="codex_child_failed",
                error_message=str(error)[:2048],
            )

    async def _consume_turn(self, state: _ChildState) -> None:
        state.state = "running"
        state.updated_at = _now()
        await self._append_event(
            state,
            "progress",
            {"state": "running"},
            {"threadId": state.native_thread_id},
        )
        async for raw in state.client.run_turn(
            state.native_thread_id,
            state.task_text,
            config=state.thread_config,
        ):
            await self._project_native_event(state, raw)
        await self._finish(state, "succeeded", output=state.final_text)

    async def _project_native_event(self, state: _ChildState, raw: Mapping[str, Any]) -> None:
        method = str(raw.get("method") or "")
        params = raw.get("params")
        if not isinstance(params, Mapping):
            params = {}
        native_ref = {
            key: str(value)
            for key, value in {
                "method": method,
                "threadId": params.get("threadId") or state.native_thread_id,
                "turnId": params.get("turnId") or _mapping_value(params.get("turn"), "id"),
                "itemId": params.get("itemId") or _mapping_value(params.get("item"), "id"),
            }.items()
            if value
        }
        if method in {"turn/started", "thread/tokenUsage/updated"}:
            await self._append_event(state, "progress", {"method": method}, native_ref)
            return
        if method == "item/agentMessage/delta":
            delta = str(params.get("delta") or "")
            state.final_text += delta
            if delta:
                await self._append_event(
                    state, "item", {"type": "agent_message_delta", "text": delta}, native_ref
                )
            return
        if method == "item/completed":
            item = params.get("item")
            item_type = _mapping_value(item, "type") or "item"
            text = _item_text(item)
            if item_type in {"agentMessage", "message"} and text:
                state.final_text = text
            await self._append_event(
                state,
                "item",
                {"type": str(item_type), **({"text": text} if text else {})},
                native_ref,
            )
            return
        if method == "error":
            message = str(params.get("message") or params.get("error") or "Codex error")
            raise RuntimeError(message)

    async def _stop(self, state: _ChildState, terminal: str) -> None:
        if state.state in _TERMINAL_STATES or state.state == "disposed":
            return
        state.requested_terminal = terminal
        try:
            await state.client.interrupt_active_turn(state.native_thread_id)
        except Exception as error:
            # A dead or already-finished native process must not prevent local
            # cancellation and, critically, must not prevent ``dispose`` from
            # closing the client and deleting its isolated CODEX_HOME.
            state.reason = f"native interrupt failed: {error}"[:2048]
        finally:
            if state.task is not None and not state.task.done():
                state.task.cancel()
                await asyncio.gather(state.task, return_exceptions=True)
            if state.result_value is None:
                await self._finish(state, terminal)

    async def _finish(
        self,
        state: _ChildState,
        terminal: str,
        *,
        output: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if state.result_value is not None:
            return
        state.state = terminal
        if error_message is not None:
            state.reason = error_message
        state.updated_at = _now()
        state.result_value = SubagentResult(
            handle_id=state.handle.handle_id,
            state=terminal,
            output=output,
            error_code=error_code,
            error_message=error_message,
        )
        await self._append_event(
            state,
            "terminal",
            {"state": terminal, **({"errorCode": error_code} if error_code else {})},
            {"threadId": state.native_thread_id},
        )

    async def _append_event(
        self,
        state: _ChildState,
        kind: str,
        payload: dict[str, Any],
        native_ref: dict[str, str],
    ) -> None:
        seq = len(state.events) + 1
        state.events.append(
            SubagentEvent(
                handle_id=state.handle.handle_id,
                event_id=f"{state.handle.handle_id}:{seq}",
                seq=seq,
                kind=kind,
                payload=payload,
                native_ref=native_ref,
            )
        )
        state.updated_at = _now()
        async with state.condition:
            state.condition.notify_all()

    async def _subscribe(self, state: _ChildState, after_seq: int) -> AsyncIterator[SubagentEvent]:
        cursor = after_seq
        while True:
            while cursor < len(state.events):
                event = state.events[cursor]
                cursor = event.seq
                yield event
            if state.result_value is not None or state.state == "disposed":
                return
            async with state.condition:
                if cursor >= len(state.events) and state.result_value is None:
                    await state.condition.wait()


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None


def _item_text(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    direct = item.get("text")
    if isinstance(direct, str):
        return direct
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(str(part.get("text") or "") for part in content if isinstance(part, Mapping))


__all__ = ["CodexOneShotSubagentProvider", "DEFAULT_CODEX_CHILD_PROVIDER_REF"]
