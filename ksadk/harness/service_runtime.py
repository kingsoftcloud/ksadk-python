"""KSADK's RuntimeAdapter for a separately deployed Harness engine."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from ssl import create_default_context
from typing import Any
from uuid import uuid4

import httpx

from ksadk.conversations.normalize import canonical_input_content_from_message_content
from ksadk.events.canonical import (
    ApprovalRequest,
    ErrorInfo,
    InteractionRequested,
    ItemCompleted,
    ItemStarted,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunProgress,
    RunStarted,
    RuntimeEvent,
    SourceRef,
    UsageReported,
)
from ksadk.events.content import ContentSnapshot, TextContent
from ksadk.harness.service_presentation import VIEW_HASH, engine_presentation, engine_view_hash
from ksadk.kernel.contracts import RuntimeCapability, RuntimeCapabilityMatrix
from ksadk.kernel.errors import UnsupportedControlError
from ksadk.runtime.adapter import (
    BaseRuntime,
    CancelResult,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)

_PROTOCOL = "agentkit.engine/v1"
_BINDING = "agentkit.engine-service/v1"
_APPROVAL = "agentkit.engine-approval/v1"
_POLICY = "agentkit.native-agent-policy/v1"
_EXECUTION_POLICY = "agentkit.engine-execution-policy/v1"
_CONTEXT = "agentkit.engine-context/v1"
_HOOKS = "agentkit.engine-hooks/v1"
_TOOL_EFFECT = "agentkit.engine-host-tool-effect/v1"
_VERIFICATION = "agentkit.engine-verification/v1"
_HOST_OBSERVATIONS = "run/host-observations"
_RETRYABLE_RPC = {
    "initialize", "run/start", "run/status", "run/events", "run/approve", _HOST_OBSERVATIONS,
}
_LOG = logging.getLogger(__name__)
TurnContext = Callable[[StartRequest, list[dict[str, Any]]], Awaitable[list[str]]]


class _ServiceRestarted(RuntimeError):
    pass


class _ServiceUnavailable(RuntimeError):
    pass


def _session_key(agent_id: str, user_id: str, session_id: str) -> str:
    identity = json.dumps([agent_id, user_id, session_id], ensure_ascii=True)
    return hashlib.sha256(identity.encode("ascii")).hexdigest()


class _ServiceRuntime(BaseRuntime):
    runtime_type = "harness"

    def native_capabilities(self) -> dict[str, Any]:
        return {"cancel": True, "attach": True, "resume": True, "durable_restore": True}


@dataclass(frozen=True)
class ServiceHostTool:
    name: str
    description: str
    parameters: dict[str, Any]
    call: Callable[[dict[str, Any]], Awaitable[Any]] | None
    read_only: bool = False
    deferred: bool = False
    source_name: str | None = None
    effect: Callable[[RunHandle, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name, "description": self.description[:4000],
            "parameters": self.parameters, "readOnly": self.read_only, "deferred": self.deferred,
            **({"effectProtocol": _TOOL_EFFECT} if self.effect is not None else {}),
        }


@dataclass(frozen=True)
class ServiceContextSource:
    definition: dict[str, Any]
    read: Callable[[RunHandle, dict[str, Any]], Awaitable[list[dict[str, Any]]]]


@dataclass(frozen=True)
class ServiceHook:
    definition: dict[str, Any]
    invoke: Callable[[RunHandle, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ServiceGoalVerifier:
    definition: dict[str, Any]
    applies: Callable[[str], bool]
    verify: Callable[[RunHandle, dict[str, Any]], Awaitable[dict[str, Any]]]


class HarnessServiceRuntimeAdapter(RuntimeAdapter):
    """The service owns the ledger, tools and model binding for this deployment."""

    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        instructions: str = "",
        model: str | None = None,
        ca_file: str | None = None,
        durable_approvals: bool = False,
    ) -> None:
        super().__init__(_ServiceRuntime())
        url = httpx.URL(endpoint)
        if (
            not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or (
                url.scheme != "https"
                and (url.scheme != "http" or url.host not in {"localhost", "127.0.0.1", "::1"})
            )
            or len(token) < 24
        ):
            raise ValueError("invalid Harness service endpoint or credential")
        self._endpoint = str(url).rstrip("/")
        self._token = token
        self._instructions = instructions
        self._durable_approvals = durable_approvals
        self._verify = create_default_context(cafile=ca_file) if ca_file else True
        self._resource_binding: str | None = None
        self._bound_scope: dict[str, str] = {}
        self._bound_model = model
        self._load_tools: Callable[[], Awaitable[tuple[ServiceHostTool, ...]]] | None = None
        self._context: TurnContext | None = None
        self._context_sources: dict[str, ServiceContextSource] = {}
        self._hooks: dict[str, ServiceHook] = {}
        self._stop_hook_limit: int | None = None
        self._tool_effects = False
        self._complete: Callable[[RunHandle, dict[str, Any]], Awaitable[None]] | None = None
        self._execution_policy: dict[str, Any] | None = None
        self._verifier: ServiceGoalVerifier | None = None
        self._tool_catalog: dict[str, ServiceHostTool] | None = None
        self._tool_catalog_lock = asyncio.Lock()

    def bind_read_only_workspace(
        self,
        *,
        binding: str,
        model: str,
        load_tools: Callable[[], Awaitable[tuple[ServiceHostTool, ...]]],
        context: TurnContext,
        scope: dict[str, str] | None = None,
        context_sources: tuple[ServiceContextSource, ...] = (),
        hooks: tuple[ServiceHook, ...] = (),
        stop_hook_limit: int | None = None,
        tool_effects: bool = False,
        complete: Callable[[RunHandle, dict[str, Any]], Awaitable[None]] | None = None,
        execution_policy: dict[str, Any] | None = None,
        verifier: ServiceGoalVerifier | None = None,
    ) -> None:
        if self._resource_binding is not None:
            raise RuntimeError("Harness service resources are already bound")
        self._resource_binding = binding
        self._bound_scope = dict(scope or {})
        self._bound_model = model
        self._load_tools = load_tools
        self._context = context
        self._context_sources = {
            source.definition["sourceId"]: source for source in context_sources
        }
        if len(self._context_sources) != len(context_sources):
            raise ValueError("Duplicate Harness context source")
        self._hooks = {hook.definition["hookId"]: hook for hook in hooks}
        if len(self._hooks) != len(hooks):
            raise ValueError("Duplicate Harness hook")
        self._stop_hook_limit = stop_hook_limit
        self._tool_effects = tool_effects
        self._complete = complete
        self._verifier = verifier
        if execution_policy is not None:
            self._execution_policy = {**execution_policy, "contract": _EXECUTION_POLICY}

    def _hook_definitions(self, tools: dict[str, ServiceHostTool]) -> dict[str, dict[str, Any]]:
        aliases: dict[str, list[str]] = {}
        for name, tool in tools.items():
            if tool.source_name is not None:
                aliases.setdefault(tool.source_name, []).append(name)
        result = {}
        for key, hook in self._hooks.items():
            definition = hook.definition
            selected = definition.get("toolNames")
            if selected is not None:
                names = []
                for name in selected:
                    candidates = aliases.get(name, [])
                    if len(candidates) > 1:
                        raise ValueError("Ambiguous Harness Hook tool name")
                    names.append(candidates[0] if candidates else name)
                definition = {**definition, "toolNames": names}
            result[key] = definition
        return result

    async def _tools(self) -> dict[str, ServiceHostTool]:
        async with self._tool_catalog_lock:
            if self._tool_catalog is None:
                tools = await self._load_tools() if self._load_tools is not None else ()
                self._tool_catalog = {tool.name: tool for tool in tools}
                if len(self._tool_catalog) != len(tools):
                    self._tool_catalog = None
                    raise ValueError("duplicate Harness service resource name")
            return self._tool_catalog

    def _http(self, *, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"Authorization": "Bearer " + self._token},
            timeout=timeout, trust_env=False, verify=self._verify, follow_redirects=False,
        )

    async def _rpc(self, session: str, method: str, **params: Any) -> dict[str, Any]:
        try:
            return await self._exchange(session, method, params)
        except _ServiceRestarted:
            if method == "initialize":
                raise
            await self._initialize(session)
            return await self._exchange(session, method, params)

    async def _exchange(self, session: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        delay = 0.125
        clock = asyncio.get_running_loop().time
        deadline = clock() + 15
        while True:
            try:
                return await self._request(session, method, params)
            except (
                httpx.NetworkError,
                httpx.RemoteProtocolError,
                _ServiceUnavailable,
            ):
                if method not in _RETRYABLE_RPC or clock() + delay >= deadline:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2)

    async def _request(self, session: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = uuid4().hex
        async with self._http(timeout=35) as client:
            response = await client.post(
                f"{self._endpoint}/v1/sessions/{session}/rpc",
                json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            )
        if response.status_code in {502, 503, 504}:
            raise _ServiceUnavailable(f"Harness service HTTP {response.status_code}")
        if response.status_code in {404, 409}:
            try:
                error_body = response.json()
            except ValueError:
                error_body = None
            if isinstance(error_body, dict) and error_body.get("error") == "session_not_found":
                raise _ServiceRestarted("Harness service session must be reattached")
            if isinstance(error_body, dict) and error_body.get("error") == "request_unavailable":
                raise _ServiceUnavailable("Harness service request was interrupted")
        if not response.is_success:
            raise RuntimeError(f"Harness service HTTP {response.status_code}")
        body = response.json()
        if (
            not isinstance(body, dict)
            or body.get("jsonrpc") != "2.0"
            or body.get("id") != request_id
        ):
            raise RuntimeError("Invalid Harness service response")
        if "error" in body:
            # Remote diagnostics may include provider data. Report only the protocol code.
            error = body["error"]
            code = error.get("code") if isinstance(error, dict) else None
            if code == -32010:
                raise _ServiceRestarted("Harness service session must be reattached")
            raise RuntimeError(f"Harness service RPC {code if isinstance(code, int) else 'error'}")
        if not isinstance(body.get("result"), dict):
            raise RuntimeError("Invalid Harness service result")
        return body["result"]

    async def view_html(self, sha256: str) -> bytes | None:
        if not VIEW_HASH.fullmatch(sha256):
            return None
        headers = {"Authorization": "Bearer " + self._token}
        async with (
            httpx.AsyncClient(
                headers=headers,
                timeout=5,
                trust_env=False,
                verify=self._verify,
            ) as client,
            client.stream("GET", f"{self._endpoint}/v1/views/{sha256}") as response,
        ):
            if response.status_code == 404:
                return None
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 1024 * 1024:
                    raise RuntimeError("Harness view exceeded the size limit")
        if hashlib.sha256(content).hexdigest() != sha256:
            raise RuntimeError("Harness view hash mismatch")
        return bytes(content)

    async def _initialize(self, session: str) -> dict[str, Any]:
        hello = await self._rpc(
            session,
            "initialize",
            protocolVersions=[_PROTOCOL],
            host={"name": "ksadk-python"},
            services={
                "durableApprovals": self._durable_approvals,
                "tools": self._load_tools is not None,
                **({"context": _CONTEXT} if self._context_sources else {}),
                **({"hooks": _HOOKS} if self._hooks else {}),
                **({"toolEffects": _TOOL_EFFECT} if self._tool_effects else {}),
                **({"verification": _VERIFICATION} if self._verifier else {}),
            },
        )
        if hello.get("protocolVersion") != _PROTOCOL:
            raise RuntimeError("Incompatible Harness service protocol")
        if (
            self._durable_approvals
            and (hello.get("capabilities") or {}).get("approvals") != "durable"
        ):
            raise RuntimeError("Harness service does not support durable approvals")
        capabilities = hello.get("capabilities") or {}
        if self._load_tools is not None and (
            capabilities.get("toolPolicy") != _POLICY
            or capabilities.get("hostTools") is not True
            or capabilities.get("scopedHostRequests") is not True
        ):
            raise RuntimeError("Harness service does not support bound KSADK resources")
        if self._context_sources and capabilities.get("hostContext") != _CONTEXT:
            raise RuntimeError("Harness service does not support bound KSADK context")
        if self._hooks and capabilities.get("hostHooks") != _HOOKS:
            raise RuntimeError("Harness service does not support bound KSADK hooks")
        if self._tool_effects and capabilities.get("hostToolEffects") != _TOOL_EFFECT:
            raise RuntimeError("Harness service does not support bound KSADK effects")
        if self._verifier and capabilities.get("hostVerification") != _VERIFICATION:
            raise RuntimeError("Harness service does not support bound KSADK goal verification")
        if self._complete is not None and _HOST_OBSERVATIONS not in capabilities.get("methods", []):
            raise RuntimeError("Harness service does not support completed-Turn host observations")
        if (
            self._execution_policy is not None
            and capabilities.get("executionPolicy") != _EXECUTION_POLICY
        ):
            raise RuntimeError("Harness service does not support bound KSADK execution policy")
        return hello

    async def preflight(self) -> None:
        async with httpx.AsyncClient(timeout=5, trust_env=False, verify=self._verify) as client:
            response = await client.get(f"{self._endpoint}/healthz")
        health = response.json() if response.status_code == 200 else {}
        if not isinstance(health, dict) or health.get("contract") != _BINDING:
            raise RuntimeError("Harness service is unavailable or incompatible")
        if self._resource_binding is not None:
            capabilities = health.get("capabilities") or {}
            if (
                not isinstance(capabilities, dict)
                or capabilities.get("scopedHostRequests") is not True
                or capabilities.get("toolPolicy") != _POLICY
            ):
                raise RuntimeError("Harness service does not support bound KSADK resources")
            if self._context_sources and capabilities.get("hostContext") != _CONTEXT:
                raise RuntimeError("Harness service does not support bound KSADK context")
            if self._hooks and capabilities.get("hostHooks") != _HOOKS:
                raise RuntimeError("Harness service does not support bound KSADK hooks")
            if self._tool_effects and capabilities.get("hostToolEffects") != _TOOL_EFFECT:
                raise RuntimeError("Harness service does not support bound KSADK effects")
            if self._verifier and capabilities.get("hostVerification") != _VERIFICATION:
                raise RuntimeError("Harness service does not support bound KSADK goal verification")
            if (
                self._execution_policy is not None
                and capabilities.get("executionPolicy") != _EXECUTION_POLICY
            ):
                raise RuntimeError("Harness service does not support bound KSADK execution policy")

    def _check_scope(self, *, agent_id: str, user_id: str, session_id: str) -> None:
        if any(
            self._bound_scope.get(key, actual) != actual
            for key, actual in (
                ("agent_id", agent_id), ("user_id", user_id), ("session_id", session_id),
            )
        ):
            raise ValueError("Harness request does not match the bound activation scope")

    def _session(self, handle: RunHandle) -> str:
        ref = handle.native_ref
        agent_id, user_id = ref.get("agent_id"), ref.get("user_id")
        if (
            handle.runtime_type != "harness"
            or ref.get("binding") != _BINDING
            or not all(
                isinstance(value, str) and value for value in (agent_id, user_id, handle.session_id)
            )
        ):
            raise ValueError("Invalid Harness service handle")
        self._check_scope(agent_id=agent_id, user_id=user_id, session_id=handle.session_id)
        return _session_key(agent_id, user_id, handle.session_id)

    async def start(self, request: StartRequest) -> RunHandle:
        conversation = request.conversation_preprocessing()
        rows = list(conversation.messages if conversation else [])
        objective = request.input if isinstance(request.input, str) else ""
        if isinstance(request.input, list):
            if all(isinstance(item, dict) and "role" in item for item in request.input):
                rows = request.input
            else:
                rows = [{"role": "user", "content": request.input}]
        elif not isinstance(request.input, str):
            raise ValueError("Harness service requires text or image input")
        current = next((row for row in reversed(rows) if row.get("role") == "user"), {})
        parts = canonical_input_content_from_message_content(
            current.get("input_content") or current.get("content")
        )
        images = [
            {
                "type": "image_url",
                "image_url": {"url": part["image_url"], "detail": part.get("detail") or "auto"},
            }
            for part in parts
            if part["type"] == "input_image"
        ]
        if not objective.strip():
            objective = "".join(part["text"] for part in parts if part["type"] == "input_text")
        if not objective.strip():
            if not images:
                raise ValueError("Harness service requires text or image input")
            objective = "请查看用户提供的图片并说明其中的内容。"
        if any(not image["image_url"]["url"].startswith("data:image/") for image in images):
            raise ValueError("Harness service requires inline image content")
        if not request.user_id.strip() or not request.session_id.strip():
            raise ValueError("Harness service requires an admitted user and session")
        agent_id = request.agent_id or self._bound_scope.get("agent_id") or "deployment"
        run_id = request.metadata.get("invocation_id") or request.metadata.get("run_id")
        handle = RunHandle(
            run_id=str(run_id or uuid4().hex),
            session_id=request.session_id,
            runtime_type="harness",
            native_ref={"binding": _BINDING, "agent_id": agent_id, "user_id": request.user_id},
        )
        session = self._session(handle)
        hello = await self._initialize(session)
        engine = hello.get("engine") or {}
        selected_model = request.model or self._bound_model
        if selected_model and (engine.get("model") or {}).get("name") != selected_model:
            raise ValueError("Selected model does not match the bound Harness service")
        tool_params: dict[str, Any] = {}
        instructions = self._instructions
        if self._resource_binding is not None:
            handle.native_ref["resource_binding"] = self._resource_binding
            if self._context is not None:
                history = rows or [{"role": "user", "content": objective}]
                sections = await self._context(request, history)
                instructions = "\n\n".join(filter(None, (instructions, *sections)))
            tools = await self._tools()
            reserved = engine.get("reservedToolNames")
            if not isinstance(reserved, list) or any(tool in reserved for tool in tools):
                raise RuntimeError("Harness service resource catalog conflicts with the engine")
            tool_params = {
                "hostTools": [tool.definition() for tool in tools.values()],
                # Only the workspace is read-only. The locked connector's tool
                # allowlist authorizes its own operations independently.
                "toolPolicy": {"contract": _POLICY, "mode": "custom", "rules": {
                    name: "inherit" for name in tools
                }},
            }
            if self._execution_policy is not None:
                tool_params["executionPolicy"] = self._execution_policy
            if self._verifier and self._verifier.applies(objective):
                tool_params["hostVerification"] = {
                    "contract": _VERIFICATION, **self._verifier.definition,
                }
            if self._context_sources:
                tool_params["hostContext"] = {
                    "contract": _CONTEXT,
                    "sources": [item.definition for item in self._context_sources.values()],
                }
            if self._hooks:
                filtered = {
                    name for hook in self._hooks.values()
                    for name in hook.definition.get("toolNames", ())
                }
                if any(
                    tool.source_name in filtered and tool.source_name in reserved
                    for tool in tools.values()
                ):
                    raise ValueError("Harness Hook tool name overlaps an engine tool")
                tool_params["hostHooks"] = {
                    "contract": _HOOKS, "hooks": list(self._hook_definitions(tools).values()),
                    **({"stopContinuationLimit": self._stop_hook_limit}
                       if self._stop_hook_limit is not None else {}),
                }
        if images:
            limits = (hello.get("capabilities") or {}).get("inputImages")
            if (
                not isinstance(limits, dict)
                or type(limits.get("maxCount")) is not int
                or type(limits.get("maxEncodedBytes")) is not int
            ):
                raise ValueError("Harness service or its model does not support image input")
            if (
                len(images) > limits["maxCount"]
                or sum(len(image["image_url"]["url"].encode("utf-8")) for image in images)
                > limits["maxEncodedBytes"]
            ):
                raise ValueError("Harness service image input exceeded its limits")
        earlier = [
            {"role": row["role"], "text": row["content"]}
            for row in rows
            if row.get("role") in {"user", "assistant"} and isinstance(row.get("content"), str)
        ]
        if earlier and earlier[-1] == {"role": "user", "text": objective}:
            earlier.pop()
        started = await self._rpc(
            session,
            "run/start",
            runId=handle.run_id,
            objective=objective,
            conversation=earlier,
            hostInstructions=instructions,
            **tool_params,
            **({"inputImages": images} if images else {}),
        )
        view_hash = engine_view_hash(started.get("view"))
        if view_hash:
            handle.native_ref["view_sha256"] = view_hash
        return handle

    async def attach(self, handle: RunHandle) -> RunHandle:
        session = self._session(handle)
        await self._initialize(session)
        await self._rpc(session, "run/status", runId=handle.run_id)
        return handle

    async def durable_restore(self, handle: RunHandle) -> RunHandle:
        return await self.attach(handle)

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        if target.kind != "invocation_id" or target.id != handle.run_id:
            raise UnsupportedControlError("Harness service can resume only the original Run")
        session = self._session(await self.attach(handle))
        if payload is not None and payload.kind == "approval_decision":
            data = payload.data
            decision = data.get("decision") if isinstance(data, dict) else None
            if (
                not self._durable_approvals
                or not payload.call_id
                or decision not in {"approve", "reject"}
            ):
                raise UnsupportedControlError(
                    "Harness service requires an explicit approval decision"
                )
            receipt = await self._rpc(
                session,
                "run/approve",
                runId=handle.run_id,
                interactionId=payload.call_id,
                decision="allow" if decision == "approve" else "deny",
            )
            if receipt.get("status") not in {"accepted", "duplicate"}:
                raise UnsupportedControlError("Harness service did not accept the approval")
            return handle
        if payload is not None and (payload.kind != "free_text" or payload.data is not None):
            raise UnsupportedControlError(
                "Harness service cannot append input while resuming a Run"
            )
        state = await self._rpc(session, "run/status", runId=handle.run_id)
        if (
            not state.get("running")
            and not state.get("terminal")
            and not state.get("pendingApproval")
        ):
            await self._rpc(session, "run/resume", runId=handle.run_id)
        return handle

    async def cancel(self, handle: RunHandle) -> CancelResult:
        session = self._session(await self.attach(handle))
        status = await self._rpc(session, "run/status", runId=handle.run_id)
        if status["terminal"]:
            return CancelResult.NOT_RUNNING
        state = await self._rpc(session, "run/cancel", runId=handle.run_id)
        if state.get("phase") == "canceled":
            return CancelResult.INTERRUPTED_ACTIVE_TURN
        return CancelResult.PENDING_CANCEL_RECORDED

    def capabilities(self) -> RuntimeCapabilityMatrix:
        native = RuntimeCapability(supported=True, mode="native")
        controls = {name: native for name in self.runtime.native_capabilities()}
        if self._durable_approvals:
            controls.update(submit_interaction=native, interaction_mode="durable_resume")
        return super().capabilities().model_copy(update=controls)

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        raise UnsupportedControlError("Harness service resumes its own journal")

    async def close(self, handle: RunHandle) -> None:
        # Stream subscriptions own their HTTP requests. Releasing a handle
        # must not cancel the service's independently owned Run.
        self._session(handle)

    async def close_all(self) -> None:
        # The PluginHost activation owns connectors and their cleanup.
        return None

    async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        session = self._session(await self.attach(handle))
        pump = (
            asyncio.create_task(self._serve_host_callbacks(session, handle))
            if self._load_tools is not None else None
        )
        try:
            async for event in self._stream_events(session, handle):
                yield event
        finally:
            if pump is not None:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)

    async def _serve_host_callbacks(self, session: str, handle: RunHandle) -> None:
        base = f"{self._endpoint}/v1/sessions/{session}"
        clock = asyncio.get_running_loop().time
        async with self._http(timeout=10) as client:
            while True:
                try:
                    started = clock()
                    response = await client.get(
                        base + "/host-requests", params={"runId": handle.run_id, "waitMs": 5000}
                    )
                    requests = []
                    if response.is_success:
                        requests = response.json().get("requests", [])
                        for frame in requests:
                            if not isinstance(frame, dict) or not isinstance(frame.get("id"), str):
                                continue
                            if frame.get("method") == "host/readContext":
                                result = await self._execute_host_context(handle, frame)
                            elif frame.get("method") == "host/invokeHook":
                                result = await self._execute_host_hook(handle, frame)
                            elif frame.get("method") == "host/verifyGoal":
                                result = await self._execute_host_verification(handle, frame)
                            else:
                                result = await self._execute_host_tool(handle, frame)
                            await client.post(base + "/host-reply", json={
                                "jsonrpc": "2.0", "id": frame["id"], "result": result,
                            })
                except (httpx.HTTPError, ValueError, AttributeError, TypeError):
                    # The ledger retains the outcome of an interrupted call as
                    # uncertain. Never repeat a claimed callback after a lost reply.
                    pass
                    await asyncio.sleep(0.15)
                    continue
                if not response.is_success or (not requests and clock() - started < 0.15):
                    await asyncio.sleep(0.15)

    async def _execute_host_verification(
        self, handle: RunHandle, frame: dict[str, Any],
    ) -> dict[str, Any]:
        params, expires, verifier = frame.get("params"), frame.get("expiresAt"), self._verifier
        unavailable = {"code": "ksadk_verifier_unavailable"}
        if (
            verifier is None or not isinstance(params, dict)
            or params.get("contract") != _VERIFICATION
            or params.get("definition") != verifier.definition
            or params.get("runId") != handle.run_id
            or not isinstance(params.get("goal"), str)
            or not verifier.applies(params["goal"])
            or not isinstance(params.get("operationKey"), str)
            or not isinstance(params.get("inputDigest"), str)
            or handle.native_ref.get("resource_binding") != self._resource_binding
            or type(expires) is not int or expires <= time.time() * 1000
        ):
            return unavailable
        try:
            async with asyncio.timeout_at(
                asyncio.get_running_loop().time() + (expires / 1000 - time.time())
            ):
                result = await verifier.verify(handle, params)
        except Exception:
            return unavailable
        return {
            **result, "contract": _VERIFICATION, "operationKey": params["operationKey"],
            "inputDigest": params["inputDigest"],
        }

    async def _execute_host_hook(self, handle: RunHandle, frame: dict[str, Any]) -> dict[str, Any]:
        params = frame.get("params")
        expires = frame.get("expiresAt")
        unavailable = {"code": "ksadk_hook_unavailable"}
        if (
            not isinstance(params, dict)
            or params.get("contract") != _HOOKS
            or params.get("runId") != handle.run_id
            or not isinstance(params.get("definition"), dict)
            or not isinstance(params["definition"].get("hookId"), str)
            or not isinstance(params.get("operationKey"), str)
            or not isinstance(params.get("inputDigest"), str)
            or type(expires) is not int
            or expires <= time.time() * 1000
            or handle.native_ref.get("resource_binding") != self._resource_binding
        ):
            return unavailable
        identifier = params["definition"]["hookId"]
        try:
            async with asyncio.timeout_at(
                asyncio.get_running_loop().time() + (expires / 1000 - time.time())
            ):
                tools = await self._tools()
                hook = self._hooks.get(identifier)
                if hook is None:
                    return unavailable
                if self._hook_definitions(tools)[identifier] != params["definition"]:
                    return unavailable
                if expires <= time.time() * 1000:
                    return unavailable
                subject = params["input"]
                call = subject.get("call")
                if call is not None and (tool := tools.get(call["name"])) and tool.source_name:
                    subject = {**subject, "call": {**call, "name": tool.source_name}}
                result = await hook.invoke(handle, {**params, "input": subject})
        except Exception:
            return unavailable
        return {
            "contract": _HOOKS, "operationKey": params["operationKey"],
            "inputDigest": params["inputDigest"], **result,
        }

    async def _execute_host_context(
        self, handle: RunHandle, frame: dict[str, Any],
    ) -> dict[str, Any]:
        params = frame.get("params")
        expires = frame.get("expiresAt")
        unavailable = {"code": "ksadk_context_unavailable"}
        if (
            not isinstance(params, dict)
            or params.get("contract") != _CONTEXT
            or params.get("runId") != handle.run_id
            or not isinstance(params.get("sourceId"), str)
            or type(expires) is not int
            or handle.native_ref.get("resource_binding") != self._resource_binding
        ):
            return unavailable
        source = self._context_sources.get(params["sourceId"])
        if (
            source is None
            or source.definition["bindingDigest"] != params.get("bindingDigest")
            or expires <= time.time() * 1000
        ):
            return unavailable
        try:
            async with asyncio.timeout_at(
                asyncio.get_running_loop().time() + (expires / 1000 - time.time())
            ):
                items = await source.read(handle, params)
        except Exception:
            return unavailable
        return {
            "contract": _CONTEXT, "sourceId": params["sourceId"],
            "bindingDigest": params["bindingDigest"], "items": items,
        }

    async def _execute_host_tool(self, handle: RunHandle, frame: dict[str, Any]) -> dict[str, Any]:
        params = frame.get("params")
        expires = frame.get("expiresAt")
        unavailable = {"status": "error", "content": {"code": "ksadk_resource_unavailable"}}
        if (
            frame.get("method") != "host/callTool"
            or not isinstance(params, dict)
            or params.get("runId") != handle.run_id
            or not isinstance(params.get("callId"), str)
            or not isinstance(params.get("arguments"), dict)
            or not isinstance(params.get("name"), str)
            or type(expires) is not int
            or handle.native_ref.get("resource_binding") != self._resource_binding
        ):
            return unavailable
        try:
            tool = (await self._tools()).get(params["name"])
            if tool is None or expires <= time.time() * 1000:
                return unavailable
            if tool.effect is not None:
                return await self._execute_tool_effect(handle, params, tool, expires)
            if tool.call is None:
                return unavailable
            async with asyncio.timeout_at(
                asyncio.get_running_loop().time() + (expires / 1000 - time.time())
            ):
                content = await tool.call(params["arguments"])
            if len(json.dumps(content, ensure_ascii=False, allow_nan=False).encode()) > 1024 * 1024:
                return {"status": "error", "content": {"code": "ksadk_resource_result_too_large"}}
        except Exception:
            return {"status": "error", "content": {"code": "ksadk_resource_execution_failed"}}
        failed = isinstance(content, dict) and (
            content.get("isError") is True or content.get("ok") is False
        )
        return {"status": "error" if failed else "ok", "content": content}

    async def _execute_tool_effect(
        self, handle: RunHandle, params: dict[str, Any], tool: ServiceHostTool, expires: int,
    ) -> dict[str, Any]:
        effect = params.get("effect")
        if (
            tool.effect is None
            or not isinstance(effect, dict)
            or effect.get("contract") != _TOOL_EFFECT
            or not isinstance(effect.get("operationKey"), str)
            or type(effect.get("approved")) is not bool
            or type(effect.get("canRequestApproval")) is not bool
        ):
            return {"status": "error", "content": {"code": "ksadk_effect_unavailable"}}
        try:
            async with asyncio.timeout_at(
                asyncio.get_running_loop().time() + (expires / 1000 - time.time())
            ):
                result = await tool.effect(handle, params)
            if (
                result.get("status") in {"ok", "error", "approval_required", "uncertain"}
                and len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) <= 1048576
            ):
                return result
        except Exception:
            pass
        return {"status": "uncertain", "content": {"code": "ksadk_effect_outcome_unknown"}}

    async def _stream_events(self, session: str, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        cursor = 0
        while True:
            page = await self._rpc(
                session, "run/events", runId=handle.run_id, cursor=cursor, waitMs=5000, follow=True
            )
            for event in page["events"]:
                if event.get("approvalContract") == _APPROVAL:
                    if not self._durable_approvals:
                        continue
                    state = await self._rpc(session, "run/status", runId=handle.run_id)
                    pending = state.get("pendingApproval") or {}
                    if pending.get("interactionId") != event.get("interactionId"):
                        continue
                for item in _project(handle.run_id, event, handle.native_ref.get("view_sha256")):
                    if self._complete is not None and isinstance(item, RunCompleted):
                        try:
                            async with asyncio.timeout(10):
                                observed = await self._rpc(
                                    session, _HOST_OBSERVATIONS, runId=handle.run_id,
                                )
                                if (
                                    observed.get("contract") == _TOOL_EFFECT
                                    and observed.get("runId") == handle.run_id
                                    and handle.native_ref.get("resource_binding")
                                    == self._resource_binding
                                ):
                                    await self._complete(handle, observed)
                        except Exception:
                            _LOG.warning(
                                "Harness completed-Turn host outcome unknown",
                                extra={"run_id": handle.run_id},
                            )
                    yield item
            if not page["events"] and not page["running"]:
                state = await self._rpc(session, "run/status", runId=handle.run_id)
                if state.get("terminal") or state.get("pendingApproval"):
                    return
                if not state.get("running"):
                    raise RuntimeError("Harness Run stopped before delivering an outcome")
            cursor = page["cursor"]


def harness_service_configured() -> bool:
    return any(
        key in os.environ for key in (
            "KSADK_HARNESS_SERVICE_URL", "KSADK_HARNESS_SERVICE_TOKEN",
            "KSADK_HARNESS_SERVICE_WORKSPACE",
        )
    )


def harness_service_from_env(
    *, instructions: str = "", model: str | None = None,
) -> HarnessServiceRuntimeAdapter | None:
    if not harness_service_configured():
        return None
    endpoint = os.environ.get("KSADK_HARNESS_SERVICE_URL")
    token = os.environ.get("KSADK_HARNESS_SERVICE_TOKEN")
    workspace = os.environ.get("KSADK_HARNESS_SERVICE_WORKSPACE")
    if workspace is not None:
        mount = Path(workspace)
        if not mount.is_absolute() or not mount.is_dir():
            raise ValueError("invalid Harness service workspace mount")
    return HarnessServiceRuntimeAdapter(
        endpoint=endpoint or "",
        token=token or "",
        instructions=instructions,
        model=model,
        ca_file=os.environ.get("KSADK_HARNESS_SERVICE_CA_FILE"),
        durable_approvals=os.environ.get("KSADK_HARNESS_SERVICE_DURABLE_APPROVALS") == "1",
    )


def _project(
    run_id: str,
    event: dict[str, Any],
    view_hash: str | None = None,
) -> list[RuntimeEvent]:
    seq = event["seq"] * 4
    kind = event["kind"]
    scope = run_id
    presentation = engine_presentation(view_hash, event)
    base: dict[str, Any] = {
        # v1 journal frames have no clock. A stable sentinel keeps replay identical.
        "schema_version": 2,
        "seq": seq,
        "timestamp": 0.0,
        "run_id": run_id,
        "scope_id": scope,
        "source": SourceRef(
            framework="ksadk",
            native_run_id=run_id,
            native_cursor=str(event["seq"]),
            metadata={"harness_service_presentation": presentation} if presentation else {},
        ),
    }

    def envelope(offset: int = 0) -> dict[str, Any]:
        return {**base, "seq": seq + offset, "event_id": f"{run_id}:harness:{seq + offset}"}

    if kind == "run.started":
        return [RunStarted(**envelope(), status="running")]
    if kind == "approval.requested" and event.get("approvalContract") == _APPROVAL:
        interaction_id = event["interactionId"]
        tool = event["title"]
        return [
            InteractionRequested(
                **envelope(),
                interaction_id=interaction_id,
                interaction_kind="approval",
                request=ApprovalRequest(
                    call_id=interaction_id,
                    kind=tool,
                    detail={"reason": event["detail"]} if event.get("detail") else None,
                ),
            )
        ]
    if kind == "model.completed" and isinstance(event.get("usage"), dict):
        count = event["usage"]
        input_tokens = count.get("inputTokens") or 0
        output_tokens = count.get("outputTokens") or 0
        events: list[RuntimeEvent] = [
            UsageReported(
                **envelope(),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )
        ]
        if event.get("title") == "initial_plan" and isinstance(event.get("detail"), str):
            events.append(RunProgress(**envelope(1), status="running", message=event["detail"]))
        return events
    if kind != "run.terminal":
        if presentation or kind in {"tool.completed", "model.failure_observed"}:
            return [RunProgress(**envelope(), status="running", message=event.get("title"))]
        return []
    phase, outcome, reason = event.get("title"), event.get("status"), event.get("reason")
    if phase == "canceled":
        return [RunCanceled(**envelope(), status="canceled", reason=reason)]
    if phase == "interrupted":
        return [RunInterrupted(**envelope(), status="interrupted", reason=reason)]
    if (
        phase != "completed" or outcome in {"failed", "budget_exhausted"}
        or event.get("requiredVerification") is True and outcome != "verified"
    ):
        return [
            RunFailed(
                **envelope(),
                status="failed",
                error=ErrorInfo(
                    code=reason or "harness_run_failed", source="runtime", scope_id=scope
                ),
            )
        ]
    text = event.get("detail")
    if not text:
        return [RunCompleted(**envelope(), status="completed", output_refs=())]
    item_id = f"{run_id}:answer"
    snapshot = ContentSnapshot(parts=(TextContent(part_id=f"{item_id}:text", text=text),))
    return [
        ItemStarted(
            **envelope(),
            item_id=item_id,
            item_kind="message",
            phase="final_answer",
            initial=snapshot,
        ),
        ItemCompleted(**envelope(1), item_id=item_id, item_kind="message", snapshot=snapshot),
        RunCompleted(
            **envelope(2),
            status="completed",
            output_refs=(OutputRef(scope_id=scope, item_id=item_id),),
        ),
    ]
