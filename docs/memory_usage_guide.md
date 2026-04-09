# KsADK 记忆库使用与配置指南

> 主入口已迁移：完整 CLI 与平台能力说明请先看 [ksadk_usage_guide.md](./ksadk_usage_guide.md)。
>
> 本文保留为 ADK 记忆能力专项参考，重点放在记忆后端、ADK 自动注入和更细的 FAQ。

## 概述

KsADK 记忆库为 ADK Agent 提供跨会话的记忆能力，让 Agent 能"记住"用户之前的对话内容。记忆库分为两层：

| 层级 | 类 | 作用 | 数据生命周期 |
|------|-----|------|------------|
| **短期记忆** | `ShortTermMemory` | 管理单次会话的上下文（历史消息） | 会话内 |
| **长期记忆** | `LongTermMemory` | 跨会话持久化用户记忆，支持语义检索 | 跨会话 |

### 长期记忆后端对比

| 后端 | 环境变量值 | 存储 | 检索方式 | 适用场景 |
|------|----------|------|---------|---------|
| **local** | `KSADK_LTM_BACKEND=local` | 内存 (进程退出丢失) | 关键词匹配 | 开发测试 |
| **http** | `KSADK_LTM_BACKEND=http` | 远程 HTTP 服务 | 取决于服务端 | 自建记忆服务 |
| **sdk** | `KSADK_LTM_BACKEND=sdk` | 金山云 AICP 云端 | **语义检索** | **生产环境（推荐）** |

---

## 一、环境变量配置

### 1.1 短期记忆 (ShortTermMemory)

短期记忆管理会话的上下文消息，通常不需要额外配置（默认使用内存）。

| 环境变量 | 必填 | 默认值 | 说明 |
|----------|------|--------|------|
| `KSADK_STM_BACKEND` | 否 | — | 后端类型: `local` / `sqlite` / `database`。不设置则不启用 |
| `KSADK_STM_DB_PATH` | 否 | `/tmp/ksadk_local_database.db` | SQLite 文件路径 (backend=sqlite 时) |
| `KSADK_STM_DB_URL` | 否 | — | 数据库 URL (backend=database 时) |

**配置示例**:

```ini
# 内存模式（开发测试，重启丢失）
KSADK_STM_BACKEND=local

# SQLite 模式（本地持久化）
KSADK_STM_BACKEND=sqlite
KSADK_STM_DB_PATH=/tmp/ksadk.db

# 外部数据库模式（如 PostgreSQL）
KSADK_STM_BACKEND=database
KSADK_STM_DB_URL=postgresql+asyncpg://user:pass@host:5432/dbname
```

### 1.2 长期记忆 — 本地后端 (开发测试)

最简配置，适合开发调试：

```ini
KSADK_LTM_BACKEND=local
```

> **注意**: local 后端使用内存存储，进程退出后数据丢失，仅用于开发测试。

### 1.3 长期记忆 — 金山云 SDK 后端（生产推荐）

通过金山云 AICP 记忆库 API（`CreateMemorySdk` / `QueryMemorySdk`）实现云端持久化和语义检索。

#### 环境变量

| 环境变量 | 必填 | 默认值 | 说明 |
|----------|------|--------|------|
| `KSADK_LTM_BACKEND` | **是** | — | 设置为 `sdk` |
| `KSADK_LTM_ACCESS_KEY` | **是** | — | 金山云 AK（可复用 `KSYUN_ACCESS_KEY`） |
| `KSADK_LTM_SECRET_KEY` | **是** | — | 金山云 SK（可复用 `KSYUN_SECRET_KEY`） |
| `KSADK_LTM_NAMESPACE` | 建议填 | — | 记忆库命名空间，用于隔离不同应用的记忆 |
| `KSADK_LTM_REGION` | 否 | `cn-north-vip1` | API 区域 |
| `KSADK_LTM_ENDPOINT` | 否 | `aicp.api.ksyun.com` | API 端点 |
| `KSADK_LTM_SCHEME` | 否 | `https` | 协议（内网用 `http`） |
| `KSADK_LTM_AGENT_ID` | 否 | — | Agent ID（可选标识） |
| `KSADK_LTM_SCENE_ID` | 否 | — | 场景 ID（可选标识） |
| `KSADK_LTM_TOP_K` | 否 | `5` | 检索返回的最大条数 |
| `KSADK_LTM_INDEX` | 否 | Agent name | 索引名称 |

