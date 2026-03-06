"""
DeepAgentsRunner - DeepAgents 框架运行时

DeepAgents 的 create_deep_agent 返回 LangGraph CompiledStateGraph，
因此运行时复用 LangGraphRunner 逻辑，保持原生能力和行为一致。
"""

from ksadk.runners.langgraph_runner import LangGraphRunner


class DeepAgentsRunner(LangGraphRunner):
    """DeepAgents 运行时（复用 LangGraphRunner）"""

