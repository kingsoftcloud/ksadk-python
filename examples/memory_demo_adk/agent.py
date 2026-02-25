"""
智能助手 Agent (带长期记忆)

这是一个标准的 Google ADK Agent。
长期记忆由 KsADK 通过环境变量自动注入，Agent 代码无需任何改动。

运行方式:
    # 方式一: 通过 agentengine 运行 (推荐)
    cd examples/memory_demo_adk
    agentengine run .

    # 方式二: 直接运行 demo 脚本
    cd examples/memory_demo_adk
    python demo.py
"""

import os
from dotenv import load_dotenv

load_dotenv()

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

# ============== 模型配置 ==============
model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)


# ============== 工具定义 ==============

def get_user_profile(user_id: str) -> dict:
    """获取用户资料信息

    Args:
        user_id: 用户ID

    Returns:
        用户资料
    """
    # 模拟用户数据
    profiles = {
        "user_001": {"name": "张三", "city": "北京", "role": "工程师"},
        "user_002": {"name": "李四", "city": "上海", "role": "产品经理"},
    }
    return profiles.get(user_id, {"name": "未知用户", "city": "未知", "role": "未知"})


def search_knowledge(query: str) -> dict:
    """搜索知识库

    Args:
        query: 搜索关键词

    Returns:
        搜索结果
    """
    return {
        "status": "success",
        "query": query,
        "results": [
            f"关于「{query}」的知识条目 1: 这是一条相关信息。",
            f"关于「{query}」的知识条目 2: 这是另一条相关信息。",
        ],
    }


# ============== Agent 定义 ==============
#
# 注意: 这里不需要手动添加 load_memory 工具。
#       当设置了 KSADK_LTM_BACKEND 环境变量后，
#       KsADK 的 ADKRunner 会自动注入 load_memory 工具。

root_agent = Agent(
    name="memory_assistant",
    model=model,
    description="带长期记忆的智能助手 - 能记住跨 session 的用户信息",
    instruction="""你是一个有记忆能力的智能助手。

你有以下能力：
1. **记忆检索**: 当用户提问时，先使用 load_memory 工具检索历史记忆，
   看看之前的对话中是否有相关信息。
2. **用户资料**: 使用 get_user_profile 查询用户信息。
3. **知识搜索**: 使用 search_knowledge 搜索知识库。

重要行为规则：
- 每次对话开始时，主动使用 load_memory 检索用户历史，了解用户偏好和之前的对话内容。
- 如果用户提到了他们的偏好、习惯、个人信息，记住这些信息（它们会被自动存储）。
- 用友好的中文回复用户。
- 如果从记忆中找到了相关信息，自然地引用它，让用户感受到被记住。
""",
    tools=[
        get_user_profile,
        search_knowledge,
        # load_memory 会由 KsADK 自动注入，无需手动添加
    ],
)
