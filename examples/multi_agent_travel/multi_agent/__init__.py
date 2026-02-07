"""
Multi-Agent 旅行规划系统

基于 LangGraph 的多智能体协作示例，展示：
- 主智能体调度 + 子智能体专业化分工
- 敏感操作的权限中断与授权流程
- 状态栈管理实现智能体间的可委派、可返回
"""

from multi_agent.agent import graph

__all__ = ["graph"]
