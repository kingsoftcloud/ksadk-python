"""金山云 AICP 记忆库 SDK 独立测试脚本

直接使用 kingsoftcloud-sdk-python 调用 CreateMemorySdk / QueryMemorySdk，
同时测试 KsADK 封装层 SdkLTMBackend 的正确性。

测试覆盖:
  Test 1: CreateMemorySdk 单条消息写入 (直接 SDK 调用)
  Test 2: CreateMemorySdk 多条对话写入 (直接 SDK 调用)
  Test 3: QueryMemorySdk 语义检索    (直接 SDK 调用)
  Test 4: SdkLTMBackend 封装层写入   (通过 KsADK 调用)
  Test 5: SdkLTMBackend 封装层检索   (通过 KsADK 调用)

配置:
    在 .env 中设置:
      KSYUN_ACCESS_KEY=你的AK
      KSYUN_SECRET_KEY=你的SK
      KSADK_LTM_ENDPOINT=aicp.inner.api.ksyun.com   (内网)
      KSADK_LTM_SCHEME=http
      KSADK_LTM_NAMESPACE=你的记忆库Namespace

运行:
    cd examples/memory_demo_adk
    python test_sdk_memory.py
"""

import json
import os
import sys
import time
import uuid

# 绕过本地代理 (Clash/V2Ray 等)，防止内网请求被代理劫持
os.environ.setdefault("NO_PROXY", "*.ksyun.com,10.*,192.168.*")
os.environ.setdefault("no_proxy", "*.ksyun.com,10.*,192.168.*")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


# ============================================================
# 配置读取
# ============================================================

AK = os.getenv("KSADK_LTM_ACCESS_KEY") or os.getenv("KSYUN_ACCESS_KEY", "")
SK = os.getenv("KSADK_LTM_SECRET_KEY") or os.getenv("KSYUN_SECRET_KEY", "")
ENDPOINT = os.getenv("KSADK_LTM_ENDPOINT", "aicp.inner.api.ksyun.com")
SCHEME = os.getenv("KSADK_LTM_SCHEME", "http")
REGION = os.getenv("KSADK_LTM_REGION", "cn-beijing-6")
NAMESPACE = os.getenv("KSADK_LTM_NAMESPACE", "")

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
INFO = "\033[94m[INFO]\033[0m"


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


# ============================================================
# 工具函数
# ============================================================

def build_conversation(messages):
    """将消息列表转换为 CreateMemorySdk 的 Conversation 格式

    正确的 API Data 结构:
    {
        "Conversation": [
            {
                "Role": "user",
                "CreatedAt": 1769513379808,
                "MessageId": "uuid",
                "Content": [{"Type": "input_text", "Text": "..."}]
            }
        ]
    }

    Args:
        messages: 消息列表，支持两种格式:
            - [{"role": "user", "text": "..."}]  — 带角色的字典
            - ["纯文本字符串", ...]               — 默认 role=user

    Returns:
        Conversation 列表
    """
    conversation = []
    for msg in messages:
        if isinstance(msg, dict):
            role = msg.get("role", "user")
            text = msg.get("text", "")
        else:
            role = "user"
            text = str(msg)

        conversation.append({
            "Role": role,
            "CreatedAt": int(time.time() * 1000),
            "MessageId": str(uuid.uuid4()),
            "Content": [{"Type": "input_text", "Text": text}],
        })
    return conversation


def diagnose_error(err_msg: str):
    """根据错误信息输出诊断建议"""
    if "NotFound" in err_msg:
        print(f"\n  >>> 诊断: Namespace '{NAMESPACE}' 不存在，请在 AICP 控制台确认正确的记忆库 ID")
    elif "InnerAccountCanOnlyAccessThroughIntranet" in err_msg:
        print(f"\n  >>> 诊断: 内网账号不能走外网，请设置:")
        print(f"      KSADK_LTM_ENDPOINT=aicp.inner.api.ksyun.com")
        print(f"      KSADK_LTM_SCHEME=http")
    elif "MissingAccesskey" in err_msg:
        print(f"\n  >>> 诊断: AK/SK 未配置，请检查 KSYUN_ACCESS_KEY / KSYUN_SECRET_KEY")
    elif "ServerNetworkError" in err_msg:
        print(f"\n  >>> 诊断: 网络错误，可能是本地代理劫持了请求")
        print(f"      尝试: NO_PROXY='*.ksyun.com' python test_sdk_memory.py")


