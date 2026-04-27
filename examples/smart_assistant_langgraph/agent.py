"""
智能助手 Agent (LangGraph 版本)

一个功能丰富的智能助手，支持多种实用工具：
- 天气查询
- 数学计算
- 网络搜索
- 时间查询
- 笔记管理
- 任务管理

与 ADK 版本功能完全一致
"""

import os
import random
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional, TypedDict

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

# 尝试导入 LangChain MCP 适配器 (langchain-mcp-adapters)
try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    _mcp_supported = True
except ImportError:
    _mcp_supported = False

# 加载环境变量
load_dotenv(Path(__file__).parent / ".env")

# ============== 内存存储 ==============
_notes_storage: list = []
_tasks_storage: list = []


# ============== 工具定义 ==============


@tool
def get_weather(city: str) -> dict:
    """获取指定城市的天气信息

    Args:
        city: 城市名称，如 "北京"、"上海"、"深圳"
    """
    weather_data = {
        "北京": {"condition": "晴朗", "temperature": 25, "humidity": 45, "wind": "东北风3级"},
        "上海": {"condition": "多云", "temperature": 28, "humidity": 65, "wind": "东南风2级"},
        "深圳": {"condition": "阵雨", "temperature": 30, "humidity": 80, "wind": "南风2级"},
        "广州": {"condition": "雷阵雨", "temperature": 31, "humidity": 85, "wind": "西南风3级"},
        "杭州": {"condition": "晴转多云", "temperature": 27, "humidity": 55, "wind": "东风2级"},
        "成都": {"condition": "阴天", "temperature": 22, "humidity": 70, "wind": "微风"},
        "西安": {"condition": "晴朗", "temperature": 26, "humidity": 40, "wind": "西北风2级"},
    }

    if city in weather_data:
        data = weather_data[city]
        return {
            "status": "success",
            "city": city,
            "condition": data["condition"],
            "temperature": f"{data['temperature']}°C",
            "humidity": f"{data['humidity']}%",
            "wind": data["wind"],
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    else:
        return {
            "status": "success",
            "city": city,
            "condition": random.choice(["晴朗", "多云", "阴天", "小雨"]),
            "temperature": f"{random.randint(15, 35)}°C",
            "humidity": f"{random.randint(30, 80)}%",
            "wind": random.choice(["东风2级", "西风3级", "南风2级", "北风3级"]),
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }


@tool
def calculate(expression: str) -> dict:
    """计算数学表达式

    Args:
        expression: 数学表达式，如 "2 + 3 * 4"、"sqrt(16)"、"100 / 5"
    """
    import math

    safe_dict = {
        "abs": abs,
        "round": round,
        "min": min,
        "max": max,
        "sum": sum,
        "pow": pow,
        "sqrt": math.sqrt,
        "sin": math.sin,
        "cos": math.cos,
        "tan": math.tan,
        "log": math.log,
        "log10": math.log10,
        "exp": math.exp,
        "pi": math.pi,
        "e": math.e,
    }

    try:
        expr = expression.replace("×", "*").replace("÷", "/").replace("（", "(").replace("）", ")")
        result = eval(expr, {"__builtins__": {}}, safe_dict)
        return {"status": "success", "expression": expression, "result": result}
    except Exception as e:
        return {"status": "error", "expression": expression, "error": f"计算错误: {str(e)}"}


@tool
def search_web(query: str, num_results: int = 3) -> dict:
    """搜索网络信息（模拟）

    Args:
        query: 搜索关键词
        num_results: 返回结果数量，默认3条
    """
    mock_results = [
        {
            "title": f"关于「{query}」的最新研究报告",
            "snippet": f"这是一篇关于{query}的详细分析文章，包含最新的行业动态和发展趋势...",
            "url": f"https://example.com/article/{hash(query) % 10000}",
        },
        {
            "title": f"{query} - 维基百科",
            "snippet": f"{query}是一个重要的概念，在多个领域都有广泛应用。本文将详细介绍其定义、历史和应用场景...",
            "url": f"https://zh.wikipedia.org/wiki/{query}",
        },
        {
            "title": f"深入理解{query}：从入门到精通",
            "snippet": f"本教程将带你全面了解{query}的核心原理和实践方法，适合初学者和进阶者...",
            "url": f"https://tutorial.example.com/{query}",
        },
        {
            "title": f"{query}行业报告2024",
            "snippet": f"最新发布的{query}行业报告显示，市场规模持续扩大，预计未来几年将保持高速增长...",
            "url": f"https://report.example.com/{query}-2024",
        },
    ]

    return {
        "status": "success",
        "query": query,
        "results": mock_results[:num_results],
        "total_found": len(mock_results),
    }


@tool
def get_current_time(timezone: Optional[str] = None) -> dict:
    """获取当前时间

    Args:
        timezone: 时区，如 "Asia/Shanghai"、"America/New_York"，默认为本地时间
    """
    now = datetime.now()

    return {
        "status": "success",
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y年%m月%d日"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][
            now.weekday()
        ],
        "timestamp": int(now.timestamp()),
        "timezone": timezone or "Asia/Shanghai (本地)",
    }


