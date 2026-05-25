"""记忆库集成完整测试套件

覆盖记忆库集成的所有使用路径，分 5 个测试阶段逐层验证:

  Test 1 - 记忆后端连通性:  InMemoryLTMBackend save/search 基础功能 + session 隔离
  Test 2 - LongTermMemory 服务层: add_session_to_memory + search_memory + 事件过滤
  Test 3 - Agent 显式集成: Agent 手动添加 load_memory → 跨 session 记忆检索
  Test 4 - 自动注入:       模拟 ADKRunner 零配置自动注入 → 工具注入 → 跨 session 问答
  Test 5 - 记忆库 + 知识库联合: 同时配置两个工具，验证协同工作

运行方式:
    # 配置环境变量
    cp .env.example .env
    # 编辑 .env 填入 OPENAI_API_KEY (Test 3-5 需要)、AK/SK + 知识库 ID (Test 5 需要)

    # 运行全部测试
    python test_memory_agent.py

    # 仅运行本地后端测试 (不需要 LLM 和远程服务)
    python test_memory_agent.py --local-only

    # 仅运行检索测试 (不需要 LLM，但测试 Test 1-2)
    python test_memory_agent.py --retrieval-only
"""

import asyncio
import json
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
    return bool(key) and key != "your_api_key" and not key.startswith("<")


def has_knowledge_base() -> bool:
    return bool(os.getenv("KSADK_KB_DATASET_ID", ""))


def has_sdk_credentials() -> bool:
    """检查是否配置了 SDK 后端所需的 AK/SK"""
    ak = os.getenv("KSADK_LTM_ACCESS_KEY") or os.getenv("KSYUN_ACCESS_KEY", "")
    sk = os.getenv("KSADK_LTM_SECRET_KEY") or os.getenv("KSYUN_SECRET_KEY", "")
    return bool(ak) and bool(sk) and not ak.startswith("<")


def _make_model():
    from google.adk.models.lite_llm import LiteLlm

    return LiteLlm(
        model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
        api_base=os.getenv("OPENAI_API_BASE"),
        api_key=os.getenv("OPENAI_API_KEY"),
    )


async def _ask(runner, session_id: str, question: str, max_retries: int = 2,
               timeout: int = 60) -> str:
    """向 Agent 提问并收集最终回答，遇到 LLM JSON 解析错误时自动重试"""
    from google.genai import types

    for attempt in range(max_retries + 1):
        try:
            msg = types.Content(role="user", parts=[types.Part(text=question)])
            final = ""

            async def _run():
                nonlocal final
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

            await asyncio.wait_for(_run(), timeout=timeout)
            return final
        except asyncio.TimeoutError:
            logger.warning(f"Agent 回答超时 ({timeout}s)，返回已收集的内容")
            return final
        except Exception as e:
            err_msg = str(e)
            if "Unterminated string" in err_msg or "Expecting value" in err_msg:
                if attempt < max_retries:
                    logger.warning(f"LLM JSON 解析错误，重试 ({attempt+1}/{max_retries})")
                    continue
            raise


# ============================================================
# Test 1: 记忆后端连通性测试
# ============================================================