# ============================================================
# 初始化 SDK 客户端
# ============================================================

def create_client():
    """创建 AICP 客户端 (多版本 fallback)"""
    from ksyun.common import credential
    from ksyun.common.profile.client_profile import ClientProfile
    from ksyun.common.profile.http_profile import HttpProfile

    aicp_module = None
    for version in ["v20251114", "v20251212", "v20240612"]:
        try:
            aicp_module = __import__(
                f"ksyun.client.aicp.{version}.client",
                fromlist=["AicpClient"],
            )
            print(f"  {INFO} SDK 版本: {version}")
            break
        except ImportError:
            continue

    if aicp_module is None:
        print(f"  {FAIL} 无法导入 ksyun.client.aicp")
        print(f"  请安装: pip install 'kingsoftcloud-sdk-python>=1.5.8.71'")
        sys.exit(1)

    cred = credential.Credential(AK, SK)

    http_profile = HttpProfile()
    http_profile.endpoint = ENDPOINT
    http_profile.reqMethod = "POST"
    http_profile.reqTimeout = 30
    http_profile.scheme = SCHEME

    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile

    client = aicp_module.AicpClient(cred, REGION, profile=client_profile)
    client._apiVersion = "2025-11-14"

    return client


# ============================================================
# Test 1: CreateMemorySdk 单条写入
# ============================================================

def test_create_memory_single(client):
    """单条消息写入测试"""
    section("Test 1: CreateMemorySdk (单条消息写入)")

    messages = [
        {"role": "user", "text": "SDK直接测试: 我叫张三，是一名Python开发工程师"},
    ]
    conversation = build_conversation(messages)

    params = {
        "Namespace": NAMESPACE,
        "UserId": "sdk_test_user",
        "Data": {"Conversation": conversation},
    }

    print(f"  请求参数:")
    print(f"    Namespace : {NAMESPACE}")
    print(f"    UserId    : sdk_test_user")
    print(f"    Conversation ({len(conversation)} 条):")
    for item in conversation:
        text = item["Content"][0]["Text"]
        print(f"      - Role={item['Role']}, Text='{text[:60]}'")
    print()

    try:
        resp = client.call("CreateMemorySdk", params, options={"IsPostJson": True})
        print(f"  {PASS} 单条写入成功!")
        print(f"  响应: {resp[:300]}")
        return True
    except Exception as e:
        err_msg = str(e)
        print(f"  {FAIL} 写入失败")
        print(f"  错误: {err_msg[:300]}")
        diagnose_error(err_msg)
        return False


# ============================================================
# Test 2: CreateMemorySdk 多条对话写入
# ============================================================

def test_create_memory_batch(client):
    """多条对话消息批量写入测试"""
    section("Test 2: CreateMemorySdk (多条对话写入)")

    messages = [
        {"role": "user", "text": "我喜欢旅行和摄影，最近去了日本京都"},
        {"role": "user", "text": "我的编程语言偏好是 Python 和 Go"},
        {"role": "user", "text": "我住在北京朝阳区，在一家互联网公司工作"},
    ]
    conversation = build_conversation(messages)

    params = {
        "Namespace": NAMESPACE,
        "UserId": "sdk_test_user",
        "Data": {"Conversation": conversation},
    }

    print(f"  请求参数:")
    print(f"    Namespace : {NAMESPACE}")
    print(f"    UserId    : sdk_test_user")
    print(f"    Conversation ({len(conversation)} 条):")
    for item in conversation:
        text = item["Content"][0]["Text"]
        print(f"      - Role={item['Role']}, Text='{text[:60]}'")
    print()

    try:
        resp = client.call("CreateMemorySdk", params, options={"IsPostJson": True})
        print(f"  {PASS} 批量写入成功! ({len(conversation)} 条消息)")
        print(f"  响应: {resp[:300]}")
        return True
    except Exception as e:
        err_msg = str(e)
        print(f"  {FAIL} 写入失败")
        print(f"  错误: {err_msg[:300]}")
        diagnose_error(err_msg)
        return False


