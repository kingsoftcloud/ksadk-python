"""
智能助手 Agent (ADK 版本)

一个功能丰富的智能助手，支持多种实用工具：
- 天气查询
- 数学计算
- 网络搜索
- 时间查询
- 笔记管理
- 任务管理
"""

import os
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# 加载环境变量
load_dotenv(Path(__file__).parent.parent / ".env")

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool import MCPToolset, StreamableHTTPConnectionParams

# ============== 模型配置 ==============
model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)

# ============== 内存存储 ==============
_notes_storage: list = []
_tasks_storage: list = []


# ============== 工具定义 ==============


def get_weather(city: str) -> dict:
    """获取指定城市的天气信息

    Args:
        city: 城市名称，如 "北京"、"上海"、"深圳"

    Returns:
        包含天气信息的字典
    """
    # 模拟天气数据
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
        # 随机生成天气
        return {
            "status": "success",
            "city": city,
            "condition": random.choice(["晴朗", "多云", "阴天", "小雨"]),
            "temperature": f"{random.randint(15, 35)}°C",
            "humidity": f"{random.randint(30, 80)}%",
            "wind": random.choice(["东风2级", "西风3级", "南风2级", "北风3级"]),
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }


def calculate(expression: str) -> dict:
    """计算数学表达式

    Args:
        expression: 数学表达式，如 "2 + 3 * 4"、"sqrt(16)"、"100 / 5"

    Returns:
        计算结果
    """
    import math

    # 安全的数学函数
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
        # 替换常见的中文符号
        expr = expression.replace("×", "*").replace("÷", "/").replace("（", "(").replace("）", ")")
        result = eval(expr, {"__builtins__": {}}, safe_dict)
        return {"status": "success", "expression": expression, "result": result}
    except Exception as e:
        return {"status": "error", "expression": expression, "error": f"计算错误: {str(e)}"}


def search_web(query: str, num_results: int = 3) -> dict:
    """搜索网络信息（模拟）

    Args:
        query: 搜索关键词
        num_results: 返回结果数量，默认3条

    Returns:
        搜索结果列表
    """
    # 模拟搜索结果
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


def get_current_time(timezone: Optional[str] = None) -> dict:
    """获取当前时间

    Args:
        timezone: 时区，如 "Asia/Shanghai"、"America/New_York"，默认为本地时间

    Returns:
        当前时间信息
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


def create_note(title: str, content: str, tags: Optional[str] = None) -> dict:
    """创建笔记

    Args:
        title: 笔记标题
        content: 笔记内容
        tags: 标签，用逗号分隔，如 "工作,重要"

    Returns:
        创建结果
    """
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


def list_notes(tag: Optional[str] = None) -> dict:
    """列出所有笔记

    Args:
        tag: 可选，按标签筛选

    Returns:
        笔记列表
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


def create_task(title: str, priority: str = "medium", due_date: Optional[str] = None) -> dict:
    """创建任务

    Args:
        title: 任务标题
        priority: 优先级，可选 "high"、"medium"、"low"
        due_date: 截止日期，格式如 "2024-12-31"

    Returns:
        创建结果
    """
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


def list_tasks(status: Optional[str] = None) -> dict:
    """列出所有任务

    Args:
        status: 可选，按状态筛选，可选 "pending"、"completed"

    Returns:
        任务列表
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


def complete_task(task_id: int) -> dict:
    """完成任务

    Args:
        task_id: 任务ID

    Returns:
        操作结果
    """
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


# ============== MCP 工具配置 ==============

# 秘塔 AI 搜索 MCP (只有配置了 API Key 才启用)
metaso_mcp = None
if os.getenv("KSC_AIPRO_API_KEY"):
    metaso_mcp = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(
            url="https://metaso-ifih3vh.aipro.ksyun.com/api/mcp",
            headers={"Authorization": f"Bearer {os.getenv('KSC_AIPRO_API_KEY')}"},
        )
    )


# ============== Agent 定义 ==============

root_agent = Agent(
    name="smart_assistant",
    model=model,
    description="智能助手 - 支持天气查询、数学计算、网络搜索、笔记和任务管理",
    instruction="""你是一个功能强大的智能助手，可以帮助用户完成多种任务：

1. **天气查询**: 使用 get_weather 工具查询城市天气
2. **数学计算**: 使用 calculate 工具进行数学运算
3. **网络搜索**: 使用 search_web 工具搜索信息
4. **时间查询**: 使用 get_current_time 工具获取当前时间
5. **笔记管理**: 使用 create_note 和 list_notes 工具管理笔记
6. **任务管理**: 使用 create_task、list_tasks 和 complete_task 工具管理任务
7. **AI 搜索**: 使用秘塔工具 (metaso_web_search/metaso_web_reader/metaso_chat) 进行深度信息检索和问答 (仅当配置了 API Key 时可用)

请根据用户的需求选择合适的工具，并用友好的中文回复。
当用户请求需要多个步骤时，请依次调用相关工具完成任务。
回复时请整理工具返回的信息，用清晰易读的格式呈现给用户。
""",
    tools=[
        get_weather,
        calculate,
        search_web,
        get_current_time,
        create_note,
        list_notes,
        create_task,
        list_tasks,
        complete_task,
    ]
    + ([metaso_mcp] if metaso_mcp else []),
)