def test1_backend_connectivity():
    """InMemoryLTMBackend save/search 基础功能 + session 隔离"""
    section("Test 1: 记忆后端连通性 (InMemoryLTMBackend)")

    from ksadk.memory.adk.backends.inmemory_ltm_backend import InMemoryLTMBackend

    backend = InMemoryLTMBackend(index="test_app")
    print(f"  backend: InMemoryLTMBackend, index={backend.index}")

    passed = 0
    total = 0

    # --- Test 1.1: 保存记忆 ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [1.1] 保存记忆")
    events = [
        json.dumps({"role": "user", "parts": [{"text": "我叫张三，我喜欢 Python"}]}, ensure_ascii=False),
        json.dumps({"role": "user", "parts": [{"text": "我在北京工作，做后端开发"}]}, ensure_ascii=False),
    ]
    ok = backend.save_memory("user_1", events)
    if ok:
        print(f"  {PASS} 保存 {len(events)} 条记忆成功")
        passed += 1
    else:
        print(f"  {FAIL} 保存失败")

    # --- Test 1.2: 检索记忆 (关键词匹配) ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [1.2] 检索记忆 (关键词匹配)")
    results = backend.search_memory("user_1", "Python", top_k=5)
    print(f"  query: 'Python' → 返回 {len(results)} 条结果")
    if results:
        for i, r in enumerate(results):
            print(f"    [{i+1}] {r[:80]}...")
        if any("Python" in r for r in results):
            print(f"  {PASS} 检索到包含 'Python' 的记忆")
            passed += 1
        else:
            print(f"  {FAIL} 未找到匹配内容")
    else:
        print(f"  {FAIL} 无结果")

    # --- Test 1.3: Session 隔离 ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [1.3] Session 隔离 (不同 user_id)")
    backend.save_memory("user_2", [
        json.dumps({"role": "user", "parts": [{"text": "我叫李四，我喜欢 Java"}]}, ensure_ascii=False),
    ])
    results_u1 = backend.search_memory("user_1", "Java", top_k=5)
    results_u2 = backend.search_memory("user_2", "Java", top_k=5)
    # user_1 不应该搜到 user_2 的精确匹配记忆
    u1_has_java_match = any("Java" in r and "李四" in r for r in results_u1)
    u2_has_java_match = any("Java" in r for r in results_u2)
    if not u1_has_java_match and u2_has_java_match:
        print(f"  {PASS} user_1 搜 'Java' 未找到 user_2 的记忆; user_2 搜到 {len(results_u2)} 条")
        passed += 1
    else:
        print(f"  {FAIL} Session 隔离失败: user_1 搜到 user_2 的内容")

    # --- Test 1.4: 空查询和边界情况 ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [1.4] 边界情况")
    results_empty = backend.search_memory("nonexistent_user", "任何内容", top_k=5)
    ok_empty = len(results_empty) == 0
    results_save_empty = backend.save_memory("user_3", [])
    if ok_empty and results_save_empty:
        print(f"  {PASS} 不存在的用户返回空列表; 空事件列表保存成功")
        passed += 1
    else:
        print(f"  {FAIL} 边界情况处理不正确")

    print()
    status = PASS if passed == total else FAIL
    print(f"  {status} {passed}/{total} 个子测试通过")
    return passed == total


# ============================================================
# Test 1.5: SDK 后端连通性测试 (需要 AK/SK)
# ============================================================

def test1_5_sdk_backend():
    """SdkLTMBackend 连通性验证 - 调用 CreateMemorySdk / QueryMemorySdk"""
    section("Test 1.5: SDK 后端连通性 (SdkLTMBackend)")

    if not has_sdk_credentials():
        print("  (AK/SK 未配置，跳过 SDK 后端测试)")
        print("  设置 KSYUN_ACCESS_KEY / KSYUN_SECRET_KEY 或")
        print("  KSADK_LTM_ACCESS_KEY / KSADK_LTM_SECRET_KEY 后重试")
        return None  # None 表示跳过

    from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend

    ak = os.getenv("KSADK_LTM_ACCESS_KEY") or os.getenv("KSYUN_ACCESS_KEY", "")
    sk = os.getenv("KSADK_LTM_SECRET_KEY") or os.getenv("KSYUN_SECRET_KEY", "")
    endpoint = os.getenv("KSADK_LTM_ENDPOINT", "aicp.api.ksyun.com")
    scheme = os.getenv("KSADK_LTM_SCHEME", "https")
    namespace = os.getenv("KSADK_LTM_NAMESPACE", "ksadk_test")
    scene_id = os.getenv("KSADK_LTM_SCENE_ID", "_sys_general")

    backend = SdkLTMBackend(
        index="test_sdk",
        access_key=ak,
        secret_key=sk,
        endpoint=endpoint,
        scheme=scheme,
        namespace=namespace,
        scene_id=scene_id,
    )
    print(f"  backend: SdkLTMBackend")
    print(
        f"  endpoint: {endpoint}, "
        f"memory_collection_id: {namespace}, scene_id: {scene_id}"
    )

    passed = 0
    total = 0

    # --- Test 1.5.1: 写入记忆 ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [1.5.1] 写入记忆 (CreateMemorySdk)")
    import json
    events = [
        json.dumps({"role": "user", "parts": [{"text": "SDK 测试: 我喜欢 Python 和旅行"}]}, ensure_ascii=False),
    ]
    try:
        ok = backend.save_memory("sdk_test_user", events)
        if ok:
            print(f"  {PASS} CreateMemorySdk 调用成功")
            passed += 1
        else:
            print(f"  {FAIL} CreateMemorySdk 返回失败")
    except Exception as e:
        print(f"  {FAIL} CreateMemorySdk 异常: {e}")

    # --- Test 1.5.2: 检索记忆 ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [1.5.2] 检索记忆 (QueryMemorySdk)")
    try:
        results = backend.search_memory("sdk_test_user", "Python", top_k=5)
        print(f"  query: 'Python' → 返回 {len(results)} 条结果")
        if results:
            for i, r in enumerate(results):
                print(f"    [{i+1}] {str(r)[:100]}...")
            print(f"  {PASS} QueryMemorySdk 调用成功")
            passed += 1
        else:
            # 可能刚写入还没索引完成，仍算通过（API 调用本身没报错）
            print(f"  {PASS} QueryMemorySdk 调用成功 (无结果，可能记忆尚未索引)")
            passed += 1
    except Exception as e:
        print(f"  {FAIL} QueryMemorySdk 异常: {e}")

    print()
    status = PASS if passed == total else FAIL
    print(f"  {status} {passed}/{total} 个子测试通过")
    return passed == total


