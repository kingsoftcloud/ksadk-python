"""CodexRunner 单测:用 fake AsyncCodexClient 不打网络,测 stream 反投射 + invoke + cancel。"""

import asyncio

from ksadk.codex.client import CodexClient
from ksadk.codex.runtime import CodexRuntime
from ksadk.runners.codex_runner import CodexRunner


class _FakeCodex(CodexClient):
    """假 codex 后端:发 commentary delta + final_answer completed,模拟 codex 流。"""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.interrupted: list[str] = []
        self.thread_configs: list = []  # 记录 start_thread 收到的 config(model/base_instructions)
        self._seq = 0

    async def start_thread(self, config=None) -> str:
        self._seq += 1
        tid = f"thread_{self._seq}"
        self.started.append(tid)
        self.thread_configs.append(config or {})
        return tid

    async def resume_thread(self, thread_id, config=None) -> str:
        return thread_id

    def run_turn(self, thread_id, prompt, *, config=None):
        async def gen():
            # 真实 codex 流:item/started(建 phase 上下文) → delta(多个) → completed
            yield {"method": "item/started",
                   "params": {"item": {"id": "rs1", "type": "reasoning",
                                       "phase": "commentary"}}}
            yield {"method": "item/agentMessage/delta",
                   "params": {"delta": "想一下", "item_id": "rs1"}}
            yield {"method": "item/completed",
                   "params": {"item": {"id": "rs1", "type": "reasoning", "phase": "commentary",
                                       "summary": ["想一下"]}}}
            yield {"method": "item/started",
                   "params": {"item": {"id": "m1", "type": "agentMessage",
                                       "phase": "final_answer"}}}
            yield {"method": "item/agentMessage/delta",
                   "params": {"delta": "你好", "item_id": "m1"}}
            yield {"method": "item/completed",
                   "params": {"item": {"id": "m1", "type": "agentMessage",
                                       "phase": "final_answer", "text": "你好"}}}

        return gen()

    async def interrupt_active_turn(self, thread_id) -> bool:
        self.interrupted.append(thread_id)
        return True

    async def close(self) -> None:
        return None


def _make_runner(monkeypatch):
    # 跳过 AsyncCodexClient 真实 SDK 构造(它 lazy import openai_codex),直接注入 fake
    runner = CodexRunner.__new__(CodexRunner)
    runner.detection_result = type("D", (), {"name": "codex-agent", "type": None})()
    runner.project_dir = "."
    runner._agent = None
    runner._client = _FakeCodex()
    runner._runtime = CodexRuntime(runner._client, sandbox_read_only=True)
    runner._handles = {}
    return runner


def test_stream_projects_text_and_thinking():
    runner = _make_runner(None)
    chunks = asyncio.run(_collect(runner.stream({"input": "hi", "session_id": "s1"})))
    types = [c.get("type") for c in chunks]
    # commentary delta -> thinking;final_answer -> text;末尾 RUN_COMPLETED -> final
    assert "thinking" in types
    assert "text" in types
    assert types[-1] == "final"
    final = chunks[-1]
    assert "你好" in final["output"]  # accumulated 文本


def test_invoke_aggregates_final_output():
    runner = _make_runner(None)
    result = asyncio.run(runner.invoke({"input": "hi", "session_id": "s2"}))
    assert "你好" in result["output"]


def test_load_agent_is_noop():
    runner = _make_runner(None)
    runner.load_agent()  # 不抛异常即通过
    assert runner._agent is True


def test_stream_passes_model_and_prompt_to_thread():
    """C:yaml 的 model/prompt 经 raw_config 传给 codex thread(model + base_instructions)。"""
    runner = _make_runner(None)
    # 模拟 detector 从 ksadk.yaml 读出的 raw_config
    runner.detection_result.raw_config = {"model": "glm-5.2", "prompt": "你是编码助手"}
    asyncio.run(_collect(runner.stream({"input": "hi", "session_id": "s9"})))
    cfg = runner._client.thread_configs[-1]
    assert cfg["model"] == "glm-5.2"
    assert cfg["base_instructions"] == "你是编码助手"
    assert cfg["sandbox_read_only"] is True


def test_stream_input_model_overrides_yaml():
    """C:本轮请求的 model 优先于 yaml(raw_config)。"""
    runner = _make_runner(None)
    runner.detection_result.raw_config = {"model": "yaml-model"}
    asyncio.run(_collect(runner.stream({"input": "hi", "session_id": "s10", "model": "glm-5.1"})))
    cfg = runner._client.thread_configs[-1]
    assert cfg["model"] == "glm-5.1"


async def _collect(agen):
    out = []
    async for c in agen:
        out.append(c)
    return out
