"""
KsADK 长期记忆完整 Demo

演示如何在 ADK Agent 中使用金山云长期记忆能力。

本 demo 展示完整的记忆流程:
  1. 创建 Agent + 记忆体
  2. Session 1: 告诉 Agent 一些个人信息
  3. 将 Session 1 保存到长期记忆
  4. Session 2 (新对话): 验证 Agent 能通过 load_memory 检索到之前的信息

运行方式:
    # 设置环境变量
    cp .env.example .env
    # 编辑 .env 填入你的 API Key

    # 运行 demo
    python demo.py

参考: VeADK docs/examples/memory/long_mem_viking_agent.py
"""

import asyncio
import datetime
import json
import logging
import os
import sys

# 确保可以导入 ksadk
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv

load_dotenv()

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from ksadk.memory.adk import LongTermMemory, ShortTermMemory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ============== 配置 ==============
APP_NAME = "memory_demo"
USER_ID = "demo_user"

model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)


# ============== 方式一: 零代码配置 (推荐) ==============
#
# 用户只需要:
#   1. 写标准 ADK Agent (agent.py)
#   2. 在 .env 中设置 KSADK_LTM_BACKEND=local (或 http)
#   3. 运行: agentengine run .
#
# KsADK 的 ADKRunner 会自动:
#   - 读取环境变量创建 LongTermMemory
#   - 注入 load_memory 工具到 Agent
#   - 传递 memory_service 到 Google ADK Runner
#
# 此方式无需修改 Agent 代码，请参考 agent.py


# ============== 方式二: 代码中显式创建 (本 demo) ==============
#
# 适用于需要更精细控制的场景。


async def demo_memory_flow():
    """演示完整的记忆流程"""

    # ===== Step 1: 创建记忆体 =====
    logger.info("=" * 60)
    logger.info("[Step 1] 创建记忆体")
    logger.info("=" * 60)

    # 短期记忆 (会话管理)
    short_term_memory = ShortTermMemory(backend="local")

    # 长期记忆 (跨 session)
    # 本地开发: backend="local" (InMemory)
    # 生产环境: backend="http" + backend_config
    long_term_memory = LongTermMemory(
        backend="local",
        app_name=APP_NAME,
    )

    # ===== Step 2: 创建 Agent =====
    logger.info("[Step 2] 创建 Agent")

    # 导入 load_memory 工具
    from google.adk.tools import load_memory

    agent = Agent(
        name="memory_assistant",
        model=model,
        description="带长期记忆的智能助手",
        instruction=(
            "你是一个有记忆能力的智能助手。"
            "当用户提问时，先使用 load_memory 工具检索历史记忆。"
            "如果从记忆中找到了相关信息，自然地引用它。"
            "用友好的中文回复。"
        ),
        tools=[load_memory],
    )

    # ===== Step 3: 创建 Runner =====
    logger.info("[Step 3] 创建 Runner (注入 memory_service)")

    runner = Runner(
        agent=agent,
        session_service=short_term_memory.session_service,
        memory_service=long_term_memory,  # 关键: 注入长期记忆
        app_name=APP_NAME,
    )

    # ===== Step 4: Session 1 - 告诉 Agent 一些信息 =====
    logger.info("=" * 60)
    logger.info("[Step 4] Session 1 - 存入记忆")
    logger.info("=" * 60)

    session_id_1 = f"session_1_{int(datetime.datetime.now().timestamp())}"

    await short_term_memory.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id_1
    )
    logger.info(f"Session 1 创建: {session_id_1}")

    # 对话: 告诉 Agent 用户的偏好
    teaching_prompt = "我叫张三，我最喜欢的编程语言是 Python，我在北京工作。"
    logger.info(f"用户输入: {teaching_prompt}")

    user_message = types.Content(
        role="user", parts=[types.Part(text=teaching_prompt)]
    )

    final_response = ""
    async for event in runner.run_async(
        user_id=USER_ID, session_id=session_id_1, new_message=user_message
    ):
        if (
            event.content
            and event.content.parts
            and hasattr(event.content.parts[0], "text")
            and event.content.parts[0].text
        ):
            final_response = event.content.parts[0].text.strip()

    logger.info(f"Agent 回复: {final_response}")

    # ===== Step 5: 保存 Session 到长期记忆 =====
    logger.info("=" * 60)
    logger.info("[Step 5] 保存 Session 1 到长期记忆")
    logger.info("=" * 60)

    completed_session = await short_term_memory.session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id_1
    )

    if completed_session:
        await long_term_memory.add_session_to_memory(completed_session)
        logger.info(f"Session {session_id_1} 已保存到长期记忆!")
    else:
        logger.error("获取 Session 失败")
        return

    # ===== Step 6: Session 2 (新对话) - 验证记忆检索 =====
    logger.info("=" * 60)
    logger.info("[Step 6] Session 2 - 验证记忆检索")
    logger.info("=" * 60)

    session_id_2 = f"session_2_{int(datetime.datetime.now().timestamp())}"

    await short_term_memory.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id_2
    )
    logger.info(f"Session 2 创建: {session_id_2} (新的对话)")

    # 提问: Agent 应该能从长期记忆中找到之前的信息
    recall_prompt = "你还记得我喜欢什么编程语言吗？"
    logger.info(f"用户输入: {recall_prompt}")

    recall_message = types.Content(
        role="user", parts=[types.Part(text=recall_prompt)]
    )

    final_response = ""
    async for event in runner.run_async(
        user_id=USER_ID, session_id=session_id_2, new_message=recall_message
    ):
        if (
            event.content
            and event.content.parts
            and hasattr(event.content.parts[0], "text")
            and event.content.parts[0].text
        ):
            final_response = event.content.parts[0].text.strip()

    logger.info(f"Agent 回复: {final_response}")

    # ===== Step 7: 直接检索验证 =====
    logger.info("=" * 60)
    logger.info("[Step 7] 直接调用 search_memory 验证")
    logger.info("=" * 60)

    search_result = await long_term_memory.search_memory(
        app_name=APP_NAME, user_id=USER_ID, query="编程语言"
    )

    if search_result.memories:
        logger.info(f"检索到 {len(search_result.memories)} 条记忆:")
        for i, entry in enumerate(search_result.memories):
            text = entry.content.parts[0].text if entry.content.parts else ""
            logger.info(f"  [{i + 1}] {text[:100]}")
    else:
        logger.info("未检索到相关记忆")

    logger.info("=" * 60)
    logger.info("Demo 完成!")
    logger.info("=" * 60)


# ============== 方式三: 通过 agentengine CLI 运行 ==============
#
# 这是最推荐的生产方式。
# 用户只需要写 agent.py，设置环境变量，然后:
#
#   agentengine run .
#
# ADKRunner 会自动完成所有记忆体的初始化和注入。
#
# 要连接金山云记忆服务，只需在 .env 中设置:
#
#   KSADK_LTM_BACKEND=http
#   KSADK_LTM_HTTP_URL=https://memory.ksyun.com/api/v1
#   KSADK_LTM_HTTP_TOKEN=sk-xxx
#


if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║     KsADK 长期记忆 Demo                         ║")
    print("║     展示跨 Session 的记忆存储和检索               ║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    print(f"模型: {os.getenv('MODEL_NAME', 'deepseek-v3.2')}")
    print(f"API: {os.getenv('OPENAI_API_BASE', '(未设置)')[:50]}")
    print()

    asyncio.run(demo_memory_flow())