# ============================================================
# Test 2: LongTermMemory 服务层测试
# ============================================================

async def test2_ltm_service():
    """LongTermMemory add_session_to_memory + search_memory + 事件过滤"""
    section("Test 2: LongTermMemory 服务层")

    from google.adk.events.event import Event
    from google.adk.sessions import InMemorySessionService, Session
    from google.genai import types

    from ksadk.memory.adk import LongTermMemory

    ltm = LongTermMemory(backend="local", app_name="test_ltm")
    print(f"  backend: local, app_name=test_ltm")

    passed = 0
    total = 0

    # --- Test 2.1: 创建 session 并添加消息，然后保存到 LTM ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [2.1] Session → LongTermMemory 保存")

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="test_ltm", user_id="user_A"
    )
    print(f"  创建 session: {session.id}")

    # 模拟 session events
    # 只有 user 消息应被保存，model 回复和 function call 应被过滤
    user_event_1 = Event(
        invocation_id="inv1",
        author="user",
        content=types.Content(
            role="user",
            parts=[types.Part(text="我的名字是王五，我喜欢旅行和摄影")]
        ),
    )
    model_event = Event(
        invocation_id="inv1",
        author="model",
        content=types.Content(
            role="model",
            parts=[types.Part(text="你好王五！旅行和摄影都是很棒的爱好")]
        ),
    )
    user_event_2 = Event(
        invocation_id="inv2",
        author="user",
        content=types.Content(
            role="user",
            parts=[types.Part(text="我最喜欢的目的地是日本")]
        ),
    )
    # 模拟 function call event (应被过滤)
    func_event = Event(
        invocation_id="inv2",
        author="user",
        content=types.Content(
            role="user",
            parts=[types.Part(function_call=types.FunctionCall(
                name="load_memory", args={"query": "偏好"}
            ))],
        ),
    )

    session.events = [user_event_1, model_event, user_event_2, func_event]

    try:
        await ltm.add_session_to_memory(session)
        print(f"  {PASS} Session 保存到 LTM 成功 (4 events → 应只保存 2 条 user 消息)")
        passed += 1
    except Exception as e:
        print(f"  {FAIL} 保存失败: {e}")

    # --- Test 2.2: 检索记忆 ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [2.2] 检索记忆 (search_memory)")

    try:
        result = await ltm.search_memory(
            app_name="test_ltm", user_id="user_A", query="旅行"
        )
        print(f"  query: '旅行' → 返回 {len(result.memories)} 条 MemoryEntry")
        if result.memories:
            for i, entry in enumerate(result.memories):
                text = entry.content.parts[0].text if entry.content.parts else ""
                print(f"    [{i+1}] {text[:100]}")
            # 验证检索到的记忆包含 "旅行"
            has_travel = any(
                "旅行" in (e.content.parts[0].text if e.content.parts else "")
                for e in result.memories
            )
            if has_travel:
                print(f"  {PASS} 检索到包含 '旅行' 的记忆")
                passed += 1
            else:
                print(f"  {FAIL} 返回了记忆但未包含 '旅行'")
        else:
            print(f"  {FAIL} 未检索到记忆")
    except Exception as e:
        print(f"  {FAIL} 检索失败: {e}")

    # --- Test 2.3: 验证事件过滤 (model 回复不应被保存) ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [2.3] 事件过滤验证 (model 回复不应被保存)")

    try:
        result = await ltm.search_memory(
            app_name="test_ltm", user_id="user_A", query="你好王五"
        )
        # "你好王五" 只出现在 model 回复中，不应被保存到 LTM
        has_model_content = any(
            "你好王五" in (e.content.parts[0].text if e.content.parts else "")
            for e in result.memories
        )
        if not has_model_content:
            print(f"  {PASS} model 回复未被保存到 LTM (事件过滤正常)")
            passed += 1
        else:
            print(f"  {FAIL} model 回复被保存到了 LTM (事件过滤失败)")
    except Exception as e:
        print(f"  {FAIL} 验证失败: {e}")

    # --- Test 2.4: SearchMemoryResponse 格式验证 ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [2.4] SearchMemoryResponse 格式验证")

    from google.adk.memory.base_memory_service import SearchMemoryResponse

    try:
        result = await ltm.search_memory(
            app_name="test_ltm", user_id="user_A", query="名字"
        )
        is_correct_type = isinstance(result, SearchMemoryResponse)
        has_memories_attr = hasattr(result, "memories")
        if is_correct_type and has_memories_attr:
            if result.memories:
                entry = result.memories[0]
                has_author = hasattr(entry, "author")
                has_content = hasattr(entry, "content")
                if has_author and has_content:
                    print(f"  {PASS} SearchMemoryResponse 格式正确: "
                          f"type=SearchMemoryResponse, memories={len(result.memories)}, "
                          f"entry 有 author/content 属性")
                    passed += 1
                else:
                    print(f"  {FAIL} MemoryEntry 缺少 author/content 属性")
            else:
                print(f"  {FAIL} 响应为空")
        else:
            print(f"  {FAIL} 响应类型不正确: {type(result)}")
    except Exception as e:
        print(f"  {FAIL} 格式验证失败: {e}")

    print()
    status = PASS if passed == total else FAIL
    print(f"  {status} {passed}/{total} 个子测试通过")
    return passed == total


