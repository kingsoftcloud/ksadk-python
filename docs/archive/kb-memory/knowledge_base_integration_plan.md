# KsADK 知识库集成实现方案

## Context

用户在使用 KsADK 开发 Agent 时，需要方便地接入金山云知识库服务进行 RAG 检索。当前每次使用知识库都需要手动编写 API 调用代码。本次集成目标是：用户仅需配置环境变量（AK/SK + 知识库ID），即可自动获得知识库检索能力，无需重复编码。

**API**: 金山云 AICP `RetrieveKnowledge` (版本 2025-11-14，endpoint: `aicp.api.ksyun.com`)
**认证**: kingsoftcloud SDK，AK/SK 签名
**框架**: ADK、LangGraph、LangChain 三种框架均需支持
**操作**: 仅检索/查询

---

## 环境变量设计

```bash
# 必填 - 启用知识库
KSADK_KB_DATASET_ID=your-knowledge-base-id    # 知识库 ID (DatasetId)

# 认证 - 复用已有的 KSYUN_ACCESS_KEY / KSYUN_SECRET_KEY
# 如需单独配置知识库的AK/SK，可使用：
KSADK_KB_ACCESS_KEY=xxx                        # 可选，默认取 KSYUN_ACCESS_KEY
KSADK_KB_SECRET_KEY=xxx                        # 可选，默认取 KSYUN_SECRET_KEY

# 可选 - 检索参数
KSADK_KB_REGION=cn-north-vip1                  # 区域 (默认 cn-north-vip1)
KSADK_KB_ENDPOINT=aicp.api.ksyun.com           # API endpoint (默认 aicp.api.ksyun.com)
KSADK_KB_TOP_K=5                               # 返回结果数 (默认 5)
KSADK_KB_SEARCH_METHOD=intelligence_search     # 检索方法 (默认 intelligence_search)
KSADK_KB_SCORE_THRESHOLD=0.5                   # 分数阈值 (可选)
KSADK_KB_RERANKING_ENABLE=false                # 是否启用重排序 (默认 false)
```

判断逻辑：当 `KSADK_KB_DATASET_ID` 存在时，自动启用知识库能力。

---

## 文件结构

```
ksadk/knowledge_base/
├── __init__.py                    # 导出 KnowledgeBaseService, create_kb_tool
├── client.py                      # 核心: AICP RetrieveKnowledge 客户端
├── tool.py                        # 通用 search_knowledge 函数 (跨框架)
├── adk_tool.py                    # ADK FunctionTool 包装
└── langchain_tool.py              # LangChain BaseTool 包装
```

---

## 实现步骤

### Step 1: 知识库客户端 `ksadk/knowledge_base/client.py`

封装 AICP RetrieveKnowledge API 调用。使用 kingsoftcloud SDK 的 `call()` 方法。

```python
class KnowledgeBaseClient(BaseModel):
    """知识库检索客户端"""
    dataset_id: str            # 知识库 ID
    access_key: str = ""       # AK
    secret_key: str = ""       # SK
    region: str = "cn-north-vip1"
    endpoint: str = "aicp.api.ksyun.com"
    top_k: int = 5
    search_method: str = "intelligence_search"
    score_threshold: float = 0.0
    score_threshold_enabled: bool = False
    reranking_enable: bool = False

    def search(self, query: str, top_k: int = None) -> list[dict]:
        """检索知识库，返回匹配的文档片段列表"""
        # 使用 ksyun SDK call("RetrieveKnowledge", params)
        # 返回格式化的结果: [{content, score, document_name, segment_id}, ...]

    @classmethod
    def from_env(cls) -> "KnowledgeBaseClient":
        """从环境变量创建实例"""
```

**关键实现**:
- 使用 `ksyun.common.credential.Credential(ak, sk)` 认证
- 使用 `ksyun.client.aicp` 的 client（尝试导入 v20251114，不存在则 fallback 到已有版本）
- AK/SK 优先取 `KSADK_KB_ACCESS_KEY`，fallback 到 `KSYUN_ACCESS_KEY` / `KSYUN_SECRET_KEY`
- 结果解析: 从 Records[].Segment.Content + Score 提取有用信息

**依赖**: `kingsoftcloud-sdk-python` (新增可选依赖)

### Step 2: 通用检索函数 `ksadk/knowledge_base/tool.py`

提供一个简单的函数，可被任何框架使用：

```python
def search_knowledge(query: str, top_k: int = None) -> str:
    """检索知识库

    从环境变量自动配置的知识库中检索相关内容。
    返回格式化的文本结果，供 LLM 参考。
    """
    client = _get_or_create_client()  # 单例，懒加载
    results = client.search(query, top_k)
    return _format_results(results)
```

### Step 3: ADK 工具包装 `ksadk/knowledge_base/adk_tool.py`

将 `search_knowledge` 包装为 ADK 可用的 FunctionTool：

