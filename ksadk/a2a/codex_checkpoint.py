"""Save native expert recovery inputs before allowing an outbound invocation."""

import asyncio

from ksadk.a2a.codex import CodexToolCall


class CodexCheckpointUnavailable(RuntimeError):
    def __init__(self, thread_id):
        super().__init__("A2A_CODEX_CHECKPOINT_UNAVAILABLE")
        self.thread_id = thread_id


class CodexCheckpointWriter:
    def __init__(self, *, client, authority, request, experts, recovery=None):
        if request.session_id != authority.session_id:
            raise ValueError("A2A_CODEX_CHECKPOINT_SESSION_CONFLICT")
        self.client, self.authority = client, authority
        self.start_request = request.model_dump(mode="json")
        self.bindings = {
            name: expert.spec.model_dump(mode="json") for name, expert in experts.items()
        }
        self.calls = {
            (call.thread_id, call.turn_id, call.call_id): call
            for call in (recovery.calls if recovery is not None else ())
        }
        self.lock = asyncio.Lock()

    async def before_invoke(self, call):
        try:
            await self.save(call)
        except Exception:
            raise CodexCheckpointUnavailable(call.thread_id) from None

    async def mark_continuation_started(self):
        if not self.calls:
            raise ValueError("A2A_CODEX_RECOVERY_CALL_REQUIRED")
        call = next(iter(self.calls.values()))
        try:
            await self.save(call, continuation_started=True)
        except Exception:
            raise CodexCheckpointUnavailable(call.thread_id) from None

    async def completed_turn(self, event):
        """Commit terminal history before Kernel may publish run.completed."""
        from ksadk.a2a.codex_session_checkpoint import CodexSessionCheckpoint

        thread_id, turn_id = event.source.metadata.get("thread_id"), event.source.native_run_id
        try:
            async with self.lock:
                rollout = await self.client.capture_completed_a2a_rollout(thread_id, turn_id)
                checkpoint = CodexSessionCheckpoint.model_validate({
                    "schema": "codex.a2a.session/v1",
                    **self.authority.runtime_checkpoint_identity(),
                    "run_id": self.authority.run_id,
                    "session_id": self.authority.session_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "start_request": self.start_request,
                    "bindings": self.bindings,
                    "rollout": rollout.model_dump(mode="json"),
                    "terminal_event": event.model_dump(mode="json"),
                })
                await self.authority.save_runtime_checkpoint(
                    checkpoint.model_dump(mode="json", by_alias=True)
                )
        except Exception:
            raise CodexCheckpointUnavailable(thread_id) from None

    async def save(self, call, *, continuation_started=False):
        call = CodexToolCall.model_validate(call)
        if call.tool not in self.bindings:
            raise ValueError("A2A_CODEX_CHECKPOINT_BINDING_UNKNOWN")
        async with self.lock:
            key = (call.thread_id, call.turn_id, call.call_id)
            previous = self.calls.get(key)
            if previous is not None and previous != call:
                raise ValueError("A2A_CODEX_CHECKPOINT_CALL_CONFLICT")
            calls = dict(self.calls)
            calls[key] = call
            rollout = await self.client.capture_a2a_rollout(call)
            for known in calls.values():
                if known.thread_id != rollout.thread_id:
                    raise ValueError("A2A_CODEX_CHECKPOINT_THREAD_CONFLICT")
                rollout.require_call(
                    turn_id=known.turn_id,
                    call_id=known.call_id,
                    tool=known.tool,
                    arguments=known.arguments,
                )
            if continuation_started and any(
                rollout.function_output(known.call_id) is None for known in calls.values()
            ):
                raise ValueError("A2A_CODEX_RECOVERY_OUTPUT_REQUIRED")
            await self.authority.save_runtime_checkpoint(
                {
                    "schema": "codex.a2a.native/v1",
                    **({"continuation_started": True} if continuation_started else {}),
                    **self.authority.runtime_checkpoint_identity(),
                    "run_id": self.authority.run_id,
                    "session_id": self.authority.session_id,
                    "thread_id": call.thread_id,
                    "start_request": self.start_request,
                    "bindings": self.bindings,
                    "calls": [
                        calls[key].model_dump(mode="json", by_alias=True) for key in sorted(calls)
                    ],
                    "rollout": rollout.model_dump(mode="json"),
                }
            )
            self.calls = calls