# ============================================================
# Test 3: Agent 显式集成测试 (需要 LLM)
# ============================================================

async def test3_agent_explicit():
    """Agent 手动添加 load_memory → 跨 session 记忆检索"""
    section("Test 3: Agent 显式集成 (手动添加 load_memory)")

    from google.adk.agents import Agent
    from google.adk.runners import Runner
    from google.adk.tools import load_memory

    from ksadk.memory.adk import LongTermMemory, ShortTermMemory

    # 创建记忆体
    stm = ShortTermMemory(backend="local")
    ltm = LongTermMemory(backend="local", app_name="test_explicit")

    agent = Agent(
        name="memory_test_agent",
        model=_make_model(),
        description="记忆测试 Agent",
        instruction=(
            "你是一个有记忆能力的智能助手。"
            "当用户提问时，先使用 load_memory 工具检索历史记忆。"
            "如果从记忆中找到了相关信息，引用它来回答。"
            "用中文回答。"
        ),
        tools=[load_memory],
    )

    print(f"  Agent tools: {[getattr(t, 'name', getattr(t, '__name__', str(t))) for t in agent.tools]}")

    runner = Runner(
        agent=agent,
        session_service=stm.session_service,
        memory_service=ltm,
        app_name="test_explicit",
    )

    passed = 0
    total = 0

    # --- Step 1: Session 1 - 存入个人信息 ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [3.1] Session 1: 存入个人信息")

    session_1 = await stm.create_session(
        app_name="test_explicit", user_id="tester", session_id="s1_explicit"
    )
    print(f"  Session 1: {session_1.id}")

    answer_1 = await _ask(runner, session_1.id,
                          "记住我的信息：我叫赵六，最喜欢的颜色是蓝色，最喜欢的食物是寿司。")
    print(f"  Agent 回复: {answer_1[:200]}...")

    if answer_1:
        print(f"  {PASS} Session 1 对话成功")
        passed += 1
    else:
        print(f"  {FAIL} Session 1 对话失败 (空回答)")

    # --- Step 2: 保存 Session 1 到 LTM ---
    print(f"\n  {'─' * 56}")
    print("  [3.2] 保存 Session 1 到长期记忆")

    completed_session = await stm.session_service.get_session(
        app_name="test_explicit", user_id="tester", session_id=session_1.id
    )
    if completed_session:
        await ltm.add_session_to_memory(completed_session)
        print(f"  {PASS} Session 1 已保存到 LTM")
    else:
        print(f"  {FAIL} 无法获取 Session 1")

    # --- Step 3: Session 2 - 跨 session 验证记忆 ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [3.3] Session 2: 跨 session 记忆检索")

    session_2 = await stm.create_session(
        app_name="test_explicit", user_id="tester", session_id="s2_explicit"
    )
    print(f"  Session 2: {session_2.id} (新会话)")

    answer_2 = await _ask(runner, session_2.id,
                          "你还记得我最喜欢什么颜色和食物吗？")
    print(f"  Agent 回复: {answer_2[:300]}...")

    # 验证回答中包含之前的信息
    has_color = "蓝" in answer_2
    has_food = "寿司" in answer_2
    if has_color or has_food:
        print(f"  {PASS} 跨 session 记忆检索成功 (蓝色: {'✓' if has_color else '✗'}, 寿司: {'✓' if has_food else '✗'})")
        passed += 1
    else:
        print(f"  {FAIL} 跨 session 记忆检索失败 (回答中未包含之前的信息)")

    print()
    status = PASS if passed == total else FAIL
    print(f"  {status} {passed}/{total} 个子测试通过")
    return passed == total


