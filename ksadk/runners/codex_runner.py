"""CodexRunner — 把 CodexRuntimeAdapter 暂时适配成 BaseRunner。

让 codex 项目能像 ADK/LangGraph 一样经 `ksadk web` 起 hosted UI:``create_runner``
返回本 runner → ``run_server`` 走 ``create_runtime_app`` → /run_sse 与 AGUI 都走它。

适配方向:``CodexRuntimeAdapter.stream`` 产出 ``RuntimeEvent``(TEXT_DELTA/RUN_COMPLETED/…),
本 runner 反向投射成 ADK-style 扁平 dict(``/run_sse`` 期望的 type 词表
text/thinking/interrupt/final),与 ``ADKRunner.stream`` 产出形状一致。AGUI 路径经
``RunnerRuntimeAdapter`` 把本 runner 包回 ``RuntimeAdapter``(``_chunk_to_event`` 已覆盖
全部 type),无需新代码。

codex thread 是 ephemeral(``ksadk/codex/client.py`` ``ephemeral=True``),不支持 resume。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Any, AsyncIterator, Dict

from ksadk.codex.client import AsyncCodexClient
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import RunHandle, StartRequest


def _prompt_with_history(current: str, history: Any) -> str:
    if not isinstance(history, list) or not history:
        return current
    rendered: list[str] = []
    labels = {"user": "用户", "assistant": "助手", "system": "系统"}
    for item in history:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        role = str(item.get("role") or "user").lower()
        rendered.append(f"{labels.get(role, role)}：{content.strip()}")
    if not rendered:
        return current
    return (
        "以下是同一会话的历史消息，仅用于保持上下文：\n\n"
        + "\n\n".join(rendered)
        + f"\n\n当前用户消息：\n{current}"
    )


class CodexRunner(BaseRunner):
    """codex 的 BaseRunner 适配。"""

    def __init__(self, detection_result: Any, project_dir: str) -> None:
        super().__init__(detection_result, project_dir)
        self._proxy_events: SimpleQueue[dict[str, Any]] = SimpleQueue()
        # opt-in 代理(env KSADK_CODEX_USE_PROXY=1)在 client 构造时自动注入 provider
        self._client = AsyncCodexClient(proxy_observer=self._observe_proxy)
        self._runtime = CodexRuntimeAdapter(self._client, sandbox_read_only=True)
        # invocation_id -> RunHandle(stream 期间持有,cancel 用)
        self._handles: dict[str, RunHandle] = {}

    def load_agent(self) -> None:
        # codex 无 root_agent 变量;agent 逻辑由 prompt 承载,无需加载
        self._agent = True

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        output = ""
        usage: dict[str, Any] = {}
        async for chunk in self.stream(input_data):
            if chunk.get("type") == "final":
                output = chunk.get("output", "") or output
                if chunk.get("usage"):
                    usage = chunk["usage"]
        return {"output": output, "usage": usage}

    def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        return self._stream(input_data)

    async def stream_runtime_events(
        self, input_data: Dict[str, Any]
    ) -> AsyncIterator[RuntimeEvent]:
        """Expose Codex's canonical event stream to ``RunnerRuntimeAdapter``.

        ``stream()`` remains the hosted-Web projection.  Runtime consumers use
        this method and therefore never flatten standard events into temporary
        dict chunks only to parse them back into ``RuntimeEvent``.
        """

        prompt = _prompt_with_history(
            str(input_data.get("input") or ""),
            input_data.get("history"),
        )
        session_id = str(input_data.get("session_id") or uuid.uuid4().hex)
        # model:本轮请求优先,fallback 到 yaml(raw_config);prompt:codex 的 base_instructions
        raw = getattr(self.detection_result, "raw_config", None) or {}
        model = input_data.get("model") or raw.get("model")
        base_instructions = raw.get("prompt")
        if model:
            self.sync_process_model_env(str(model))
        config: Dict[str, Any] = {"sandbox_read_only": True}
        config["cwd"] = str(Path(self.project_dir).resolve())
        if base_instructions:
            config["base_instructions"] = str(base_instructions)
        request = StartRequest(
            input=prompt,
            user_id="local",
            session_id=session_id,
            model=str(model) if model else None,
            config=config,
        )
        handle = await self._runtime.start(request)
        self._handles[handle.run_id] = handle
        try:
            for proxy_event in self._drain_proxy_events():
                yield self._proxy_runtime_event(handle, request, proxy_event)
            async for event in self._runtime.stream(handle):
                for proxy_event in self._drain_proxy_events():
                    yield self._proxy_runtime_event(handle, request, proxy_event)
                yield event
        except asyncio.CancelledError:
            await self._runtime.cancel(handle)
            raise
        finally:
            self._handles.pop(handle.run_id, None)

    async def _stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        accumulated = ""          # TEXT_DELTA 累积(流式增量,前端打字机用)
        completed_text = ""       # 最后一次 TEXT_COMPLETED 的全文(权威,防 delta 丢包)
        final_sent = False
        async for event in self.stream_runtime_events(input_data):
            et = event.event_type
            payload = event.payload or {}
            if et == EventType.TEXT_DELTA:
                phase = event.phase or "commentary"
                delta = payload.get("text", "")
                # final_answer 的 delta 当 text 流(前端打字机);commentary 当 thinking
                if phase == "final_answer":
                    accumulated += delta
                    yield {"type": "text", "delta": delta}
                else:
                    yield {"type": "thinking", "delta": delta}
            elif et == EventType.TEXT_COMPLETED:
                # 收尾全文:权威,记下来给 final 用;不当 delta 发(前端已通过 delta 收到)。
                # 只收 final_answer(真正的回复);commentary completed 是思考收尾,忽略。
                phase = event.phase or "final_answer"
                if phase == "final_answer":
                    completed_text = payload.get("text", "")
            elif et == EventType.TOOL_CALL_BEGIN:
                yield {
                    "type": "tool",
                    "status": "started",
                    "call_id": payload.get("call_id", ""),
                    "name": payload.get("name", ""),
                    "args": payload.get("args") or {},
                }
            elif et == EventType.TOOL_CALL_END:
                yield {
                    "type": "tool",
                    "status": "completed",
                    "call_id": payload.get("call_id", ""),
                    "name": payload.get("name", ""),
                    "result": payload.get("result") or {},
                }
            elif et == EventType.USAGE_REPORTED:
                yield {"type": "usage", "usage": dict(payload)}
            elif et == EventType.RUN_PROGRESS:
                native_event = str(payload.get("native_event") or "run.progress")
                yield {
                    "type": "proxy" if native_event.startswith("proxy.") else "progress",
                    "event": native_event,
                    "data": dict(payload.get("native_data") or {}),
                }
            elif et == EventType.RUN_COMPLETED:
                final_sent = True
                # final.output 优先用 completed 全文,fallback accumulated(delta 流)
                yield {
                    "type": "final",
                    "output": completed_text or accumulated,
                    "duration_ms": payload.get("duration_ms"),
                    "started_at": payload.get("started_at"),
                    "completed_at": payload.get("completed_at"),
                    "metrics_source": payload.get("source"),
                }
            elif et == EventType.RUN_FAILED:
                final_sent = True
                yield {
                    "type": "final",
                    "output": completed_text or accumulated,
                    "error": payload.get("error", "codex run failed"),
                }
            elif et == EventType.RUN_INTERRUPTED:
                yield {"type": "interrupt", "reason": "interrupted"}
            elif et == EventType.RUN_CANCELED:
                yield {"type": "interrupt", "reason": "canceled"}
        if not final_sent:
            yield {"type": "final", "output": completed_text or accumulated}

    @staticmethod
    def _proxy_runtime_event(
        handle: RunHandle,
        request: StartRequest,
        proxy_event: dict[str, Any],
    ) -> RuntimeEvent:
        return RuntimeEvent.create(
            EventType.RUN_PROGRESS,
            agent_id=request.agent_id or "agent",
            user_id=request.user_id,
            session_id=request.session_id,
            invocation_id=handle.run_id,
            seq_id=0,
            payload={
                "status": "in_progress",
                "native_event": str(proxy_event.get("event") or "proxy.event"),
                "native_data": dict(proxy_event.get("data") or {}),
            },
        )

    def _observe_proxy(self, event: str, data: dict[str, Any]) -> None:
        self._proxy_events.put({"type": "proxy", "event": event, "data": data})

    def _drain_proxy_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while True:
            try:
                events.append(self._proxy_events.get_nowait())
            except Empty:
                return events

    def request_cancel(self, invocation_id: str) -> str:
        handle = self._handles.get(invocation_id)
        if handle is None:
            return "not_running"
        result: Any
        try:
            result = asyncio.run(self._runtime.cancel(handle))
        except RuntimeError:
            # 已有事件循环(异步上下文):用独立线程跑 cancel,避免嵌套 asyncio.run
            result = asyncio.run(asyncio.to_thread(self._runtime.cancel, handle))
        return str(getattr(result, "value", result))

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:  # noqa: BLE001
            pass
