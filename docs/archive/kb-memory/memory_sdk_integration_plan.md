# 记忆库 SDK 集成计划

## Context

SDK 已更新到 1.5.8.71，在 AICP v20251114 client 中新增了两个记忆库 API：
- **`CreateMemorySdk`** — 向指定记忆库写入记忆
- **`QueryMemorySdk`** — 从记忆库检索记忆

当前项目安装的 SDK 是 1.5.8.70（v20251114 client 中只有知识库方法，没有记忆库方法）。
现有的 `HttpLTMBackend` 使用通用 HTTP 端点（标记 TODO），需要改为使用官方 SDK API。

本次任务：升级 SDK → 新增 SDK 记忆库后端 → 修改已有逻辑 → 更新测试。

---

## SDK 记忆库 API 签名

```python
# v20251114/client.py
class AicpClient:
    def CreateMemorySdk(self, request):  # 写入记忆
    def QueryMemorySdk(self, request):   # 检索记忆

# v20251114/models.py
class CreateMemorySdkRequest:
    Namespace: str       # 命名空间（隔离不同应用）
    UserId: str          # 用户 ID
    AgentId: str         # Agent ID
    SessionId: str       # 会话 ID
    SceneId: str         # 场景 ID
    DataType: str        # 数据类型
    Data: Object         # 记忆数据

class QueryMemorySdkRequest:
    Namespace: str       # 命名空间
    UserId: str          # 用户 ID
    Query: str           # 查询文本（语义检索）
    SceneId: str         # 场景 ID
    OccurredAfter: Long  # 时间过滤（起始时间戳）
    OccurredBefore: Long # 时间过滤（结束时间戳）
    Mode: str            # 检索模式
    ReturnCitations: bool # 是否返回引用
    Limit: int           # 返回数量
```

---

## Step 1: 升级 SDK 到 1.5.8.71

```bash
pip install kingsoftcloud-sdk-python==1.5.8.71
```

验证：
```bash
grep -r "CreateMemorySdk\|QueryMemorySdk" .venv/.../ksyun/client/aicp/v20251114/client.py
```

---

## Step 2: 新建 SDK 记忆库后端

**新建文件**: `ksadk/memory/adk/backends/sdk_ltm_backend.py`

参照 `ksadk/knowledge_base/client.py` 的模式（L97-155），关键设计：

```python
class SdkLTMBackend(BaseLongTermMemoryBackend):
    """金山云 AICP 记忆库 SDK 后端"""

    # 配置参数（参照 KnowledgeBaseClient 的字段模式）
    access_key: str = ""
    secret_key: str = ""
    region: str = "cn-north-vip1"
    endpoint: str = "aicp.api.ksyun.com"
    scheme: str = "https"
    namespace: str = ""      # 对应 API 的 Namespace 参数
    agent_id: str = ""       # 对应 API 的 AgentId
    scene_id: str = ""       # 对应 API 的 SceneId

    _aicp_client = None      # 懒加载

    def _get_client(self):
        """懒加载 AICP 客户端 - 复用 KnowledgeBaseClient._get_client() 的模式"""
        # 导入 ksyun.common.credential
        # 多版本 fallback: v20251114 → v20251212 → v20240612
        # 构建 HttpProfile + ClientProfile
        # 创建 AicpClient 实例
        # 设置 _apiVersion = "2025-11-14"

    def save_memory(self, user_id, event_strings, **kwargs):
        """调用 CreateMemorySdk 写入记忆"""
        # params = {
        #     "Namespace": self.namespace or self.index,
        #     "UserId": user_id,
        #     "AgentId": self.agent_id,
        #     "SessionId": kwargs.get("session_id", ""),
        #     "DataType": "text",
        #     "Data": event_strings (或 JSON 序列化)
        # }
        # client.call("CreateMemorySdk", params, options={"IsPostJson": True})

    def search_memory(self, user_id, query, top_k=5, **kwargs):
        """调用 QueryMemorySdk 检索记忆"""
        # params = {
        #     "Namespace": self.namespace or self.index,
        #     "UserId": user_id,
        #     "Query": query,
        #     "Limit": top_k,
        # }
        # response = client.call("QueryMemorySdk", params, options={"IsPostJson": True})
        # 解析响应，返回 List[str]
```

环境变量：
```
KSADK_LTM_BACKEND=sdk
KSADK_LTM_ACCESS_KEY=<AK>       # fallback to KSYUN_ACCESS_KEY
KSADK_LTM_SECRET_KEY=<SK>       # fallback to KSYUN_SECRET_KEY
KSADK_LTM_REGION=cn-north-vip1
KSADK_LTM_ENDPOINT=aicp.api.ksyun.com
KSADK_LTM_SCHEME=https
KSADK_LTM_NAMESPACE=<namespace> # 记忆库命名空间
KSADK_LTM_AGENT_ID=<agent_id>   # Agent ID
KSADK_LTM_SCENE_ID=<scene_id>   # 场景 ID
```