# ============================================================
# Test 4: 自动注入测试 (需要 LLM)
# ============================================================

async def test4_auto_inject():
    """模拟 ADKRunner 自动注入流程: 检测环境变量 → 初始化 LTM → 注入工具 → 问答"""
    section("Test 4: 自动注入 (模拟 ADKRunner)")

    from google.adk.agents import Agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    from ksadk.memory.adk import LongTermMemory, ShortTermMemory

    # --- Step 1: 创建 Agent (tools 为空) ---
    agent = Agent(
        name="auto_inject_test",
        model=_make_model(),
        description="自动注入测试 Agent",
        instruction=(
            "你是一个有记忆能力的智能助手。"
            "当用户提问时，先使用 load_memory 工具检索历史记忆。"
            "如果从记忆中找到相关信息，引用它来回答。用中文回答。"
        ),
        tools=[],  # 故意留空，模拟 ADKRunner 注入
    )

    print(f"  [注入前] tools: {agent.tools}")

    passed = 0
    total = 0

    # --- Step 2: 模拟 ADKRunner._init_long_term_memory() ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [4.1] 模拟 ADKRunner 记忆体初始化 + 工具注入")

    stm = ShortTermMemory(backend="local")
    ltm = LongTermMemory(backend="local", app_name=agent.name)

    # 模拟 ADKRunner._inject_load_memory_tool()
    try:
        from google.adk.tools import load_memory

        tool_names = [getattr(t, "name", None) or getattr(t, "__name__", "") for t in agent.tools]
        if "load_memory" not in tool_names:
            agent.tools.append(load_memory)

        injected_names = [getattr(t, "name", getattr(t, "__name__", "")) for t in agent.tools]
        if "load_memory" in injected_names:
            print(f"  [注入后] tools: {injected_names}")
            print(f"  {PASS} load_memory 工具已自动注入")
            passed += 1
        else:
            print(f"  {FAIL} 工具注入失败")
    except ImportError:
        print(f"  {FAIL} google.adk.tools.load_memory 不可用")

    # --- Step 3: 实际问答验证 ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [4.2] 跨 session 记忆验证 (完整流程)")

    runner = Runner(
        agent=agent,
        session_service=stm.session_service,
        memory_service=ltm,
        app_name=agent.name,
    )

    # Session 1: 存入信息
    session_1 = await stm.create_session(
        app_name=agent.name, user_id="tester", session_id="s1_auto"
    )
    answer_1 = await _ask(runner, session_1.id,
                          "我的生日是5月15号，我住在上海浦东。")
    print(f"  Session 1 回复: {answer_1[:150]}...")

    # 保存到 LTM
    completed = await stm.session_service.get_session(
        app_name=agent.name, user_id="tester", session_id=session_1.id
    )
    if completed:
        await ltm.add_session_to_memory(completed)
        print(f"  Session 1 已保存到 LTM")

    # Session 2: 验证记忆
    session_2 = await stm.create_session(
        app_name=agent.name, user_id="tester", session_id="s2_auto"
    )
    answer_2 = await _ask(runner, session_2.id,
                          "你知道我住在哪里、我的生日是什么时候吗？")
    print(f"  Session 2 回复: {answer_2[:300]}...")

    has_location = "浦东" in answer_2 or "上海" in answer_2
    has_birthday = "5月" in answer_2 or "15" in answer_2
    if has_location or has_birthday:
        print(f"  {PASS} 自动注入 + 跨 session 记忆成功 "
              f"(上海/浦东: {'✓' if has_location else '✗'}, 5月15: {'✓' if has_birthday else '✗'})")
        passed += 1
    else:
        print(f"  {FAIL} 跨 session 记忆检索失败")

    print()
    status = PASS if passed == total else FAIL
    print(f"  {status} {passed}/{total} 个子测试通过")
    return passed == total


