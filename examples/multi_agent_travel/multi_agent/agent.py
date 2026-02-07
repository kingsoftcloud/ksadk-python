"""
Multi-Agent 旅行规划系统 - 适配 KsADK + 金山云模型

核心特性：
1. 主智能体 + 子智能体（航班/酒店）的分工协作
2. 敏感工具的权限中断与授权流程
3. agent_stack 实现可委派、可返回的上下文管理

运行方式：
    cd examples/multi_agent_travel
    ksadk run .
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Literal, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver


# ----------------------------
# 1) State: 全局记忆体
# ----------------------------
class TravelState(dict):
    """
    旅行系统的全局状态

    字段说明：
    - messages: 对话历史
    - user_info: 用户信息（姓名、偏好等）
    - agent_stack: 智能体调用栈（实现委派和返回）
    - pending_action: 待授权的敏感操作
    """
    messages: List[Any]
    user_info: Dict[str, Any]
    agent_stack: List[Literal["main", "flights", "hotels", "cars"]]
    pending_action: Optional[Dict[str, Any]]


# ----------------------------
# 2) 工具：安全 vs 敏感
# ----------------------------
@tool
def search_flights(origin: str, destination: str, date: str) -> Dict[str, Any]:
    """安全工具：查询航班（只读操作）

    Args:
        origin: 出发城市
        destination: 目的地城市
        date: 出发日期，格式 YYYY-MM-DD

    Returns:
        航班查询结果，包含可选航班列表
    """
    # 模拟数据（真实场景接供应商 API）
    return {
        "origin": origin,
        "destination": destination,
        "date": date,
        "options": [
            {"flight_no": "MU5201", "depart": "08:30", "arrive": "11:20", "price": 1580},
            {"flight_no": "CZ3157", "depart": "14:15", "arrive": "17:05", "price": 1680},
            {"flight_no": "HU7604", "depart": "19:45", "arrive": "22:35", "price": 1520},
        ],
    }


@tool
def search_hotels(city: str, checkin: str, checkout: str) -> Dict[str, Any]:
    """安全工具：查询酒店（只读操作）

    Args:
        city: 城市名称
        checkin: 入住日期，格式 YYYY-MM-DD
        checkout: 退房日期，格式 YYYY-MM-DD

    Returns:
        酒店查询结果，包含可选酒店列表
    """
    return {
        "city": city,
        "checkin": checkin,
        "checkout": checkout,
        "options": [
            {"hotel": "北京国贸大酒店", "price_per_night": 1280, "refundable": True},
            {"hotel": "北京希尔顿酒店", "price_per_night": 980, "refundable": False},
            {"hotel": "北京如家快捷酒店", "price_per_night": 320, "refundable": True},
        ],
    }


@tool
def cancel_hotel(order_id: str) -> Dict[str, Any]:
    """敏感工具：取消酒店订单（修改操作，需要授权）

    Args:
        order_id: 酒店订单号

    Returns:
        取消结果，包含退款信息
    """
    # 模拟取消成功
    return {"order_id": order_id, "status": "cancelled", "refund": 1200}


@tool
def update_ticket(ticket_id: str, new_flight_no: str) -> Dict[str, Any]:
    """敏感工具：改签机票（修改操作，需要授权）

    Args:
        ticket_id: 机票订单号
        new_flight_no: 新航班号

    Returns:
        改签结果
    """
    return {"ticket_id": ticket_id, "new_flight_no": new_flight_no, "status": "rebooked", "fee": 200}


# 工具分类
SAFE_TOOLS = [search_flights, search_hotels]
SENSITIVE_TOOLS = [cancel_hotel, update_ticket]


# ----------------------------
# 3) 委派信号（路由工具）
# ----------------------------
@tool
def ToFlightAssistant() -> str:
    """委派到航班助手"""
    return "route:flights"


@tool
def ToHotelAssistant() -> str:
    """委派到酒店助手"""
    return "route:hotels"


ROUTING_TOOLS = [ToFlightAssistant, ToHotelAssistant]


@tool
def CompleteOrEscalate(reason: str) -> str:
    """子助手完成任务或向上移交

    Args:
        reason: 完成原因或移交原因

    Returns:
        完成信号
    """
    return f"complete:{reason}"


# ----------------------------
# 4) LLM 配置（适配金山云）
# ----------------------------
def make_llm() -> ChatOpenAI:
    """创建 LLM 实例，使用金山云 OpenAI 兼容接口"""
    return ChatOpenAI(
        model=os.getenv("MODEL_NAME", "deepseek-v3.2"),
        base_url=os.getenv("OPENAI_API_BASE", "http://kspmas.ksyun.com/v1"),
        api_key=os.getenv("OPENAI_API_KEY"),
        temperature=0.2,
    )


# ----------------------------
# 5) Entry Node: 智能体切换上下文生成器
# ----------------------------
def create_entry_node(target: Literal["flights", "hotels"]):
    """创建智能体入口节点

    当主智能体委派任务给子智能体时：
    1. 将目标智能体压入 agent_stack
    2. 生成上下文交代背景和任务
    """
    def _entry(state: TravelState) -> TravelState:
        stack = list(state.get("agent_stack", ["main"]))
        stack.append(target)

        assistant_names = {
            "flights": "航班助手",
            "hotels": "酒店助手",
        }

        background = (
            f"你现在是旅行系统的 {assistant_names.get(target, target)}。\n"
            "你已收到主助手的对话上下文，请直接推进任务。\n"
            "- 如果任务完成，使用 CompleteOrEscalate 工具并说明原因。\n"
            "- 如果需要执行敏感操作（取消/改签），系统会自动触发授权流程。\n"
        )

        new_messages = list(state.get("messages", []))
        new_messages.append(SystemMessage(content=background))

        return {
            **state,
            "messages": new_messages,
            "agent_stack": stack,
        }

    return _entry


# ----------------------------
# 6) 各智能体节点
# ----------------------------
def main_assistant(state: TravelState) -> TravelState:
    """主智能体：调度中心"""
    llm = make_llm().bind_tools(ROUTING_TOOLS)

    system_prompt = SystemMessage(
        content=(
            "你是旅行规划系统的主助手（调度中心）。\n\n"
            "职责：\n"
            "1. 识别用户意图：航班查询/改签、酒店预订/取消等\n"
            "2. 简单问题直接回答（如政策咨询）\n"
            "3. 需要专业处理时委派到对应子助手：\n"
            "   - 航班相关 → ToFlightAssistant\n"
            "   - 酒店相关 → ToHotelAssistant\n\n"
            "注意：\n"
            "- 涉及修改操作时，明确告知用户需要确认\n"
            "- 保持友好、专业的服务态度\n"
        )
    )

    messages = [system_prompt] + state.get("messages", [])
    response = llm.invoke(messages)

    new_messages = list(state.get("messages", []))
    new_messages.append(response)

    return {**state, "messages": new_messages}


def flight_assistant(state: TravelState) -> TravelState:
    """航班助手：专注航班查询和改签"""
    llm = make_llm().bind_tools([*SAFE_TOOLS, *SENSITIVE_TOOLS, CompleteOrEscalate])

    system_prompt = SystemMessage(
        content=(
            "你是航班助手，专注于航班相关服务。\n\n"
            "可用工具：\n"
            "- search_flights: 查询航班（安全，可直接使用）\n"
            "- update_ticket: 改签机票（敏感，需要用户授权）\n\n"
            "流程：\n"
            "1. 查询航班信息\n"
            "2. 如需改签，直接调用 update_ticket（系统会自动请求用户确认）\n"
            "3. 任务完成后，调用 CompleteOrEscalate 返回主助手\n"
        )
    )

    messages = [system_prompt] + state.get("messages", [])
    response = llm.invoke(messages)

    new_messages = list(state.get("messages", []))
    new_messages.append(response)

    # 检查是否调用了敏感工具
    pending = None
    if hasattr(response, "tool_calls") and response.tool_calls:
        for tc in response.tool_calls:
            if tc["name"] == "update_ticket":
                pending = {
                    "tool": tc["name"],
                    "args": tc["args"],
                    "tool_call_id": tc["id"],
                }
                break

    return {**state, "messages": new_messages, "pending_action": pending}


def hotel_assistant(state: TravelState) -> TravelState:
    """酒店助手：专注酒店查询和取消"""
    llm = make_llm().bind_tools([*SAFE_TOOLS, *SENSITIVE_TOOLS, CompleteOrEscalate])

    system_prompt = SystemMessage(
        content=(
            "你是酒店助手，专注于酒店相关服务。\n\n"
            "可用工具：\n"
            "- search_hotels: 查询酒店（安全，可直接使用）\n"
            "- cancel_hotel: 取消订单（敏感，需要用户授权）\n\n"
            "流程：\n"
            "1. 查询酒店信息\n"
            "2. 如需取消，直接调用 cancel_hotel（系统会自动请求用户确认）\n"
            "3. 任务完成后，调用 CompleteOrEscalate 返回主助手\n"
        )
    )

    messages = [system_prompt] + state.get("messages", [])
    response = llm.invoke(messages)

    new_messages = list(state.get("messages", []))
    new_messages.append(response)

    # 检查是否调用了敏感工具
    pending = None
    if hasattr(response, "tool_calls") and response.tool_calls:
        for tc in response.tool_calls:
            if tc["name"] == "cancel_hotel":
                pending = {
                    "tool": tc["name"],
                    "args": tc["args"],
                    "tool_call_id": tc["id"],
                }
                break

    return {**state, "messages": new_messages, "pending_action": pending}


# ----------------------------
# 7) 路由函数
# ----------------------------
def route_from_main(state: TravelState) -> str:
    """主助手的路由决策"""
    messages = state.get("messages", [])
    if not messages:
        return "end"

    last = messages[-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        tool_name = last.tool_calls[0]["name"]
        if tool_name == "ToFlightAssistant":
            return "to_flights"
        if tool_name == "ToHotelAssistant":
            return "to_hotels"

    return "end"


def route_after_child(state: TravelState) -> str:
    """子助手的路由决策"""
    # 1. 如果有待授权操作，先处理授权
    if state.get("pending_action"):
        return "needs_approval"

    # 2. 检查是否调用了 CompleteOrEscalate
    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            for tc in last.tool_calls:
                if tc["name"] == "CompleteOrEscalate":
                    # 返回主助手
                    stack = list(state.get("agent_stack", ["main"]))
                    if len(stack) > 1:
                        stack.pop()
                    state["agent_stack"] = stack
                    return "back_to_main"

    # 3. 继续在当前助手
    return "stay"


def handle_approval(state: TravelState) -> TravelState:
    """处理敏感操作的审批

    注意：这是一个简化实现
    真实场景应该通过 Web UI 或交互式命令行请求用户确认
    这里直接执行并记录日志
    """
    pending = state.get("pending_action")
    if not pending:
        return state

    tool_name = pending["tool"]
    args = pending["args"]

    # 执行敏感工具
    if tool_name == "cancel_hotel":
        result = cancel_hotel.invoke(args)
    elif tool_name == "update_ticket":
        result = update_ticket.invoke(args)
    else:
        result = {"error": f"未知敏感工具: {tool_name}"}

    # 添加工具执行结果
    new_messages = list(state.get("messages", []))
    new_messages.append(
        ToolMessage(
            content=f"✓ 敏感操作已执行: {tool_name}\n结果: {result}",
            tool_call_id=pending["tool_call_id"]
        )
    )

    return {
        **state,
        "messages": new_messages,
        "pending_action": None,
    }


def after_approval_route(state: TravelState) -> str:
    """审批后的路由：返回当前栈顶助手"""
    stack = state.get("agent_stack", ["main"])
    top = stack[-1]

    route_map = {
        "main": "main",
        "flights": "flights",
        "hotels": "hotels",
    }

    return route_map.get(top, "main")


# ----------------------------
# 8) 构建 LangGraph
# ----------------------------
def build_graph():
    """构建多智能体协作图"""
    workflow = StateGraph(TravelState)

    # 添加节点
    workflow.add_node("main", main_assistant)
    workflow.add_node("entry_flights", create_entry_node("flights"))
    workflow.add_node("entry_hotels", create_entry_node("hotels"))
    workflow.add_node("flights", flight_assistant)
    workflow.add_node("hotels", hotel_assistant)
    workflow.add_node("approval", handle_approval)

    # 设置入口
    workflow.set_entry_point("main")

    # 主助手的路由
    workflow.add_conditional_edges(
        "main",
        route_from_main,
        {
            "to_flights": "entry_flights",
            "to_hotels": "entry_hotels",
            "end": END,
        },
    )

    # 入口节点到子助手
    workflow.add_edge("entry_flights", "flights")
    workflow.add_edge("entry_hotels", "hotels")

    # 子助手的路由
    workflow.add_conditional_edges(
        "flights",
        route_after_child,
        {
            "needs_approval": "approval",
            "back_to_main": "main",
            "stay": "flights",
        },
    )

    workflow.add_conditional_edges(
        "hotels",
        route_after_child,
        {
            "needs_approval": "approval",
            "back_to_main": "main",
            "stay": "hotels",
        },
    )

    # 审批后返回
    workflow.add_conditional_edges(
        "approval",
        after_approval_route,
        {
            "main": "main",
            "flights": "flights",
            "hotels": "hotels",
        },
    )

    # 编译图（添加内存持久化）
    memory = MemorySaver()
    return workflow.compile(checkpointer=memory)


# 导出 graph 供 KsADK 使用
graph = build_graph()


# ----------------------------
# 9) 本地测试入口（可选）
# ----------------------------
if __name__ == "__main__":
    """本地测试运行

    使用方式：
        python -m multi_agent.agent
    """
    import sys

    print("=== Multi-Agent 旅行系统 ===")
    print("输入 'exit' 退出\n")

    # 初始化状态
    config = {"configurable": {"thread_id": "test-session"}}
    initial_state: TravelState = {
        "messages": [],
        "user_info": {"name": "张三", "preferences": {"budget": 2000}},
        "agent_stack": ["main"],
        "pending_action": None,
    }

    while True:
        user_input = input("\n你: ").strip()
        if user_input.lower() == "exit":
            print("再见！")
            break

        if not user_input:
            continue

        # 添加用户消息
        initial_state["messages"].append(HumanMessage(content=user_input))

        # 调用图
        try:
            result = graph.invoke(initial_state, config=config)
            initial_state = result

            # 打印最后一条 AI 消息
            for msg in reversed(result["messages"]):
                if isinstance(msg, AIMessage) and msg.content:
                    print(f"\n助手: {msg.content}")
                    break

        except Exception as e:
            print(f"\n错误: {e}")
            import traceback
            traceback.print_exc()