#### .env 配置示例

**外网访问**:

```ini
KSADK_LTM_BACKEND=sdk
KSADK_LTM_ACCESS_KEY=你的AccessKey
KSADK_LTM_SECRET_KEY=你的SecretKey
KSADK_LTM_NAMESPACE=my_app_memory
KSADK_LTM_ENDPOINT=aicp.api.ksyun.com
KSADK_LTM_SCHEME=https
KSADK_LTM_TOP_K=5
```

**内网访问**（推荐，延迟更低）:

```ini
KSADK_LTM_BACKEND=sdk
KSADK_LTM_ACCESS_KEY=你的AccessKey
KSADK_LTM_SECRET_KEY=你的SecretKey
KSADK_LTM_NAMESPACE=my_app_memory
KSADK_LTM_ENDPOINT=aicp.inner.api.ksyun.com
KSADK_LTM_SCHEME=http
KSADK_LTM_TOP_K=5
```

**复用已有 AK/SK**:

如果已经为知识库配置了 `KSYUN_ACCESS_KEY` / `KSYUN_SECRET_KEY`，记忆库会自动复用，无需重复配置：

```ini
# 全局 AK/SK（知识库和记忆库共用）
KSYUN_ACCESS_KEY=你的AccessKey
KSYUN_SECRET_KEY=你的SecretKey

# 记忆库配置
KSADK_LTM_BACKEND=sdk
KSADK_LTM_NAMESPACE=my_app_memory
# 无需单独配置 KSADK_LTM_ACCESS_KEY/SECRET_KEY
```

#### AK/SK 获取方式