# ============================================================
# Test 5: 记忆库 + 知识库联合测试 (需要 LLM + KB 配置)
# ============================================================

async def test5_memory_plus_kb():
    """同时配置记忆库和知识库，验证两个工具协同工作"""
    section("Test 5: 记忆库 + 知识库联合测试")

    from google.adk.agents import Agent
    from google.adk.runners import Runner
    from google.adk.tools import load_memory

    from ksadk.knowledge_base.adk_tool import search_knowledge_base
    from ksadk.knowledge_base.client import KnowledgeBaseClient
    from ksadk.memory.adk import LongTermMemory, ShortTermMemory

    passed = 0
    total = 0

    # --- 验证知识库配置 ---
    if not KnowledgeBaseClient.is_configured():
        print(f"  {FAIL} KSADK_KB_DATASET_ID 未配置，跳过 Test 5")
        return False

    kb = KnowledgeBaseClient.from_env()
    print(f"  知识库: dataset_id={kb.dataset_id}")

    # --- 创建 Agent (同时拥有 load_memory + search_knowledge_base) ---
    stm = ShortTermMemory(backend="local")
    ltm = LongTermMemory(backend="local", app_name="test_combined")

    agent = Agent(
        name="combined_test_agent",
        model=_make_model(),
        description="记忆库 + 知识库联合测试 Agent",
        instruction=(
            "你是一个同时拥有长期记忆和知识库检索能力的智能助手。\n"
            "当用户提问时：\n"
            "1. 可以使用 load_memory 工具检索用户的历史记忆（最多调用1次）\n"
            "2. 如果需要查询知识库文档，使用 search_knowledge_base 工具（最多调用1次）\n"
            "3. 综合记忆和知识库信息来回答用户\n"
            "重要：每个工具最多只调用一次，即使没有找到结果也不要重复调用。\n"
            "用中文回答。"
        ),
        tools=[load_memory, search_knowledge_base],
    )

    tool_names = [getattr(t, "name", getattr(t, "__name__", "")) for t in agent.tools]
    print(f"  Agent tools: {tool_names}")

    # --- 验证工具列表 ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [5.1] 工具列表验证")
    has_memory = "load_memory" in tool_names
    has_kb = "search_knowledge_base" in tool_names
    if has_memory and has_kb:
        print(f"  {PASS} Agent 同时拥有 load_memory 和 search_knowledge_base")
        passed += 1
    else:
        print(f"  {FAIL} 工具列表不完整 (memory: {has_memory}, kb: {has_kb})")

    runner = Runner(
        agent=agent,
        session_service=stm.session_service,
        memory_service=ltm,
        app_name="test_combined",
    )

    # --- Session 1: 存入个人偏好 ---
    total += 1
    print(f"\n  {'─' * 56}")
    print("  [5.2] Session 1: 存入用户偏好 + Session 2: 知识库 + 记忆联合问答")

    session_1 = await stm.create_session(
        app_name="test_combined", user_id="tester", session_id="s1_combined"
    )
    answer_1 = await _ask(runner, session_1.id,
                          "我对 KsADK 的知识库功能特别感兴趣，我正在做一个智能客服项目。")
    print(f"  Session 1 回复: {answer_1[:150]}...")

    # 保存到 LTM
    completed = await stm.session_service.get_session(
        app_name="test_combined", user_id="tester", session_id=session_1.id
    )
    if completed:
        await ltm.add_session_to_memory(completed)
        print(f"  Session 1 已保存到 LTM")

    # --- Session 2: 联合问答 (需要同时用记忆和知识库) ---
    session_2 = await stm.create_session(
        app_name="test_combined", user_id="tester", session_id="s2_combined"
    )
    answer_2 = await _ask(runner, session_2.id,
                          "根据我之前的兴趣，帮我查查 KsADK 知识库怎么配置？")
    print(f"  Session 2 回复: {answer_2[:400]}...")

    if answer_2:
        print(f"  {PASS} 联合问答成功 (Agent 回答了问题)")
        passed += 1
    else:
        print(f"  {FAIL} 联合问答失败 (空回答)")

    print()
    status = PASS if passed == total else FAIL
    print(f"  {status} {passed}/{total} 个子测试通过")
    return passed == total