@tool
def create_note(title: str, content: str, tags: Optional[str] = None) -> dict:
    """创建笔记

    Args:
        title: 笔记标题
        content: 笔记内容
        tags: 标签，用逗号分隔，如 "工作,重要"
    """
    global _notes_storage
    note_id = len(_notes_storage) + 1
    note = {
        "id": note_id,
        "title": title,
        "content": content,
        "tags": tags.split(",") if tags else [],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _notes_storage.append(note)

    return {"status": "success", "message": f"笔记「{title}」创建成功", "note": note}


@tool
def list_notes(tag: Optional[str] = None) -> dict:
    """列出所有笔记

    Args:
        tag: 可选，按标签筛选
    """
    notes = _notes_storage

    if tag:
        notes = [n for n in notes if tag in n.get("tags", [])]

    return {
        "status": "success",
        "total": len(notes),
        "notes": notes,
        "filter": {"tag": tag} if tag else None,
    }


@tool
def create_task(title: str, priority: str = "medium", due_date: Optional[str] = None) -> dict:
    """创建任务

    Args:
        title: 任务标题
        priority: 优先级，可选 "high"、"medium"、"low"
        due_date: 截止日期，格式如 "2024-12-31"
    """
    global _tasks_storage
    task_id = len(_tasks_storage) + 1
    priority_map = {"high": "高", "medium": "中", "low": "低"}

    task = {
        "id": task_id,
        "title": title,
        "priority": priority,
        "priority_display": priority_map.get(priority, "中"),
        "due_date": due_date,
        "status": "pending",
        "status_display": "待完成",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _tasks_storage.append(task)

    return {"status": "success", "message": f"任务「{title}」创建成功", "task": task}


@tool
def list_tasks(status: Optional[str] = None) -> dict:
    """列出所有任务

    Args:
        status: 可选，按状态筛选，可选 "pending"、"completed"
    """
    tasks = _tasks_storage

    if status:
        tasks = [t for t in tasks if t.get("status") == status]

    return {
        "status": "success",
        "total": len(tasks),
        "tasks": tasks,
        "filter": {"status": status} if status else None,
    }


@tool
def complete_task(task_id: int) -> dict:
    """完成任务

    Args:
        task_id: 任务ID
    """
    global _tasks_storage
    for task in _tasks_storage:
        if task["id"] == task_id:
            task["status"] = "completed"
            task["status_display"] = "已完成"
            task["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return {
                "status": "success",
                "message": f"任务「{task['title']}」已标记为完成",
                "task": task,
            }

    return {"status": "error", "message": f"未找到ID为 {task_id} 的任务"}


# ============== MCP 工具支持 (使用 langchain-mcp-adapters) ==============

_mcp_session = None
_mcp_tools = []


async def _init_mcp_tools():
    """初始化 MCP 工具 (使用 langchain-mcp-adapters)"""
    global _mcp_session, _mcp_tools

    if not _mcp_supported:
        print("[MCP] langchain-mcp-adapters 未安装，MCP 功能不可用")
        return []

    api_key = os.getenv("KSC_AIPRO_API_KEY")
    if not api_key:
        print("[MCP] 未配置 KSC_AIPRO_API_KEY，MCP 功能不可用")
        return []

    if _mcp_tools:
        return _mcp_tools

    try:
        from langchain_mcp_adapters.tools import load_mcp_tools

        mcp_url = "https://metaso-ifih3vh.aipro.ksyun.com/api/mcp"
        headers = {"Authorization": f"Bearer {api_key}"}

        # 使用 streamable HTTP 连接 MCP 服务器
        async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 使用 langchain-mcp-adapters 加载工具
                _mcp_tools = await load_mcp_tools(session)
                print(
                    f"[MCP] 成功加载 {len(_mcp_tools)} 个 MCP 工具: {[t.name for t in _mcp_tools]}"
                )
                return _mcp_tools

    except Exception as e:
        import traceback

        print(f"[MCP] 初始化失败: {e}")
        traceback.print_exc()
        return []


def get_mcp_tools() -> list:
    """获取已加载的 MCP 工具列表 (同步接口，供图构建时使用)"""
    return _mcp_tools


# MCP 工具包装函数 (用于 LangGraph ToolNode)
# 注意: 这些是备用的手动包装，如果 load_mcp_tools 成功，可以直接使用返回的工具列表


@tool
async def metaso_search(query: str) -> str:
    """秘塔 AI 搜索 (Metaso Search) - 联网深度搜索最新信息

    Args:
        query: 搜索关键词
    """
    if not _mcp_supported:
        return "错误: MCP 功能不可用，请安装 langchain-mcp-adapters"

    api_key = os.getenv("KSC_AIPRO_API_KEY")
    if not api_key:
        return "错误: 未配置 KSC_AIPRO_API_KEY"

    try:
        mcp_url = "https://metaso-ifih3vh.aipro.ksyun.com/api/mcp"
        headers = {"Authorization": f"Bearer {api_key}"}

        async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 调用 MCP 工具
                response = await session.call_tool(
                    "metaso_web_search", arguments={"q": query, "includeSummary": True}
                )
                result = response.model_dump(exclude_none=True, mode="json")

                # 提取结果文本
                content = result.get("content", [])
                if isinstance(content, list) and len(content) > 0:
                    first_item = content[0]
                    if isinstance(first_item, dict) and "text" in first_item:
                        return first_item["text"]

                return str(result)

    except Exception as e:
        return f"搜索失败: {str(e)}"


@tool
async def metaso_read(url: str) -> str:
    """秘塔网页读取 (Metaso Read) - 读取网页内容

    Args:
        url: 网页 URL
    """
    if not _mcp_supported:
        return "错误: MCP 功能不可用，请安装 langchain-mcp-adapters"

    api_key = os.getenv("KSC_AIPRO_API_KEY")
    if not api_key:
        return "错误: 未配置 KSC_AIPRO_API_KEY"

    try:
        mcp_url = "https://metaso-ifih3vh.aipro.ksyun.com/api/mcp"
        headers = {"Authorization": f"Bearer {api_key}"}

        async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                response = await session.call_tool("metaso_web_reader", arguments={"url": url})
                result = response.model_dump(exclude_none=True, mode="json")

                content = result.get("content", [])
                if isinstance(content, list) and len(content) > 0:
                    first_item = content[0]
                    if isinstance(first_item, dict) and "text" in first_item:
                        return first_item["text"]

                return str(result)

    except Exception as e:
        return f"读取失败: {str(e)}"


@tool
async def metaso_answer(question: str) -> str:
    """秘塔智能问答 (Metaso Answer) - 深度问答服务 (RAG)

    Args:
        question: 用户的问题
    """
    if not _mcp_supported:
        return "错误: MCP 功能不可用，请安装 langchain-mcp-adapters"

    api_key = os.getenv("KSC_AIPRO_API_KEY")
    if not api_key:
        return "错误: 未配置 KSC_AIPRO_API_KEY"

    try:
        mcp_url = "https://metaso-ifih3vh.aipro.ksyun.com/api/mcp"
        headers = {"Authorization": f"Bearer {api_key}"}

        async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                response = await session.call_tool("metaso_chat", arguments={"message": question})
                result = response.model_dump(exclude_none=True, mode="json")

                content = result.get("content", [])
                if isinstance(content, list) and len(content) > 0:
                    first_item = content[0]
                    if isinstance(first_item, dict) and "text" in first_item:
                        return first_item["text"]

                return str(result)

    except Exception as e:
        return f"问答失败: {str(e)}"


# ============== LangGraph 图定义 ==============


class AgentState(TypedDict):
    """Agent 状态"""

    messages: Annotated[list, add_messages]


# 定义工具列表
tools = [
    get_weather,
    calculate,
    search_web,
    get_current_time,
    create_note,
    list_notes,
    create_task,
    list_tasks,
    complete_task,
    metaso_search,
    metaso_read,
    metaso_answer,
]

# 创建 LLM
llm = ChatOpenAI(
    model=os.getenv("MODEL_NAME", "qwen2.5:14b"),
    base_url=os.getenv("OPENAI_API_BASE", "http://localhost:11434/v1"),
    api_key=os.getenv("OPENAI_API_KEY", "ollama"),
    temperature=0.7,
    streaming=True,
)

# 绑定工具到 LLM
llm_with_tools = llm.bind_tools(tools)


def should_continue(state: AgentState) -> str:
    """决定是否继续调用工具"""
    messages = state["messages"]
    last_message = messages[-1]

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END


def call_model(state: AgentState) -> dict:
    """调用模型"""
    messages = state["messages"]

    # 添加系统提示
    system_message = SystemMessage(
        content="""你是一个功能强大的智能助手，可以帮助用户完成多种任务：

1. **天气查询**: 使用 get_weather 工具查询城市天气
2. **数学计算**: 使用 calculate 工具进行数学运算
3. **网络搜索**: 使用 search_web 工具搜索信息
4. **时间查询**: 使用 get_current_time 工具获取当前时间
5. **笔记管理**: 使用 create_note 和 list_notes 工具管理笔记
6. **任务管理**: 使用 create_task、list_tasks 和 complete_task 工具管理任务
7. **AI 搜索**: 使用 metaso_search/read/answer 工具进行深度信息检索和问答 (需要 API Key)

请根据用户的需求选择合适的工具，并用友好的中文回复。
当用户请求需要多个步骤时，请依次调用相关工具完成任务。
回复时请整理工具返回的信息，用清晰易读的格式呈现给用户。
"""
    )

    full_messages = [system_message] + messages
    response = llm_with_tools.invoke(full_messages)

    return {"messages": [response]}


# 创建工具节点
tool_node = ToolNode(tools)

# 创建图
workflow = StateGraph(AgentState)

# 添加节点
workflow.add_node("agent", call_model)
workflow.add_node("tools", tool_node)

# 设置入口
workflow.set_entry_point("agent")

# 添加边
workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
workflow.add_edge("tools", "agent")

# 编译图
graph = workflow.compile()


# ============== 自定义 State 转换 Hook (可选) ==============
#
# 如果你的 LangGraph 图使用自定义 State (而非 messages), 可以定义
# ksadk_prepare_state 函数来控制输入到 State 的转换。
# 当此函数存在时, ksadk 会跳过默认的 messages 转换逻辑, 直接使用你的返回值。
#
# def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
#     """自定义输入到 State 的转换
#
#     Args:
#         payload: 简化输入, 格式为 {"input": "用户消息"}
#         session_context: 会话上下文, 包含:
#             - session_id: 会话 ID
#             - history: 历史消息列表
#             - kb_context: 知识库上下文
#             - memory_context: 长期记忆上下文
#             - platform_context: 平台上下文 (agent_id, user_id 等)
#             - attachments: 附件列表
#             - input_parts: 输入片段列表
#
#     Returns:
#         符合你的 State TypedDict 的 dict
#     """
#     return {
#         "query": payload["input"],
#         "kb_context": session_context.get("kb_context", {}),
#     }