# ============================================================
# Test 3: QueryMemorySdk 语义检索
# ============================================================

def test_query_memory(client):
    """语义检索测试"""
    section("Test 3: QueryMemorySdk (语义检索)")

    params = {
        "Namespace": NAMESPACE,
        "UserId": "sdk_test_user",
        "Query": "Python 编程",
        "Limit": 5,
    }

    print(f"  请求参数:")
    print(f"    Namespace : {NAMESPACE}")
    print(f"    UserId    : sdk_test_user")
    print(f"    Query     : Python 编程")
    print(f"    Limit     : 5")
    print()

    try:
        resp = client.call("QueryMemorySdk", params, options={"IsPostJson": True})
        print(f"  {PASS} 查询成功!")
        print(f"  原始响应: {resp[:500]}")

        # 解析响应
        try:
            data = json.loads(resp)
            memories = (
                data.get("Memories")
                or data.get("Data")
                or data.get("Results")
                or []
            )
            print(f"\n  解析到 {len(memories)} 条记忆:")
            for i, item in enumerate(memories):
                if isinstance(item, str):
                    print(f"    [{i + 1}] {item[:120]}")
                elif isinstance(item, dict):
                    text = (
                        item.get("Content")
                        or item.get("Text")
                        or item.get("Data")
                        or json.dumps(item, ensure_ascii=False)
                    )
                    print(f"    [{i + 1}] {str(text)[:120]}")
        except json.JSONDecodeError:
            print(f"  (响应非 JSON 格式)")

        return True
    except Exception as e:
        err_msg = str(e)
        print(f"  {FAIL} 查询失败")
        print(f"  错误: {err_msg[:300]}")
        diagnose_error(err_msg)
        return False


# ============================================================
# Test 4: SdkLTMBackend 封装层写入
# ============================================================

def test_sdk_backend_save():
    """通过 KsADK SdkLTMBackend 封装层写入记忆"""
    section("Test 4: SdkLTMBackend 封装层写入")

    try:
        from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend
    except ImportError as e:
        print(f"  {FAIL} 无法导入 SdkLTMBackend: {e}")
        return False

    backend = SdkLTMBackend(
        index="sdk_test",
        access_key=AK,
        secret_key=SK,
        endpoint=ENDPOINT,
        scheme=SCHEME,
        region=REGION,
        namespace=NAMESPACE,
    )
    print(f"  Backend: SdkLTMBackend")
    print(f"  endpoint={ENDPOINT}, namespace={NAMESPACE}")

    # 模拟 LongTermMemory 传入的 event_strings 格式 (JSON 序列化的 ADK event)
    event_strings = [
        json.dumps(
            {"role": "user", "parts": [{"text": "封装层测试: 我喜欢机器学习和深度学习"}]},
            ensure_ascii=False,
        ),
        json.dumps(
            {"role": "user", "parts": [{"text": "封装层测试: 最近在研究 Transformer 架构"}]},
            ensure_ascii=False,
        ),
    ]

    print(f"\n  写入 {len(event_strings)} 条事件:")
    for i, es in enumerate(event_strings):
        parsed = json.loads(es)
        text = parsed["parts"][0]["text"]
        print(f"    [{i + 1}] {text}")
    print()

    try:
        ok = backend.save_memory("sdk_test_user", event_strings)
        if ok:
            print(f"  {PASS} SdkLTMBackend.save_memory() 成功")
            return True
        else:
            print(f"  {FAIL} SdkLTMBackend.save_memory() 返回 False")
            return False
    except Exception as e:
        print(f"  {FAIL} SdkLTMBackend.save_memory() 异常: {e}")
        diagnose_error(str(e))
        return False


