"""记忆库 Agent 集成测试

一个简单的交互式测试脚本，验证 Agent 的跨 session 记忆能力。

流程:
  Session 1: 告诉 Agent 一些个人信息
  Session 2: 新会话中验证 Agent 是否记住了这些信息

运行方式:
    cd examples/memory_demo_adk
    python test_memory_integration.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.tools import load_memory
from google.genai import types

from ksadk.memory.adk import LongTermMemory, ShortTermMemory

# ============================================================
# 配置
# ============================================================

APP_NAME = "memory_integration_test"

model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)

# ============================================================
# 工具函数
# ============================================================

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


async def ask(runner, session_id: str, question: str, timeout: int = 60) -> str:
    """向 Agent 提问并收集最终回答"""
    msg = types.Content(role="user", parts=[types.Part(text=question)])
    final = ""

    async def _run():
        nonlocal final
        async for event in runner.run_async(
            user_id="test_user", session_id=session_id, new_message=msg
        ):
            if (
                event.content
                and event.content.parts
                and hasattr(event.content.parts[0], "text")
                and event.content.parts[0].text
            ):
                if not getattr(event.content.parts[0], "thought", False):
                    final = event.content.parts[0].text.strip()

    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"  (超时 {timeout}s，返回已收集内容)")

    return final


# ============================================================
# 测试主流程
# ============================================================

async def main():
    section("记忆库 Agent 集成测试")

    # --- 检查 LLM 配置 ---
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key or api_key.startswith("<"):
        print("  OPENAI_API_KEY 未配置，请在 .env 中设置后重试")
        return

    print(f"  模型: {os.getenv('MODEL_NAME', 'deepseek-v3.2')}")
    print(f"  API : {os.getenv('OPENAI_API_BASE', '(未设置)')}")

    # --- 初始化记忆体 ---
    ltm_backend = os.getenv("KSADK_LTM_BACKEND", "local")
    print(f"  LTM : backend={ltm_backend}")

    stm = ShortTermMemory(backend="local")

    if ltm_backend == "sdk":
        ltm = LongTermMemory.from_env()
        ltm.app_name = APP_NAME
        ltm.index = APP_NAME
    else:
        ltm = LongTermMemory(backend="local", app_name=APP_NAME)

    # --- 创建 Agent ---
    agent = Agent(
        name="test_agent",
        model=model,
        description="记忆测试 Agent",
        instruction=(
            "你是一个有记忆能力的智能助手。\n"
            "当用户提问时，先使用 load_memory 工具检索历史记忆。\n"
            "如果从记忆中找到了相关信息，引用它来回答。\n"
            "用中文回答，保持简洁。"
        ),
        tools=[load_memory],
    )

    runner = Runner(
        agent=agent,
        session_service=stm.session_service,
        memory_service=ltm,
        app_name=APP_NAME,
    )

    results = {}

    # ========================================
    # Session 1: 存入个人信息
    # ========================================
    section("Session 1: 存入个人信息")

    session_1 = await stm.create_session(
        app_name=APP_NAME, user_id="test_user", session_id="integration_s1"
    )
    print(f"  Session ID: {session_1.id}")

    # 第一轮对话
    q1 = "你好！记住以下信息：我叫小明，我是一名后端工程师，最喜欢的编程语言是 Go，住在杭州西湖区。"
    print(f"\n  [用户] {q1}")
    a1 = await ask(runner, session_1.id, q1)
    print(f"  [Agent] {a1[:300]}")

    if a1:
        print(f"\n  {PASS} Session 1 对话成功")
        results["Session 1 对话"] = True
    else:
        print(f"\n  {FAIL} Session 1 对话失败 (空回答)")
        results["Session 1 对话"] = False

    # --- 保存 Session 1 到长期记忆 ---
    print(f"\n  {'─' * 56}")
    print("  保存 Session 1 到长期记忆...")

    completed = await stm.session_service.get_session(
        app_name=APP_NAME, user_id="test_user", session_id=session_1.id
    )
    if completed:
        await ltm.add_session_to_memory(completed)
        print(f"  {PASS} 已保存到 LTM")
    else:
        print(f"  {FAIL} 获取 Session 失败")

    # ========================================
    # Session 2: 跨 session 验证记忆
    # ========================================
    section("Session 2: 跨 session 记忆验证")

    session_2 = await stm.create_session(
        app_name=APP_NAME, user_id="test_user", session_id="integration_s2"
    )
    print(f"  Session ID: {session_2.id} (新会话)\n")

    # 测试 1: 基本信息回忆
    q2 = "你还记得我叫什么名字吗？我的职业是什么？"
    print(f"  [用户] {q2}")
    a2 = await ask(runner, session_2.id, q2)
    print(f"  [Agent] {a2[:300]}")

    has_name = "小明" in a2
    has_job = "后端" in a2 or "工程师" in a2
    if has_name and has_job:
        print(f"\n  {PASS} 基本信息回忆成功 (小明: ✓, 工程师: ✓)")
        results["基本信息回忆"] = True
    elif has_name or has_job:
        print(f"\n  {PASS} 部分信息回忆 (小明: {'✓' if has_name else '✗'}, 工程师: {'✓' if has_job else '✗'})")
        results["基本信息回忆"] = True
    else:
        print(f"\n  {FAIL} 基本信息回忆失败")
        results["基本信息回忆"] = False

    # 测试 2: 偏好信息回忆
    print(f"\n  {'─' * 56}")
    q3 = "我最喜欢什么编程语言？我住在哪里？"
    print(f"  [用户] {q3}")
    a3 = await ask(runner, session_2.id, q3)
    print(f"  [Agent] {a3[:300]}")

    has_lang = "Go" in a3 or "go" in a3
    has_city = "杭州" in a3 or "西湖" in a3
    if has_lang and has_city:
        print(f"\n  {PASS} 偏好信息回忆成功 (Go: ✓, 杭州: ✓)")
        results["偏好信息回忆"] = True
    elif has_lang or has_city:
        print(f"\n  {PASS} 部分偏好回忆 (Go: {'✓' if has_lang else '✗'}, 杭州: {'✓' if has_city else '✗'})")
        results["偏好信息回忆"] = True
    else:
        print(f"\n  {FAIL} 偏好信息回忆失败")
        results["偏好信息回忆"] = False

    # ========================================
    # 汇总
    # ========================================
    section("测试结果汇总")

    all_passed = True
    for name, ok in results.items():
        icon = PASS if ok else FAIL
        print(f"  {icon} {name}")
        if not ok:
            all_passed = False

    print()
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    if all_passed:
        print(f"  {passed}/{total} 全部通过!")
    else:
        print(f"  {passed}/{total} 通过，存在失败项")
    print()


if __name__ == "__main__":
    asyncio.run(main())