---

## Step 3: 修改已有逻辑

### 3a. `ksadk/memory/adk/long_term_memory.py`

- `_get_backend_cls()` 函数（L44-61）增加 `"sdk"` 分支：
  ```python
  if backend == "sdk":
      from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend
      return SdkLTMBackend
  ```
- `LongTermMemory` 类的 `backend` Literal type（L95-98）更新：
  ```python
  backend: Union[Literal["local", "http", "sdk"], BaseLongTermMemoryBackend]
  ```
- `from_env()` 方法（L274-303）增加 sdk 后端的配置读取

### 3b. `ksadk/runners/adk_runner.py`

- `_init_long_term_memory()` 方法（L107-148）增加 `backend == "sdk"` 分支：
  ```python
  elif backend == "sdk":
      backend_config = {
          "access_key": os.environ.get("KSADK_LTM_ACCESS_KEY") or os.environ.get("KSYUN_ACCESS_KEY", ""),
          "secret_key": os.environ.get("KSADK_LTM_SECRET_KEY") or os.environ.get("KSYUN_SECRET_KEY", ""),
          "region": os.environ.get("KSADK_LTM_REGION", "cn-north-vip1"),
          "endpoint": os.environ.get("KSADK_LTM_ENDPOINT", "aicp.api.ksyun.com"),
          "scheme": os.environ.get("KSADK_LTM_SCHEME", "https"),
          "namespace": os.environ.get("KSADK_LTM_NAMESPACE", ""),
          "agent_id": os.environ.get("KSADK_LTM_AGENT_ID", ""),
          "scene_id": os.environ.get("KSADK_LTM_SCENE_ID", ""),
      }
  ```

### 3c. `pyproject.toml`

- 更新 SDK 版本要求：`kingsoftcloud-sdk-python>=1.5.8.71`

---

## Step 4: 更新测试

**修改文件**: `examples/memory_demo_adk/test_memory_agent.py`

在已有 5 个测试基础上，修改/新增：

- **Test 1** 新增 1.5 子测试：SDK 后端连通性
  - 创建 `SdkLTMBackend` 实例（需要 AK/SK 配置）
  - 调用 `save_memory()` + `search_memory()` 验证
  - 如果 AK/SK 未配置，跳过并打印提示

- **更新 `.env.example`**: 增加 SDK 后端相关环境变量模板

---

## Step 5: 验证

1. SDK 升级验证：`pip show kingsoftcloud-sdk-python` → 1.5.8.71
2. 确认 SDK 有记忆库 API：`grep CreateMemorySdk ...v20251114/client.py`
3. 本地后端测试：`python test_memory_agent.py --local-only` → Test 1-2 通过
4. SDK 后端测试（需要 AK/SK）：`python test_memory_agent.py` → Test 1.5 SDK 连通性
5. 完整测试：配置 .env → 全部 5 个测试通过

---

## 关键文件

| 文件 | 操作 |
|------|------|
| `ksadk/memory/adk/backends/sdk_ltm_backend.py` | **新建** — SDK 记忆库后端 |
| `ksadk/memory/adk/long_term_memory.py` | **修改** — 增加 sdk 后端支持 (L44-61, L95-98, L274-303) |
| `ksadk/runners/adk_runner.py` | **修改** — 增加 sdk 后端环境变量处理 (L107-148) |
| `pyproject.toml` | **修改** — SDK 版本 ≥1.5.8.71 (L90) |
| `examples/memory_demo_adk/test_memory_agent.py` | **修改** — 新增 SDK 后端测试 |
| `examples/memory_demo_adk/.env.example` | **修改** — 增加 SDK 配置模板 |

## 参照模块

| 模块 | 路径 | 复用方式 |
|------|------|----------|
| `KnowledgeBaseClient._get_client()` | `ksadk/knowledge_base/client.py` L97-155 | AICP client 创建模式（credential, profile, multi-version fallback） |
| `KnowledgeBaseClient.search()` | `ksadk/knowledge_base/client.py` L207-239 | `client.call()` 调用 + 响应解析模式 |
| `BaseLongTermMemoryBackend` | `ksadk/memory/adk/backends/base_ltm_backend.py` | 新后端继承此基类 |
| `HttpLTMBackend` | `ksadk/memory/adk/backends/http_ltm_backend.py` | 结构参照（同样继承 BaseLongTermMemoryBackend） |