1. 登录 [金山云控制台](https://console.ksyun.com)
2. 进入 **IAM** > **访问密钥管理**
3. 创建或查看已有的 AccessKey / SecretKey

### 1.4 长期记忆 — HTTP 后端

连接自建的远程记忆服务：

| 环境变量 | 必填 | 默认值 | 说明 |
|----------|------|--------|------|
| `KSADK_LTM_BACKEND` | **是** | — | 设置为 `http` |
| `KSADK_LTM_HTTP_URL` | **是** | — | 记忆服务 HTTP 地址 |
| `KSADK_LTM_HTTP_TOKEN` | 否 | — | 认证 Token |
| `KSADK_LTM_TOP_K` | 否 | `5` | 检索返回条数 |

```ini
KSADK_LTM_BACKEND=http
KSADK_LTM_HTTP_URL=https://your-memory-service.com/api/v1
KSADK_LTM_HTTP_TOKEN=your-auth-token
```

---

## 二、零代码集成（推荐）

### 2.1 agentengine run 自动注入

只需配置环境变量，Agent 代码中不需要导入任何记忆相关的模块。ADKRunner 会自动完成初始化和工具注入。

**agent.py** — 无需修改，tools 可留空：

```python
import os
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)

# tools 为空 — load_memory 工具会被 ADKRunner 自动注入
root_agent = Agent(
    name="memory_assistant",
    model=model,
    description="有记忆的智能助手",
    instruction="""你是一个有记忆能力的智能助手。
你能记住用户之前告诉你的信息。
新对话开始时，使用 load_memory 工具回忆之前的对话内容。""",
    tools=[],
)
```

**.env** — 配置记忆库：

```ini
# 模型
OPENAI_API_KEY=你的API_KEY
OPENAI_API_BASE=http://kspmas.ksyun.com/v1
MODEL_NAME=deepseek-v3.2

# 长期记忆（金山云 SDK 后端）
KSADK_LTM_BACKEND=sdk
KSYUN_ACCESS_KEY=你的AccessKey
KSYUN_SECRET_KEY=你的SecretKey
KSADK_LTM_NAMESPACE=my_app_memory
KSADK_LTM_ENDPOINT=aicp.inner.api.ksyun.com
KSADK_LTM_SCHEME=http
```

**运行**：

```bash
agentengine run .
```

ADKRunner 自动完成：
1. 读取 `KSADK_LTM_BACKEND=sdk` → 初始化 `SdkLTMBackend` + `LongTermMemory`
2. 将 `LongTermMemory` 作为 `memory_service` 传给 Runner
3. 自动注入 `load_memory` 工具到 Agent 的 tools 列表
4. Agent 每次对话时可通过 `load_memory` 检索历史记忆

### 2.2 自动注入机制对照表

当通过 `agentengine run .` 启动时，ADKRunner 根据环境变量自动完成初始化和工具注入：

| 环境变量 | 触发行为 | 注入的工具 |
|----------|---------|-----------|
| `KSADK_STM_BACKEND` 存在 | 初始化 ShortTermMemory SessionService | (无工具，替换 SessionService) |
| `KSADK_LTM_BACKEND` 存在 | 初始化 LongTermMemory MemoryService | `load_memory` |
| `KSADK_KB_DATASET_ID` 存在 | 初始化知识库客户端 | `search_knowledge_base` |

**三者可独立使用，也可同时启用，互不冲突。**

---

## 三、显式代码集成

如果你需要在代码中手动控制记忆库的初始化和使用，可以采用以下方式：

### 3.1 手动初始化 + 工具添加

```python
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.tools import load_memory
from google.genai import types

from ksadk.memory.adk import LongTermMemory, ShortTermMemory

model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)

# 初始化记忆体
stm = ShortTermMemory(backend="local")
ltm = LongTermMemory(backend="local", app_name="my_agent")

# 显式添加 load_memory 工具
agent = Agent(
    name="my_agent",
    model=model,
    description="有记忆的智能助手",
    instruction="先用 load_memory 检索记忆，基于记忆回答用户问题。",
    tools=[load_memory],
)

# Runner — 传入 memory_service
runner = Runner(
    agent=agent,
    session_service=stm.session_service,
    memory_service=ltm,       # 关键: 传入长期记忆
    app_name="my_agent",
)


async def main():
    # Session 1: 存入信息
    session_1 = await stm.create_session(
        app_name="my_agent", user_id="user1", session_id="s1"
    )
    msg = types.Content(role="user", parts=[types.Part(text="我叫张三，喜欢Python")])
    async for event in runner.run_async(
        user_id="user1", session_id=session_1.id, new_message=msg
    ):
        pass  # 收集回复

    # 保存到长期记忆
    completed = await stm.session_service.get_session(
        app_name="my_agent", user_id="user1", session_id=session_1.id
    )
    await ltm.add_session_to_memory(completed)

    # Session 2: 新会话中检索记忆
    session_2 = await stm.create_session(
        app_name="my_agent", user_id="user1", session_id="s2"
    )
    msg2 = types.Content(role="user", parts=[types.Part(text="你还记得我叫什么吗？")])
    async for event in runner.run_async(
        user_id="user1", session_id=session_2.id, new_message=msg2
    ):
        if event.content and event.content.parts:
            if hasattr(event.content.parts[0], "text") and event.content.parts[0].text:
                print(event.content.parts[0].text)

asyncio.run(main())
```

### 3.2 from_env() 快速创建（SDK 后端）

```python
from ksadk.memory.adk import LongTermMemory

# 自动读取 KSADK_LTM_* 环境变量
ltm = LongTermMemory.from_env()
```

等价于手动指定：

```python
ltm = LongTermMemory(
    backend="sdk",
    backend_config={
        "access_key": "你的AK",
        "secret_key": "你的SK",
        "region": "cn-north-vip1",
        "endpoint": "aicp.inner.api.ksyun.com",
        "scheme": "http",
        "namespace": "my_app_memory",
    },
    app_name="my_agent",
    top_k=5,
)
```

---

## 四、记忆库 + 知识库联合使用

### 4.1 零代码方式（推荐）

只需在 `.env` 中同时配置记忆库和知识库的环境变量：

```ini
# ============ 模型 ============
OPENAI_API_KEY=你的API_KEY
OPENAI_API_BASE=http://kspmas.ksyun.com/v1
MODEL_NAME=deepseek-v3.2

# ============ 全局 AK/SK ============
KSYUN_ACCESS_KEY=你的AccessKey
KSYUN_SECRET_KEY=你的SecretKey

# ============ 知识库 ============
KSADK_KB_DATASET_ID=你的知识库ID
KSADK_KB_ENDPOINT=aicp.inner.api.ksyun.com
KSADK_KB_SCHEME=http

# ============ 长期记忆 ============
KSADK_LTM_BACKEND=sdk
KSADK_LTM_NAMESPACE=my_app_memory
KSADK_LTM_ENDPOINT=aicp.inner.api.ksyun.com
KSADK_LTM_SCHEME=http
```

**agent.py**:

```python
import os
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)

root_agent = Agent(
    name="smart_assistant",
    model=model,
    description="具备知识库检索和记忆能力的智能助手",
    instruction="""你是一个智能助手，同时具备知识库检索和记忆能力。

## 工具使用
- **search_knowledge_base**: 查询企业知识库中的文档内容
- **load_memory**: 回忆之前与该用户的对话内容

## 回答原则
- 知识库的信息用于回答专业问题，标注来源文档
- 记忆中的信息用于个性化服务（如称呼用户、记住偏好）
""",
    tools=[],  # search_knowledge_base + load_memory 都会自动注入
)
```

```bash
agentengine run .
```

### 4.2 显式导入方式

```python
from google.adk.tools import load_memory
from ksadk.knowledge_base.adk_tool import search_knowledge_base
from ksadk.memory.adk import LongTermMemory

ltm = LongTermMemory.from_env()

agent = Agent(
    name="full_agent",
    model=model,
    instruction="综合使用 load_memory 和 search_knowledge_base 回答。",
    tools=[load_memory, search_knowledge_base],
)

runner = Runner(
    agent=agent,
    session_service=session_service,
    memory_service=ltm,
    app_name="full_agent",
)
```

---

## 五、后端选型建议

| 场景 | 推荐后端 | 理由 |
|------|---------|------|
| 本地开发/单元测试 | `local` | 无需外部依赖，即开即用 |
| 功能验证/Demo 演示 | `local` 或 `sqlite`(STM) | 轻量级，快速验证 |
| 小规模生产（单实例） | `sdk` | 云端持久化，语义检索，无需运维 |
| 大规模生产（多实例） | `sdk` | 云端存储，多实例共享，支持命名空间隔离 |
| 自建记忆服务 | `http` | 完全自主可控 |

### 各后端优缺点

| 后端 | 优点 | 缺点 |
|------|------|------|
| **local** | 无依赖、速度快、简单 | 数据不持久、仅关键词匹配 |
| **sdk** | 语义检索、云端持久化、免运维 | 需要 AK/SK、网络依赖 |
| **http** | 灵活可控、可自定义存储 | 需自建服务 |

---

## 六、常见问题 FAQ

### Q1: SDK 后端连接失败，报 InnerAccountCanOnlyAccessThroughIntranet

**原因**: 使用了内网账号但通过外网端点访问。

**解决**: 修改端点配置为内网地址：
```ini
KSADK_LTM_ENDPOINT=aicp.inner.api.ksyun.com
KSADK_LTM_SCHEME=http
```

### Q2: 记忆没有被保存到长期记忆

**排查步骤**:
1. 确认 `KSADK_LTM_BACKEND` 环境变量已设置
2. 检查 Session 中是否有用户消息（LTM 只保存 `author="user"` 的消息，model 回复和 function call 会被过滤）
3. 查看日志中是否有 `Saving N events to long term memory` 的输出

### Q3: load_memory 工具没有被自动注入到 Agent

**排查步骤**:
1. 确认 `KSADK_LTM_BACKEND` 环境变量已设置
2. 确认通过 `agentengine run .` 启动（直接 `python agent.py` 不会触发自动注入）
3. 查看启动日志中是否有 `Injected 'load_memory' tool into agent` 的输出

### Q4: 不同用户的记忆会互相看到吗？

不会。记忆库通过 `user_id` 进行隔离：
- `save_memory()` 和 `search_memory()` 都以 `user_id` 为维度
- 用户 A 的记忆不会出现在用户 B 的检索结果中
- SDK 后端还支持通过 `namespace` 进行应用级别的隔离

### Q5: 记忆库需要额外安装依赖吗？

| 后端 | 额外依赖 |
|------|---------|
| `local` | 无（内置） |
| `http` | `httpx`（已包含在 ksadk 核心依赖中） |
| `sdk` | `kingsoftcloud-sdk-python>=1.5.8.71`（通过 `pip install ksadk[kb]` 安装） |

### Q6: SDK 后端写入后立即查询返回空结果

这是正常现象。AICP 记忆库服务在写入后需要短暂时间完成索引，通常几秒到十几秒后即可检索到。在实际使用场景中，写入和检索通常不在同一次对话中发生，因此不影响使用。
