"""Fixed interpreter for Studio's declarative ``agentkit.bundle/v2`` artifacts.

This adapter deliberately has no source-loader seam: a Studio Bundle contains a
resolved specification and lock files, never Python source.  The first native
surface is OpenAI-compatible model chat; tools, MCP and skills fail before a run
is accepted until their separately attested providers are installed.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from ksadk.events.canonical import (
    ErrorInfo,
    ItemCompleted,
    ItemStarted,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import ContentSnapshot, TextContent
from ksadk.events.identity import (
    stable_event_id,
    stable_item_id,
    stable_part_id,
    stable_scope_id,
)
from ksadk.kernel.contracts import RuntimeCapability, RuntimeCapabilityMatrix
from ksadk.kernel.errors import UnsupportedControlError
from ksadk.runtime.adapter import (
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)
from ksadk.runtime.launch import RuntimeLaunchContext
from ksadk.studio.contracts import ResolvedAgentSpec

_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_BUNDLE_FORMAT = "agentkit.bundle/v2"


class AgentkitBundleRuntime(BaseRuntime):
    runtime_type = "agentkit"

    def native_capabilities(self) -> dict[str, Any]:
        return {"model_chat": True, "tools": False, "durable_resume": False}


class AgentkitBundleRuntimeAdapter(RuntimeAdapter):
    """Run one checked declarative Bundle with no executable user artifact."""

    def __init__(self, context: RuntimeLaunchContext) -> None:
        super().__init__(AgentkitBundleRuntime())
        self._context = context
        self._spec: ResolvedAgentSpec | None = None
        self._requests: dict[str, StartRequest] = {}
        self._cancelled: set[str] = set()

    async def preflight(self) -> None:
        spec = self._load_spec()
        self._validate_spec(spec)

    async def start(self, request: StartRequest) -> RunHandle:
        await self.preflight()
        run_id = str(request.metadata.get("run_id") or uuid.uuid4())
        self._requests[run_id] = request
        return RunHandle(run_id=run_id, session_id=request.session_id, runtime_type="agentkit")

    async def _stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        request = self._requests.get(handle.run_id)
        if request is None:
            raise UnsupportedControlError(f"agentkit run {handle.run_id!r} is not attached")
        spec = self._load_spec()
        framework = "ksadk"
        scope_id = stable_scope_id(framework, "agentkit", handle.run_id)
        run_item_id = stable_item_id(framework, handle.run_id, "$run")
        message_item_id = stable_item_id(framework, handle.run_id, "assistant", 0)
        part_id = stable_part_id(framework, handle.run_id, "assistant", 0)
        now = time.time
        yield RunStarted(
            schema_version=2,
            event_id=stable_event_id(
                framework, scope_id, run_item_id, "run.started", "run", handle.run_id, 0
            ),
            seq=1,
            timestamp=now(),
            run_id=handle.run_id,
            scope_id=scope_id,
            source=SourceRef(framework=framework, metadata={"runtime": "agentkit"}),
            status="running",
        )
        if handle.run_id in self._cancelled:
            yield self._cancel_event(handle, scope_id, run_item_id, 2, now())
            return
        yield ItemStarted(
            schema_version=2,
            event_id=stable_event_id(
                framework,
                scope_id,
                message_item_id,
                "item.started",
                "message",
                handle.run_id,
                0,
            ),
            seq=2,
            timestamp=now(),
            run_id=handle.run_id,
            scope_id=scope_id,
            source=SourceRef(framework=framework, metadata={"runtime": "agentkit"}),
            item_id=message_item_id,
            item_kind="message",
            phase="final_answer",
        )
        try:
            output = await self._complete(spec, request)
        except Exception as exc:
            yield RunFailed(
                schema_version=2,
                event_id=stable_event_id(
                    framework,
                    scope_id,
                    run_item_id,
                    "run.failed",
                    "run",
                    handle.run_id,
                    0,
                ),
                seq=3,
                timestamp=now(),
                run_id=handle.run_id,
                scope_id=scope_id,
                source=SourceRef(framework=framework, metadata={"runtime": "agentkit"}),
                status="failed",
                error=ErrorInfo(
                    code="AGENTKIT_MODEL_REQUEST_FAILED",
                    message=str(exc),
                    source="agentkit",
                    scope_id=scope_id,
                ),
            )
            return
        if handle.run_id in self._cancelled:
            yield self._cancel_event(handle, scope_id, run_item_id, 3, now())
            return
        snapshot = ContentSnapshot(parts=(TextContent(part_id=part_id, text=output),))
        yield ItemCompleted(
            schema_version=2,
            event_id=stable_event_id(
                framework,
                scope_id,
                message_item_id,
                "item.completed",
                part_id,
                handle.run_id,
                0,
            ),
            seq=3,
            timestamp=now(),
            run_id=handle.run_id,
            scope_id=scope_id,
            source=SourceRef(framework=framework, metadata={"runtime": "agentkit"}),
            item_id=message_item_id,
            item_kind="message",
            snapshot=snapshot,
        )
        yield RunCompleted(
            schema_version=2,
            event_id=stable_event_id(
                framework,
                scope_id,
                run_item_id,
                "run.completed",
                "run",
                handle.run_id,
                0,
            ),
            seq=4,
            timestamp=now(),
            run_id=handle.run_id,
            scope_id=scope_id,
            source=SourceRef(framework=framework, metadata={"runtime": "agentkit"}),
            status="completed",
            output_refs=(OutputRef(scope_id=scope_id, item_id=message_item_id, part_id=part_id),),
        )

    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        return self._stream(handle)

    async def cancel(self, handle: RunHandle) -> CancelResult:
        if handle.run_id not in self._requests:
            return CancelResult.NOT_RUNNING
        self._cancelled.add(handle.run_id)
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def resume(
        self, handle: RunHandle, target: ResumeTarget, payload: ResumePayload | None
    ) -> RunHandle:
        raise UnsupportedControlError("agentkit Bundle Runtime has no native resume provider")

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        return CheckpointDescriptor(
            checkpoint_id="",
            invocation_id=handle.run_id,
            capability=CheckpointCapability(
                supported=False,
                granularity="none",
                rollback_scope="none",
                fork_supported=False,
                durable=False,
                shared_across_pods=False,
                reason="agentkit_bundle_no_checkpoint_provider",
            ),
        )

    async def close(self, handle: RunHandle) -> None:
        self._requests.pop(handle.run_id, None)
        self._cancelled.discard(handle.run_id)

    def capabilities(self) -> RuntimeCapabilityMatrix:
        unavailable = RuntimeCapability(
            supported=False, mode="unavailable", reason="agentkit_bundle_provider_unavailable"
        )
        return RuntimeCapabilityMatrix(
            cancel=RuntimeCapability(supported=True, mode="native"),
            pause=unavailable,
            resume=unavailable,
            submit_interaction=unavailable,
            attach=unavailable,
            steer=unavailable,
            inject=unavailable,
            checkpoint=unavailable,
            durable_restore=unavailable,
        )

    def _load_spec(self) -> ResolvedAgentSpec:
        if self._spec is not None:
            return self._spec
        launch = dict(self._context.config)
        if (
            launch.get("framework") != "agentkit"
            or launch.get("bundle") != "resolved-agent-spec.json"
            or launch.get("bundle_format") != _BUNDLE_FORMAT
        ):
            raise ValueError("invalid agentkit Bundle launch descriptor")
        path = self._context.project_dir / "resolved-agent-spec.json"
        if not path.is_file():
            raise ValueError("agentkit Bundle resolved specification is missing")
        self._spec = ResolvedAgentSpec.model_validate(json.loads(path.read_text(encoding="utf-8")))
        return self._spec

    @staticmethod
    def _validate_spec(spec: ResolvedAgentSpec) -> None:
        if spec.capabilities.tools or spec.capabilities.mcp_servers or spec.capabilities.skills:
            raise ValueError("agentkit Bundle runtime has no installed capability provider")
        if (spec.model.wire_api or "chat").lower() != "chat":
            raise ValueError("agentkit Bundle runtime only supports chat model endpoints")
        parsed = urlparse(spec.model.endpoint_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("agentkit Bundle model endpoint must be an HTTP(S) URL")
        ref = spec.model.credential_ref.removeprefix("env://")
        if (
            not spec.model.credential_ref.startswith("env://")
            or not _ENVIRONMENT_NAME.fullmatch(ref)
        ):
            raise ValueError("agentkit Bundle credentialRef must be an env:// reference")
        if not os.environ.get(ref):
            raise ValueError("agentkit Bundle model credential is not configured")

    async def _complete(self, spec: ResolvedAgentSpec, request: StartRequest) -> str:
        completion = self._context.services.agentkit_completion
        payload = {
            "model": spec.model.model,
            "messages": self._messages(spec, request.input),
            "temperature": spec.model.parameters.temperature,
            "max_tokens": spec.model.parameters.max_tokens,
        }
        if spec.model.parameters.top_p is not None:
            payload["top_p"] = spec.model.parameters.top_p
        if completion is not None:
            result = completion(spec.model, payload)
            result = await result if inspect.isawaitable(result) else result
            if not isinstance(result, str):
                raise TypeError("agentkit completion provider must return text")
            return result
        credential_name = spec.model.credential_ref.removeprefix("env://")
        async with httpx.AsyncClient(
            timeout=spec.execution.timeout_seconds, follow_redirects=False
        ) as client:
            response = await client.post(
                spec.model.endpoint_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {os.environ[credential_name]}",
                    "Content-Type": "application/json",
                },
            )
        response.raise_for_status()
        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("agentkit model response has no chat message content") from exc
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        raise ValueError("agentkit model response content is unsupported")

    @staticmethod
    def _messages(spec: ResolvedAgentSpec, input_value: Any) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        system = "\n\n".join(
            part for part in (spec.instructions.system, spec.instructions.task) if part
        )
        if system:
            messages.append({"role": "system", "content": system})
        content = (
            input_value
            if isinstance(input_value, str)
            else json.dumps(input_value, ensure_ascii=False)
        )
        messages.append({"role": "user", "content": content})
        return messages

    @staticmethod
    def _cancel_event(
        handle: RunHandle, scope_id: str, item_id: str, seq: int, timestamp: float
    ) -> RunCanceled:
        return RunCanceled(
            schema_version=2,
            event_id=stable_event_id(
                "ksadk", scope_id, item_id, "run.canceled", "run", handle.run_id, 0
            ),
            seq=seq,
            timestamp=timestamp,
            run_id=handle.run_id,
            scope_id=scope_id,
            source=SourceRef(framework="ksadk", metadata={"runtime": "agentkit"}),
            status="canceled",
            reason="cancel_requested",
        )


__all__ = ["AgentkitBundleRuntime", "AgentkitBundleRuntimeAdapter"]
