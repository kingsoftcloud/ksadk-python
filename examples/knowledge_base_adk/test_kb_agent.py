"""知识库集成完整测试套件

覆盖知识库集成的所有使用路径，分 4 个测试阶段逐层验证:

  Test 1 - 客户端连通性:  KnowledgeBaseClient 直连 AICP API 检索
  Test 2 - 工具层封装:    search_knowledge() 函数 + 结果格式化
  Test 3 - Agent 显式集成: Agent 显式添加工具 → 检索 → LLM 回答
  Test 4 - 自动注入:       模拟 ADKRunner 零配置自动注入工具 → 检索 → LLM 回答

运行方式:
    # 配置环境变量
    cp .env.example .env
    # 编辑 .env 填入 OPENAI_API_KEY、AK/SK、知识库 ID

    # 运行全部测试 (Test 1-2 不需要 LLM，Test 3-4 需要)
    python test_kb_agent.py

    # 仅运行检索测试 (不需要 LLM API Key)
    python test_kb_agent.py --retrieval-only
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================
# 公共工具
# ============================================================

PASS = "[PASS]"
FAIL = "[FAIL]"


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def has_valid_api_key() -> bool:
    key = os.getenv("OPENAI_API_KEY", "")
    return bool(key) and key != "your_api_key"


def _make_model():
    from google.adk.models.lite_llm import LiteLlm

    return LiteLlm(
        model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
        api_base=os.getenv("OPENAI_API_BASE"),
        api_key=os.getenv("OPENAI_API_KEY"),
    )


async def _ask(runner, session_id: str, question: str, max_retries: int = 2) -> str:
    """向 Agent 提问并收集最终回答，遇到 LLM JSON 解析错误时自动重试"""
    from google.genai import types

    for attempt in range(max_retries + 1):
        try:
            msg = types.Content(role="user", parts=[types.Part(text=question)])
            final = ""
            async for event in runner.run_async(
                user_id="tester", session_id=session_id, new_message=msg
            ):
                if (
                    event.content
                    and event.content.parts
                    and hasattr(event.content.parts[0], "text")
                    and event.content.parts[0].text
                ):
                    if not getattr(event.content.parts[0], "thought", False):
                        final = event.content.parts[0].text.strip()
            return final
        except Exception as e:
            err_msg = str(e)
            # LLM 偶发的 JSON 格式问题，重试
            if "Unterminated string" in err_msg or "Expecting value" in err_msg:
                if attempt < max_retries:
                    logger.warning(f"LLM JSON 解析错误，重试 ({attempt+1}/{max_retries})")
                    continue
            raise


# ============================================================
# Test 1: 客户端连通性测试
# ============================================================

def test1_client_connectivity():
    """直接通过 KnowledgeBaseClient 检索，验证 AICP API 连通性"""
    section("Test 1: 客户端连通性 (KnowledgeBaseClient)")

    from ksadk.knowledge_base.client import KnowledgeBaseClient

    kb = KnowledgeBaseClient.from_env()
    print(f"  dataset_id : {kb.dataset_id}")
    print(f"  endpoint   : {kb.endpoint}")
    print(f"  scheme     : {kb.scheme}")
    print(f"  region     : {kb.region}")
    print(f"  top_k      : {kb.top_k}")
    print()

    queries = [
        ("KsADK 是什么", "01_产品概述"),
        ("知识库如何配置", "03_知识库配置"),
        ("如何部署 Agent", "05_部署指南"),
        ("记忆体配置", "04_记忆体配置"),
        ("工具开发指南", "06_工具开发"),
    ]

    passed = 0
    for q, expected_doc_prefix in queries:
        print(f"  {'─' * 56}")
        print(f"  query: {q}")
        try:
            results = kb.search(q, top_k=3)
            print(f"  返回 {len(results)} 条结果:")
            for j, r in enumerate(results):
                doc = r.document_name or "未知"
                snippet = r.content[:100].replace("\n", " ")
                print(f"    [{j+1}] score={r.score:.3f} | {doc}")
                print(f"        {snippet}...")
            if results:
                passed += 1
            print()
        except Exception as e:
            print(f"    [ERROR] {e}\n")

    status = PASS if passed == len(queries) else FAIL
    print(f"  {status} {passed}/{len(queries)} 个查询返回结果")
    return passed == len(queries)


# ============================================================
# Test 2: 工具层封装测试
# ============================================================

def test2_tool_function():
    """通过 search_knowledge() 函数调用，验证工具层封装和结果格式化"""
    section("Test 2: 工具层封装 (search_knowledge)")

    from ksadk.knowledge_base.tool import search_knowledge

    test_queries = [
        "快速入门指南",
        "常见问题",
    ]

    passed = 0
    for q in test_queries:
        print(f"  query: {q}")
        result = search_knowledge(q, top_k=3)
        if result and "来源:" in result:
            preview = result[:200].replace("\n", "\n  ")
            print(f"  {preview}...")
            print(f"  {PASS} 返回格式化结果\n")
            passed += 1
        elif result:
            print(f"  {result[:200]}...")
            print(f"  {PASS} 返回结果 (无来源标注)\n")
            passed += 1
        else:
            print(f"  {FAIL} 无结果\n")

    status = PASS if passed == len(test_queries) else FAIL
    print(f"  {status} {passed}/{len(test_queries)} 个查询通过")
    return passed == len(test_queries)


# ============================================================
# Test 3: Agent 显式集成测试
# ============================================================

async def test3_agent_explicit():
    """Agent 显式导入 search_knowledge_base 工具，验证完整 RAG 问答"""
    section("Test 3: Agent 显式集成 (手动添加工具)")

    from google.adk.agents import Agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from ksadk.knowledge_base.adk_tool import search_knowledge_base

    agent = Agent(
        name="kb_explicit_test",
        model=_make_model(),
        description="知识库问答测试 Agent (显式集成)",
        instruction=(
            "你是一个知识库问答助手。收到用户问题时，先调用 search_knowledge_base 工具检索，"
            "然后基于检索结果给出准确回答。回答要标注信息来源文档名。用中文回答。"
        ),
        tools=[search_knowledge_base],
    )

    print(f"  Agent tools: {[getattr(t, '__name__', str(t)) for t in agent.tools]}")

    session_service = InMemorySessionService()
    runner = Runner(agent=agent, session_service=session_service, app_name="kb_test")
    session = await session_service.create_session(app_name="kb_test", user_id="tester")

    questions = [
        "KsADK 是什么？有哪些核心功能？",
        "如何配置知识库？需要哪些参数？",
        "如何部署一个 Agent 应用？",
    ]

    passed = 0
    for i, q in enumerate(questions, 1):
        print(f"\n  {'─' * 56}")
        print(f"  [Q{i}] {q}")
        try:
            answer = await _ask(runner, session.id, q)
            if answer:
                print(f"  [A{i}] {answer[:400]}...")
                passed += 1
            else:
                print(f"  [A{i}] (空回答)")
        except Exception as e:
            print(f"  [ERROR] {e}")

    print()
    status = PASS if passed == len(questions) else FAIL
    print(f"  {status} {passed}/{len(questions)} 个问答通过")
    return passed == len(questions)


# ============================================================
# Test 4: 自动注入测试
# ============================================================

async def test4_auto_inject():
    """模拟 ADKRunner 自动注入流程: Agent tools=[] → 检测环境变量 → 注入工具 → 问答"""
    section("Test 4: 自动注入 (模拟 ADKRunner)")

    from google.adk.agents import Agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from ksadk.knowledge_base.client import KnowledgeBaseClient

    agent = Agent(
        name="auto_inject_test",
        model=_make_model(),
        description="自动注入测试 Agent",
        instruction=(
            "你是一个知识库问答助手。当用户提问时，使用 search_knowledge_base 工具检索，"
            "然后基于结果回答。标注来源文档。用中文。"
        ),
        tools=[],  # 故意留空
    )

    print(f"  [注入前] tools: {agent.tools}")

    # 模拟 ADKRunner._init_knowledge_base() + _inject_search_knowledge_tool()
    if not KnowledgeBaseClient.is_configured():
        print(f"  {FAIL} KSADK_KB_DATASET_ID 未配置")
        return False

    kb = KnowledgeBaseClient.from_env()
    print(f"  知识库已检测: dataset_id={kb.dataset_id}")

    from ksadk.knowledge_base.adk_tool import search_knowledge_base

    tool_names = [getattr(t, "name", None) or getattr(t, "__name__", "") for t in agent.tools]
    if "search_knowledge_base" not in tool_names:
        agent.tools.append(search_knowledge_base)

    print(f"  [注入后] tools: {[getattr(t, '__name__', str(t)) for t in agent.tools]}")

    final_names = [getattr(t, '__name__', getattr(t, 'name', '')) for t in agent.tools]
    if "search_knowledge_base" not in final_names:
        print(f"  {FAIL} 工具注入失败")
        return False

    print(f"  {PASS} search_knowledge_base 工具已自动注入")

    # 实际问答验证
    session_service = InMemorySessionService()
    runner = Runner(agent=agent, session_service=session_service, app_name="auto_test")
    session = await session_service.create_session(app_name="auto_test", user_id="tester")

    questions = [
        "Agent 工具开发有哪些关键要求？",
        "部署 Agent 有几种方式？",
    ]

    passed = 0
    for i, q in enumerate(questions, 1):
        print(f"\n  {'─' * 56}")
        print(f"  [Q{i}] {q}")
        try:
            answer = await _ask(runner, session.id, q)
            if answer:
                print(f"  [A{i}] {answer[:400]}...")
                passed += 1
            else:
                print(f"  [A{i}] (空回答)")
        except Exception as e:
            print(f"  [ERROR] {e}")

    print()
    status = PASS if passed == len(questions) else FAIL
    print(f"  {status} {passed}/{len(questions)} 个问答通过")
    return passed == len(questions)


# ============================================================
# 主入口
# ============================================================

async def run_all(retrieval_only: bool = False):
    print()
    print("╔════════════════════════════════════════════════════════╗")
    print("║          KsADK 知识库集成 - 完整测试套件              ║")
    print("╚════════════════════════════════════════════════════════╝")
    print()
    print(f"  模型      : {os.getenv('MODEL_NAME', 'deepseek-v3.2')}")
    print(f"  API       : {os.getenv('OPENAI_API_BASE', '(未设置)')}")
    print(f"  知识库 ID : {os.getenv('KSADK_KB_DATASET_ID', '(未设置)')}")
    print(f"  端点      : {os.getenv('KSADK_KB_ENDPOINT', 'aicp.api.ksyun.com')}")
    print(f"  LLM Key   : {'已配置' if has_valid_api_key() else '未配置'}")

    results = {}

    # Test 1 & 2: 纯检索，不需要 LLM
    results["Test 1: 客户端连通性"] = test1_client_connectivity()
    results["Test 2: 工具层封装"] = test2_tool_function()

    if retrieval_only:
        print("\n  (--retrieval-only 模式，跳过 Agent 测试)")
    elif not has_valid_api_key():
        print("\n  (OPENAI_API_KEY 未配置，跳过 Agent 测试)")
        print("  请在 .env 中设置 OPENAI_API_KEY 后运行完整测试")
    else:
        # Test 3 & 4: 需要 LLM
        results["Test 3: Agent 显式集成"] = await test3_agent_explicit()
        results["Test 4: 自动注入"] = await test4_auto_inject()

    # 汇总
    section("测试结果汇总")
    all_passed = True
    for name, ok in results.items():
        icon = PASS if ok else FAIL
        print(f"  {icon} {name}")
        if not ok:
            all_passed = False

    print()
    if all_passed:
        print("  所有测试通过!")
    else:
        print("  存在失败的测试，请检查上方输出")
    print()


if __name__ == "__main__":
    retrieval_only = "--retrieval-only" in sys.argv
    asyncio.run(run_all(retrieval_only=retrieval_only))
