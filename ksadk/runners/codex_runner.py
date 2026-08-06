"""CodexRunner — 把 codex runtime(CodexRuntime,RuntimeAdapter)适配成 BaseRunner。

让 codex 项目能像 ADK/LangGraph 一样经 `ksadk web` 起 hosted UI:``create_runner``
返回本 runner → ``run_server`` 走 ``create_runtime_app`` → /run_sse 与 AGUI 都走它。

适配方向:``CodexRuntime.stream`` 产出 ``RuntimeEvent``(TEXT_DELTA/RUN_COMPLETED/…),
本 runner 反向投射成 ADK-style 扁平 dict(``/run_sse`` 期望的 type 词表
text/thinking/interrupt/final),与 ``ADKRunner.stream`` 产出形状一致。AGUI 路径经
``RunnerRuntimeAdapter`` 把本 runner 包回 ``RuntimeAdapter``(``_chunk_to_event`` 已覆盖
全部 type),无需新代码。

codex thread 是 ephemeral(``ksadk/codex/client.py`` ``ephemeral=True``),不支持 resume。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, AsyncIterator, Dict

from ksadk.codex.client import AsyncCodexClient
from ksadk.codex.runtime import CodexRuntime
from ksadk.events.runtime_event import EventType
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import RunHandle, StartRequest


class CodexRunner(BaseRunner):
    """codex 的 BaseRunner 适配。"""

    def __init__(self, detection_result: Any, project_dir: str) -> None:
        super().__init__(detection_result, project_dir)
        # opt-in 代理(env KSADK_CODEX_USE_PROXY=1)在 client 构造时自动注入 provider
        self._client = AsyncCodexClient()
        self._runtime = CodexRuntime(self._client, sandbox_read_only=True)
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

    async def _stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        prompt = str(input_data.get("input") or "")
        session_id = str(input_data.get("session_id") or uuid.uuid4().hex)
        # model:本轮请求优先,fallback 到 yaml(raw_config);prompt:codex 的 base_instructions
        raw = getattr(self.detection_result, "raw_config", None) or {}
        model = input_data.get("model") or raw.get("model")
        base_instructions = raw.get("prompt")
        if model:
            self.sync_process_model_env(str(model))
        config: Dict[str, Any] = {"sandbox_read_only": True}
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
            accumulated = ""          # TEXT_DELTA 累积(流式增量,前端打字机用)
            completed_text = ""       # 最后一次 TEXT_COMPLETED 的全文(权威,防 delta 丢包)
            final_sent = False
            async for event in self._runtime.stream(handle):
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
                elif et == EventType.RUN_COMPLETED:
                    final_sent = True
                    # final.output 优先用 completed 全文,fallback accumulated(delta 流)
                    yield {"type": "final", "output": completed_text or accumulated}
                elif et == EventType.RUN_FAILED:
                    final_sent = True
                    yield {"type": "final", "output": completed_text or accumulated,
                           "error": payload.get("error", "codex run failed")}
                elif et == EventType.RUN_INTERRUPTED:
                    yield {"type": "interrupt", "reason": "interrupted"}
                elif et == EventType.RUN_CANCELED:
                    yield {"type": "interrupt", "reason": "canceled"}
            if not final_sent:
                yield {"type": "final", "output": completed_text or accumulated}
        finally:
            self._handles.pop(handle.run_id, None)

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
