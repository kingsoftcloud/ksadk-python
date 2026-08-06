"""LangChainRunner - LangChain 框架运行时(复用 LangGraph 基座,deepagents 同款薄壳)。

新 LangChain(``langchain.agents.create_agent``)返回的就是 LangGraph
``CompiledStateGraph``,因此运行时复用 :class:`LangGraphRunner`,与
``DeepAgentsRunner`` 同款薄壳模式,保持原生能力(工具调用 / 人工审批 HITL /
checkpoint / cancel)和行为一致。

**legacy LangChain 不再支持运行**:``AgentExecutor`` / ``LLMChain`` / LCEL 链
(``prompt | llm | parser``)只有 ``invoke``、没有 ``get_state``/checkpoint,不是
LangGraph 图。本 runner 在 ``load_agent`` 时识别并拒绝,给出迁移指引,而不是等
``stream()`` 时才崩一个莫名其妙的 ``AttributeError``。
"""

from __future__ import annotations

from ksadk.runners.langgraph_runner import LangGraphRunner

_LEGACY_LANGCHAIN_ERROR = (
    "legacy LangChain(AgentExecutor / LLMChain / LCEL 链 `prompt | llm | parser`)"
    "不再支持运行。\n"
    "请迁移到 LangGraph 基座:\n"
    "  • 用 `langchain.agents.create_agent(...)` 定义 agent"
    "(产出 LangGraph 图,自带工具调用 / 人工审批 HITL / checkpoint),或\n"
    "  • 直接用 `langgraph.graph.StateGraph` 编排。\n"
    "迁移后 detector 会识别为 LANGCHAIN/LANGGRAPH,复用 LangGraphRunner 运行。"
)


class LangChainRunner(LangGraphRunner):
    """LangChain 运行时(复用 :class:`LangGraphRunner`)。"""

    def load_agent(self) -> None:
        super().load_agent()
        # 复用 LangGraphRunner 的前提:agent 必须是 LangGraph 图(create_agent /
        # StateGraph 编译产物,具备 get_state/checkpoint)。legacy 链(LCEL /
        # AgentExecutor)只有 invoke、无 get_state —— 在此拒绝,而不是运行时才崩。
        if not hasattr(self._agent, "get_state"):
            raise ValueError(_LEGACY_LANGCHAIN_ERROR)