```python
from google.adk.tools import FunctionTool

def _search_knowledge_for_adk(query: str) -> dict:
    """搜索知识库获取相关信息"""
    result = search_knowledge(query)
    return {"result": result}

search_knowledge_tool = FunctionTool(func=_search_knowledge_for_adk)
```

### Step 4: LangChain 工具包装 `ksadk/knowledge_base/langchain_tool.py`

```python
from langchain_core.tools import tool

@tool
def search_knowledge_base(query: str) -> str:
    """搜索知识库获取相关信息。当需要查找专业知识或文档内容时使用此工具。"""
    return search_knowledge(query)
```

### Step 5: ADKRunner 集成 `ksadk/runners/adk_runner.py`

参考 `_init_long_term_memory()` 和 `_inject_load_memory_tool()` 的模式：

新增方法:
- `_init_knowledge_base()`: 检测 `KSADK_KB_DATASET_ID` 环境变量，初始化 client
- `_inject_search_knowledge_tool()`: 自动注入 search_knowledge 工具到 agent

在 `load_agent()` 中添加:
```python
# 初始化知识库 (从环境变量)
self._knowledge_base = self._init_knowledge_base()
if self._knowledge_base:
    self._inject_search_knowledge_tool()
```

### Step 6: LangGraphRunner / LangChainRunner 集成

由于 LangGraph 的 CompiledGraph 和 LangChain 的 Agent/Chain 在编译后无法轻松注入新工具，采用**模块级自动注册**方式：

在 `load_agent()` 中：
```python
def load_agent(self) -> None:
    self._agent, self._module = load_agent_module(...)
    # 将 kb_tool 注入到用户模块的命名空间中
    self._inject_kb_to_module()
```

`_inject_kb_to_module()` 会检测环境变量，如果 KB 已配置，则在用户模块中注入 `kb_tool` 变量，供用户在构建 graph/chain 时引用。

**但更实际的方案**是：为 LangGraph/LangChain 用户提供一行 import 即可使用的工具：

```python
# 用户的 agent.py 中
from ksadk.knowledge_base import search_knowledge_base  # LangChain tool
tools = [search_knowledge_base, ...]  # 直接使用，自动从env var读取配置
```

### Step 7: 依赖与包配置 `pyproject.toml`

```toml
[project.optional-dependencies]
kb = [
    "kingsoftcloud-sdk-python>=1.5.0",
]
```

### Step 8: `__init__.py` 导出

```python
# ksadk/knowledge_base/__init__.py
from ksadk.knowledge_base.client import KnowledgeBaseClient
from ksadk.knowledge_base.tool import search_knowledge

def create_langchain_tool():
    from ksadk.knowledge_base.langchain_tool import search_knowledge_base
    return search_knowledge_base

def create_adk_tool():
    from ksadk.knowledge_base.adk_tool import search_knowledge_tool
    return search_knowledge_tool
```

---

## 需要修改的文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `ksadk/knowledge_base/__init__.py` | **新建** | 模块入口，导出公共接口 |
| `ksadk/knowledge_base/client.py` | **新建** | AICP RetrieveKnowledge 客户端 |
| `ksadk/knowledge_base/tool.py` | **新建** | 通用 search_knowledge 函数 |
| `ksadk/knowledge_base/adk_tool.py` | **新建** | ADK FunctionTool 包装 |
| `ksadk/knowledge_base/langchain_tool.py` | **新建** | LangChain Tool 包装 |
| `ksadk/runners/adk_runner.py` | **修改** | 新增 `_init_knowledge_base()` + `_inject_search_knowledge_tool()` |
| `pyproject.toml` | **修改** | 新增 `kb` 可选依赖 |

## 复用的已有代码

- `ksadk/common/auth.py`: `AWSV4Auth` 类 — 作为 fallback 认证方式（如 SDK 不可用时）
- `ksadk/memory/adk/long_term_memory.py`: 整体设计模式参考（Pydantic BaseModel + from_env + 后端抽象）
- `ksadk/runners/adk_runner.py`: `_inject_load_memory_tool()` 模式 — 工具自动注入参考

---

## 验证方案

### 1. 单元测试
```bash
# 测试 KnowledgeBaseClient 初始化和参数构建
pytest tests/test_knowledge_base.py -v
```

### 2. 集成测试（需要真实 AK/SK 和知识库）
```bash
# 设置环境变量
export KSYUN_ACCESS_KEY=xxx
export KSYUN_SECRET_KEY=xxx
export KSADK_KB_DATASET_ID=your-kb-id

# ADK Agent 测试
cd examples/smart_assistant_adk
agentengine run .

# 在对话中验证知识库工具是否被自动注入和调用
```

### 3. LangChain/LangGraph 测试
```python
# 验证工具可以独立使用
from ksadk.knowledge_base import search_knowledge
result = search_knowledge("你的查询")
print(result)
```