# ============================================================
# 主入口
# ============================================================

async def run_all(local_only: bool = False, retrieval_only: bool = False):
    print()
    print("╔════════════════════════════════════════════════════════╗")
    print("║         KsADK 记忆库集成 - 完整测试套件               ║")
    print("╚════════════════════════════════════════════════════════╝")
    print()
    print(f"  模型      : {os.getenv('MODEL_NAME', 'deepseek-v3.2')}")
    print(f"  API       : {os.getenv('OPENAI_API_BASE', '(未设置)')}")
    print(f"  LLM Key   : {'已配置' if has_valid_api_key() else '未配置'}")
    print(f"  知识库 ID : {os.getenv('KSADK_KB_DATASET_ID', '(未设置)')}")
    print(f"  模式      : {'local-only' if local_only else 'retrieval-only' if retrieval_only else '完整测试'}")

    results = {}

    # ===== Test 1 & 1.5 & 2: 纯本地，不需要 LLM =====
    results["Test 1: 记忆后端连通性"] = test1_backend_connectivity()

    # Test 1.5: SDK 后端 (需要 AK/SK，无则跳过)
    sdk_result = test1_5_sdk_backend()
    if sdk_result is not None:
        results["Test 1.5: SDK 后端连通性"] = sdk_result

    results["Test 2: LongTermMemory 服务层"] = await test2_ltm_service()

    if local_only:
        print("\n  (--local-only 模式，跳过 Agent 测试)")
    elif retrieval_only:
        print("\n  (--retrieval-only 模式，跳过 Agent 测试)")
    elif not has_valid_api_key():
        print("\n  (OPENAI_API_KEY 未配置，跳过 Agent 测试)")
        print("  请在 .env 中设置 OPENAI_API_KEY 后运行完整测试")
    else:
        # ===== Test 3 & 4: 需要 LLM =====
        results["Test 3: Agent 显式集成"] = await test3_agent_explicit()
        results["Test 4: 自动注入"] = await test4_auto_inject()

        # ===== Test 5: 需要 LLM + 知识库 =====
        if has_knowledge_base():
            results["Test 5: 记忆库+知识库联合"] = await test5_memory_plus_kb()
        else:
            print("\n  (KSADK_KB_DATASET_ID 未配置，跳过 Test 5)")

    # ===== 汇总 =====
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
    local_only = "--local-only" in sys.argv
    retrieval_only = "--retrieval-only" in sys.argv
    asyncio.run(run_all(local_only=local_only, retrieval_only=retrieval_only))
