"""
KsADK 知识库问答完整 Demo

演示如何在 ADK Agent 中使用金山云知识库检索能力。

本 demo 展示完整流程:
  1. 创建知识库客户端，验证连通性
  2. 创建带知识库工具的 Agent
  3. 发送问题，Agent 自动调用知识库检索并回答

运行方式:
    # 设置环境变量
    cp .env.example .env
    # 编辑 .env 填入你的 API Key、AK/SK、知识库 ID

    # 运行 demo
    python demo.py
"""

import asyncio
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

from ksadk.knowledge_base.client import KnowledgeBaseClient
from ksadk.knowledge_base.adk_tool import search_knowledge_base

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ============== 配置 ==============
APP_NAME = "knowledge_base_demo"
USER_ID = "demo_user"

model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)


async def demo_knowledge_base():
    """演示完整的知识库问答流程"""

    # ===== Step 1: 验证知识库连通性 =====
    logger.info("=" * 60)
    logger.info("[Step 1] 创建知识库客户端，验证连通性")
    logger.info("=" * 60)

    kb_client = KnowledgeBaseClient.from_env()
    logger.info(
        f"知识库客户端已创建: dataset_id={kb_client.dataset_id}, "
        f"region={kb_client.region}"
    )

    # 直接调用检索，验证 API 可达
    test_query = "测试查询"
    logger.info(f"测试检索: query='{test_query}'")

    try:
        results = kb_client.search(test_query, top_k=3)
        logger.info(f"检索成功! 返回 {len(results)} 条结果")
        for i, r in enumerate(results):
            logger.info(
                f"  [{i + 1}] score={r.score:.3f} "
                f"doc={r.document_name or 'N/A'} "
                f"content={r.content[:80]}..."
            )
    except Exception as e:
        logger.error(f"检索失败: {e}")
        logger.error("请检查 AK/SK、知识库 ID 和网络连通性")
        return

    # ===== Step 2: 创建带知识库工具的 Agent =====
    logger.info("=" * 60)
    logger.info("[Step 2] 创建 Agent (显式添加 search_knowledge_base 工具)")
    logger.info("=" * 60)

    agent = Agent(
        name="kb_assistant",
        model=model,
        description="基于知识库的智能问答助手",
        instruction=(
            "你是一个专业的知识库问答助手。\n"
            "当用户提问时，请使用 search_knowledge_base 工具从知识库中检索相关信息。\n"
            "基于检索结果给出准确的回答，并标注信息来源。\n"
            "如果知识库中没有相关信息，如实告知用户。\n"
            "用中文回复。"
        ),
        tools=[search_knowledge_base],
    )

    # ===== Step 3: 创建 Runner =====
    logger.info("[Step 3] 创建 Runner")

    runner = Runner(
        agent=agent,
        session_service=InMemorySessionService(),
        app_name=APP_NAME,
    )

    # ===== Step 4: 对话 - Agent 自动调用知识库 =====
    logger.info("=" * 60)
    logger.info("[Step 4] 发送问题，Agent 自动检索知识库并回答")
    logger.info("=" * 60)

    # 创建 session
    session = await InMemorySessionService().create_session(
        app_name=APP_NAME, user_id=USER_ID
    )
    session_id = session.id
    logger.info(f"Session 创建: {session_id}")

    # 提问
    question = os.getenv("DEMO_QUESTION", "请帮我查一下相关信息")
    logger.info(f"用户提问: {question}")

    user_message = types.Content(
        role="user", parts=[types.Part(text=question)]
    )

    final_response = ""
    async for event in runner.run_async(
        user_id=USER_ID, session_id=session_id, new_message=user_message
    ):
        if (
            event.content
            and event.content.parts
            and hasattr(event.content.parts[0], "text")
            and event.content.parts[0].text
        ):
            is_thought = getattr(event.content.parts[0], "thought", False)
            if not is_thought:
                final_response = event.content.parts[0].text.strip()

    logger.info("-" * 60)
    logger.info("Agent 回答:")
    logger.info(final_response)

    # ===== Step 5: 再来一轮对话 =====
    logger.info("=" * 60)
    logger.info("[Step 5] 追问一个问题")
    logger.info("=" * 60)

    follow_up = "能再详细解释一下吗？"
    logger.info(f"用户追问: {follow_up}")

    follow_up_message = types.Content(
        role="user", parts=[types.Part(text=follow_up)]
    )

    final_response = ""
    async for event in runner.run_async(
        user_id=USER_ID, session_id=session_id, new_message=follow_up_message
    ):
        if (
            event.content
            and event.content.parts
            and hasattr(event.content.parts[0], "text")
            and event.content.parts[0].text
        ):
            is_thought = getattr(event.content.parts[0], "thought", False)
            if not is_thought:
                final_response = event.content.parts[0].text.strip()

    logger.info("-" * 60)
    logger.info("Agent 回答:")
    logger.info(final_response)

    logger.info("=" * 60)
    logger.info("Demo 完成!")
    logger.info("=" * 60)


if __name__ == "__main__":
    print()
    print("=" * 56)
    print("   KsADK 知识库问答 Demo")
    print("   演示 Agent 自动检索知识库并回答问题")
    print("=" * 56)
    print()
    print(f"模型: {os.getenv('MODEL_NAME', 'deepseek-v3.2')}")
    print(f"API:  {os.getenv('OPENAI_API_BASE', '(未设置)')[:50]}")
    print(f"知识库 ID: {os.getenv('KSADK_KB_DATASET_ID', '(未设置)')}")
    print()

    if not os.getenv("KSADK_KB_DATASET_ID"):
        print("错误: 请设置 KSADK_KB_DATASET_ID 环境变量")
        print("提示: cp .env.example .env && vim .env")
        sys.exit(1)

    asyncio.run(demo_knowledge_base())
