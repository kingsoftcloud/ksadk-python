"""A2A protocol adapter for evaluation targets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.client.errors import A2AClientError, A2AClientTimeoutError, AgentCardResolutionError
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    StreamResponse,
    TaskState,
)
from google.protobuf.json_format import MessageToDict

from .adapters import TargetAdapterError
from .contracts import (
    EvalCase,
    EvalRunSpec,
    TargetKind,
    TargetRef,
    TargetRun,
    TargetRunStatus,
    TargetSnapshot,
)

_SUPPORTED_BINDINGS = ["JSONRPC", "HTTP+JSON"]
_TERMINAL_FAILURES = {
    TaskState.TASK_STATE_FAILED: ("A2A_TASK_FAILED", "远端 A2A Task 执行失败"),
    TaskState.TASK_STATE_REJECTED: ("A2A_TASK_REJECTED", "远端 A2A Task 被拒绝"),
    TaskState.TASK_STATE_AUTH_REQUIRED: ("A2A_AUTH_REQUIRED", "远端 A2A Task 需要鉴权"),
    TaskState.TASK_STATE_INPUT_REQUIRED: ("A2A_INPUT_REQUIRED", "远端 A2A Task 需要额外输入"),
}


class A2ATargetError(TargetAdapterError):
    """Safe, classified failure while resolving an A2A target."""


@dataclass(frozen=True)
class _CardLocation:
    base_url: str
    relative_path: str | None


@dataclass
class _ResponseCollector:
    state: int = TaskState.TASK_STATE_UNSPECIFIED
    task_id: str | None = None
    context_id: str | None = None
    task_output: str = ""
    status_output: str = ""
    direct_output: str = ""
    artifact_chunks: list[str] = field(default_factory=list)

    def observe(self, response: StreamResponse) -> None:
        if response.task:
            self.task_id = response.task.id or self.task_id
            self.context_id = response.task.context_id or self.context_id
            self.state = int(response.task.status.state)
            self.task_output = _parts_text(
                part
                for artifact in response.task.artifacts
                for part in artifact.parts
            )
            self.status_output = _message_text(response.task.status.message)

        if response.status_update:
            update = response.status_update
            self.task_id = update.task_id or self.task_id
            self.context_id = update.context_id or self.context_id
            self.state = int(update.status.state)
            self.status_output = _message_text(update.status.message) or self.status_output

        if response.artifact_update:
            update = response.artifact_update
            self.task_id = update.task_id or self.task_id
            self.context_id = update.context_id or self.context_id
            text = _parts_text(update.artifact.parts)
            if text:
                # canonical executor 的 replace 快照带 ksadk_output_snapshot 标记,
                # 是权威全文;命中时重置而非继续拼接,避免 delta+快照翻倍。
                if any(
                    dict(part.metadata or {}).get("ksadk_output_snapshot")
                    for part in update.artifact.parts
                ):
                    self.artifact_chunks = [text]
                else:
                    self.artifact_chunks.append(text)

        if response.message:
            self.task_id = response.message.task_id or self.task_id
            self.context_id = response.message.context_id or self.context_id
            self.direct_output = _message_text(response.message) or self.direct_output

    @property
    def output(self) -> str:
        return (
            self.task_output
            or "".join(self.artifact_chunks)
            or self.status_output
            or self.direct_output
        )


@dataclass(frozen=True)
class _TurnResult:
    status: TargetRunStatus
    output: str
    task_id: str | None
    context_id: str | None
    task_state: int
    error_code: str | None = None
    error_message: str | None = None


def _new_http_client(*, headers: dict[str, str], timeout_seconds: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
    )


class A2ATargetAdapter:
    """Execute evaluation cases through the official A2A 1.0 client."""

    kind = TargetKind.A2A

    def __init__(self, *, timeout_seconds: int) -> None:
        self._timeout_seconds = timeout_seconds
        self._card: AgentCard | None = None
        self._headers: dict[str, str] = {}

    async def snapshot(self, target: TargetRef) -> TargetSnapshot:
        if target.kind is not self.kind:
            raise A2ATargetError("A2A_TARGET_KIND_INVALID", "A2A adapter 收到非 A2A target")

        location = _card_location(target.locator)
        headers = _credential_headers(target.credential_ref)
        try:
            async with _new_http_client(
                headers=headers,
                timeout_seconds=self._timeout_seconds,
            ) as http_client:
                resolver = A2ACardResolver(
                    httpx_client=http_client,
                    base_url=location.base_url,
                )
                card = await resolver.get_agent_card(
                    relative_card_path=location.relative_path,
                )
                client = await create_client(
                    agent=card,
                    client_config=ClientConfig(
                        httpx_client=http_client,
                        streaming=True,
                        supported_protocol_bindings=_SUPPORTED_BINDINGS,
                    ),
                )
                await client.close()
        except AgentCardResolutionError as exc:
            raise A2ATargetError(
                "A2A_CARD_UNAVAILABLE",
                "无法读取或校验 A2A Agent Card",
            ) from exc
        except (A2AClientError, httpx.HTTPError, ValueError) as exc:
            raise A2ATargetError(
                "A2A_PROTOCOL_UNAVAILABLE",
                "A2A Agent Card 没有可用的协议接口",
            ) from exc

        digest = _agent_card_digest(card)
        self._card = card
        self._headers = headers
        return TargetSnapshot(
            kind=self.kind,
            entrypoint=target.locator,
            revision_digest=f"sha256:{digest}",
            runtime="a2a/1.0",
            metadata={
                "agentName": card.name,
                "agentVersion": card.version,
                "credentialRef": target.credential_ref,
                "supportedInterfaces": [
                    {
                        "protocolBinding": interface.protocol_binding,
                        "protocolVersion": interface.protocol_version,
                    }
                    for interface in card.supported_interfaces
                ],
            },
        )

    async def run_case(
        self,
        spec: EvalRunSpec,
        case: EvalCase,
        *,
        attempt: int,
    ) -> TargetRun:
        del spec, attempt
        if self._card is None:
            raise RuntimeError("A2A target must be snapshotted before execution")

        started_at = time.perf_counter()
        task_ids: list[str] = []
        context_id: str | None = None
        final_output = ""
        last_state = TaskState.TASK_STATE_UNSPECIFIED

        try:
            async with _new_http_client(
                headers=self._headers,
                timeout_seconds=self._timeout_seconds,
            ) as http_client:
                client = await create_client(
                    agent=self._card,
                    client_config=ClientConfig(
                        httpx_client=http_client,
                        streaming=True,
                        supported_protocol_bindings=_SUPPORTED_BINDINGS,
                    ),
                )
                try:
                    for turn in case.turns:
                        result = await self._run_turn(client, turn.input, context_id)
                        context_id = result.context_id or context_id
                        last_state = result.task_state
                        if result.task_id:
                            task_ids.append(result.task_id)
                        if result.output:
                            final_output = result.output
                        if result.status != TargetRunStatus.PASSED:
                            return _target_run(
                                result.status,
                                started_at=started_at,
                                output=final_output,
                                task_ids=task_ids,
                                context_id=context_id,
                                task_state=last_state,
                                error_code=result.error_code,
                                error_message=result.error_message,
                            )
                finally:
                    await client.close()
        except A2AClientTimeoutError:
            return _target_run(
                TargetRunStatus.ERROR,
                started_at=started_at,
                task_ids=task_ids,
                context_id=context_id,
                task_state=last_state,
                error_code="A2A_NETWORK_TIMEOUT",
                error_message="A2A 请求超时",
            )
        except (A2AClientError, httpx.HTTPError, ValueError):
            return _target_run(
                TargetRunStatus.ERROR,
                started_at=started_at,
                task_ids=task_ids,
                context_id=context_id,
                task_state=last_state,
                error_code="A2A_PROTOCOL_ERROR",
                error_message="A2A 请求或响应不符合协议",
            )

        return _target_run(
            TargetRunStatus.PASSED,
            started_at=started_at,
            output=final_output,
            task_ids=task_ids,
            context_id=context_id,
            task_state=last_state,
        )

    async def _run_turn(self, client: object, text: str, context_id: str | None) -> _TurnResult:
        collector = _ResponseCollector(context_id=context_id)
        request = _build_send_request(text, context_id)
        try:
            async for response in client.send_message(request):  # type: ignore[attr-defined]
                collector.observe(response)
        except asyncio.CancelledError:
            if collector.task_id:
                with suppress(Exception):
                    await client.cancel_task(  # type: ignore[attr-defined]
                        CancelTaskRequest(id=collector.task_id)
                    )
            raise

        status, error_code, error_message = _task_result(collector)
        return _TurnResult(
            status=status,
            output=collector.output,
            task_id=collector.task_id,
            context_id=collector.context_id,
            task_state=collector.state,
            error_code=error_code,
            error_message=error_message,
        )


def _build_send_request(
    text: str,
    context_id: str | None,
) -> SendMessageRequest:
    return SendMessageRequest(
        message=Message(
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
            message_id=f"eval-message-{uuid4().hex}",
            context_id=context_id or "",
        ),
        configuration=SendMessageConfiguration(return_immediately=False),
    )

def _card_location(locator: str) -> _CardLocation:
    parsed = urlsplit(locator)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise A2ATargetError(
            "A2A_URL_INVALID",
            "A2A target 必须是有效的 http 或 https URL",
        )
    if parsed.username or parsed.password or parsed.fragment:
        raise A2ATargetError(
            "A2A_URL_INVALID",
            "A2A target URL 不能包含用户凭据或 fragment",
        )

    marker = "/.well-known/"
    if marker not in parsed.path:
        return _CardLocation(locator.rstrip("/"), None)

    base_path, card_name = parsed.path.split(marker, 1)
    base_url = urlunsplit((parsed.scheme, parsed.netloc, base_path.rstrip("/"), "", ""))
    relative_path = f".well-known/{card_name}"
    if parsed.query:
        relative_path = f"{relative_path}?{parsed.query}"
    return _CardLocation(base_url, relative_path)


def _credential_headers(reference: str | None) -> dict[str, str]:
    if not reference:
        return {}
    if not reference.startswith("env://"):
        raise A2ATargetError(
            "A2A_CREDENTIAL_REF_UNSUPPORTED",
            "本地 A2A 评测当前只支持 env:// 鉴权引用",
        )

    name = reference.removeprefix("env://")
    if not name or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for char in name):
        raise A2ATargetError(
            "A2A_CREDENTIAL_REF_INVALID",
            "A2A env:// 鉴权引用格式无效",
        )
    token = os.environ.get(name)
    if not token:
        raise A2ATargetError(
            "A2A_CREDENTIAL_NOT_FOUND",
            "A2A 鉴权引用尚未配置",
        )
    if len(token) > 16_384 or token != token.strip() or any(ord(char) < 32 for char in token):
        raise A2ATargetError(
            "A2A_CREDENTIAL_INVALID",
            "A2A 鉴权值格式无效",
        )
    return {"Authorization": f"Bearer {token}"}


def _agent_card_digest(card: AgentCard) -> str:
    payload = MessageToDict(card, preserving_proto_field_name=False)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _message_text(message: Message) -> str:
    return _parts_text(message.parts) if message else ""


def _parts_text(parts: object) -> str:
    return "".join(part.text for part in parts if part.text)  # type: ignore[attr-defined]


def _task_result(
    collector: _ResponseCollector,
) -> tuple[TargetRunStatus, str | None, str | None]:
    if collector.state == TaskState.TASK_STATE_CANCELED:
        return TargetRunStatus.CANCELLED, "A2A_TASK_CANCELLED", "远端 A2A Task 已取消"
    if collector.state in _TERMINAL_FAILURES:
        code, message = _TERMINAL_FAILURES[collector.state]
        return TargetRunStatus.FAILED, code, message
    if collector.state in {
        TaskState.TASK_STATE_SUBMITTED,
        TaskState.TASK_STATE_WORKING,
    }:
        return (
            TargetRunStatus.ERROR,
            "A2A_TASK_INCOMPLETE",
            "A2A 响应结束时 Task 尚未进入终态",
        )
    if collector.state == TaskState.TASK_STATE_COMPLETED or collector.output:
        return TargetRunStatus.PASSED, None, None
    return TargetRunStatus.UNAVAILABLE, "A2A_OUTPUT_UNAVAILABLE", "A2A 响应未提供文本结果"


def _target_run(
    status: TargetRunStatus,
    *,
    started_at: float,
    output: str = "",
    task_ids: list[str],
    context_id: str | None,
    task_state: int,
    error_code: str | None = None,
    error_message: str | None = None,
) -> TargetRun:
    metadata: dict[str, object] = {"remoteTaskIds": task_ids}
    if context_id:
        metadata["remoteContextId"] = context_id
    if task_state != TaskState.TASK_STATE_UNSPECIFIED:
        metadata["taskState"] = _task_state_name(task_state)
    return TargetRun(
        status=status,
        output=output,
        duration_ms=max(0, round((time.perf_counter() - started_at) * 1000)),
        error_code=error_code,
        error_message=error_message,
        metadata=metadata,
    )


def _task_state_name(state: int) -> str:
    try:
        return TaskState.Name(state)
    except ValueError:
        return str(state)