# ============================================================
# Test 5: SdkLTMBackend 封装层检索
# ============================================================

def test_sdk_backend_search():
    """通过 KsADK SdkLTMBackend 封装层检索记忆"""
    section("Test 5: SdkLTMBackend 封装层检索")

    try:
        from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend
    except ImportError as e:
        print(f"  {FAIL} 无法导入 SdkLTMBackend: {e}")
        return False

    backend = SdkLTMBackend(
        index="sdk_test",
        access_key=AK,
        secret_key=SK,
        endpoint=ENDPOINT,
        scheme=SCHEME,
        region=REGION,
        namespace=NAMESPACE,
    )

    query = "旅行 摄影"
    print(f"  Backend: SdkLTMBackend")
    print(f"  query='{query}', top_k=5")
    print()

    try:
        results = backend.search_memory("sdk_test_user", query, top_k=5)
        print(f"  {PASS} SdkLTMBackend.search_memory() 成功")
        print(f"  返回 {len(results)} 条结果:")
        for i, r in enumerate(results):
            print(f"    [{i + 1}] {str(r)[:120]}")

        if not results:
            print(f"  {INFO} 无结果 (记忆可能尚未索引完成)")

        return True
    except Exception as e:
        print(f"  {FAIL} SdkLTMBackend.search_memory() 异常: {e}")
        diagnose_error(str(e))
        return False


# ============================================================
# 主入口
# ============================================================

def main():
    section("金山云 AICP 记忆库 SDK 测试")

    # 检查配置
    print(f"  AK        : {AK[:12]}..." if AK else f"  {FAIL} AK 未配置")
    print(f"  SK        : {SK[:12]}..." if SK else f"  {FAIL} SK 未配置")
    print(f"  Endpoint  : {ENDPOINT}")
    print(f"  Scheme    : {SCHEME}")
    print(f"  Region    : {REGION}")
    print(f"  Namespace : {NAMESPACE}")
    print(f"  NO_PROXY  : {os.getenv('NO_PROXY', '(未设置)')}")

    if not AK or not SK:
        print(f"\n  {FAIL} 请在 .env 中配置 KSYUN_ACCESS_KEY 和 KSYUN_SECRET_KEY")
        return

    if not NAMESPACE:
        print(f"\n  {FAIL} 请在 .env 中配置 KSADK_LTM_NAMESPACE (记忆库 ID)")
        return

    # 创建客户端
    client = create_client()
    print(f"  {PASS} 客户端创建成功\n")

    # ---- 直接 SDK 调用测试 ----
    ok1 = test_create_memory_single(client)
    ok2 = test_create_memory_batch(client)
    ok3 = test_query_memory(client)

    # ---- KsADK 封装层测试 ----
    ok4 = test_sdk_backend_save()
    ok5 = test_sdk_backend_search()

    # ---- 汇总 ----
    section("测试结果汇总")
    results = [
        ("Test 1: CreateMemorySdk 单条写入", ok1),
        ("Test 2: CreateMemorySdk 多条写入", ok2),
        ("Test 3: QueryMemorySdk 语义检索", ok3),
        ("Test 4: SdkLTMBackend 封装层写入", ok4),
        ("Test 5: SdkLTMBackend 封装层检索", ok5),
    ]

    all_pass = True
    for name, ok in results:
        status = PASS if ok else FAIL
        print(f"  {status} {name}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print("  全部通过!")
    else:
        print("  存在失败项，请根据上方诊断信息排查")
    print()


if __name__ == "__main__":
    main()
