# KsADK 知识库与记忆体 — 代码示例详解

本文档提供知识库（RAG 检索）和记忆体（短期/长期记忆）的完整代码示例

---

## 一、知识库（Knowledge Base）

###  ADK Agent + 知识库（显式导入）

创建一个 Agent，手动将 `search_knowledge_base` 工具加入 tools 列表。

```python
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()  # 从 .env 加载配置

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# 显式导入知识库工具
from ksadk.knowledge_base.adk_tool import search_knowledge_base

# 模型
model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)

# Agent — 显式添加知识库工具
agent = Agent(
    name="kb_assistant",
    model=model,
    description="知识库问答助手",
    instruction="""你是一个知识库问答助手。
当用户提问时，先调用 search_knowledge_base 工具检索知识库，
然后基于检索结果回答。回答中标注信息来源文档名。用中文回答。""",
    tools=[search_knowledge_base],
)

# Runner
session_service = InMemorySessionService()
runner = Runner(agent=agent, session_service=session_service, app_name="kb_demo")


async def chat(question: str):
    session = await session_service.create_session(app_name="kb_demo", user_id="user1")
    msg = types.Content(role="user", parts=[types.Part(text=question)])

    final = ""
    async for event in runner.run_async(
        user_id="user1", session_id=session.id, new_message=msg
    ):
        if (
            event.content
            and event.content.parts
            and hasattr(event.content.parts[0], "text")
            and event.content.parts[0].text
        ):
            if not getattr(event.content.parts[0], "thought", False):
                final = event.content.parts[0].text.strip()

    print(f"Q: {question}")
    print(f"A: {final}")


asyncio.run(chat("如何配置知识库？"))
```

### ADK Agent + 知识库（自动注入，零代码）

Agent 代码中不导入任何知识库工具，只要环境变量 `KSADK_KB_DATASET_ID` 存在，通过 `agentengine run` 启动时 ADKRunner 会自动注入。

**agent.py：**

```python
import os
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)

# 注意: tools 为空，知识库工具会被 ADKRunner 自动注入
root_agent = Agent(
    name="my_agent",
    model=model,
    description="智能助手",
    instruction="当用户提问时，使用 search_knowledge_base 工具检索知识库并回答。",
    tools=[],
)
```

**.env：**

```ini
OPENAI_API_KEY=xxx
OPENAI_API_BASE=http://kspmas.ksyun.com/v1
KSADK_KB_DATASET_ID=你的知识库ID    # 只要这个存在，工具就自动注入
KSYUN_ACCESS_KEY=你的AK
KSYUN_SECRET_KEY=你的SK
```

**运行：**

```bash
agentengine run .
```

---

## 二、记忆体（Memory）

### 2.1 短期记忆 — 会话管理

短期记忆管理单次对话的上下文（历史消息），由 `ShortTermMemory` 类封装。

#### 内存模式（开发测试）

```python
from ksadk.memory.adk.short_term_memory import ShortTermMemory

# 内存模式 — 重启后数据丢失
stm = ShortTermMemory(backend="local")

# 创建会话
import asyncio

async def demo_stm():
    session = await stm.create_session(
        app_name="my_app",
        user_id="user1",
    )
    print(f"Session ID: {session.id}")

    # 获取已有会话（传入相同 session_id）
    same_session = await stm.create_session(
        app_name="my_app",
        user_id="user1",
        session_id=session.id,
    )
    print(f"Same session: {same_session.id == session.id}")  # True

asyncio.run(demo_stm())
```



### 2.2 长期记忆 — 跨会话记忆

长期记忆在会话结束后保存关键信息，在新会话开始时检索历史记忆。

#### 内存模式（开发测试）

```python
import asyncio
from ksadk.memory.adk.long_term_memory import LongTermMemory

ltm = LongTermMemory(backend="local", app_name="my_app")

async def demo_ltm():
    # 手动保存记忆（通常由 ADKRunner 自动完成）
    ltm._get_backend().save_memory(
        user_id="user1",
        event_strings=[
            '{"role": "user", "parts": [{"text": "我喜欢用 Python 写代码"}]}',
            '{"role": "user", "parts": [{"text": "我的项目是一个 AI Agent 框架"}]}',
        ],
    )

    # 检索记忆
    memories = ltm._get_backend().search_memory(
        user_id="user1",
        query="用户喜欢什么编程语言",
        top_k=3,
    )
    for m in memories:
        print(f"  记忆: {m}")

asyncio.run(demo_ltm())
```

#### SDK 模式（金山云 AICP 记忆库，生产推荐）

通过金山云 AICP 记忆库 API 实现云端持久化和语义检索：

```python
from ksadk.memory.adk.long_term_memory import LongTermMemory

# 方式 1: 手动指定配置
ltm = LongTermMemory(
    backend="sdk",
    backend_config={
        "access_key": "你的AK",
        "secret_key": "你的SK",
        "region": "cn-north-vip1",
        "endpoint": "aicp.inner.api.ksyun.com",  # 内网
        "scheme": "http",
        "namespace": "my_app_memory",             # 命名空间隔离
    },
    app_name="my_app",
    top_k=5,
)

# 方式 2: 从环境变量自动配置（推荐）
# 需设置: KSADK_LTM_BACKEND=sdk, KSYUN_ACCESS_KEY, KSYUN_SECRET_KEY 等
ltm = LongTermMemory.from_env()
```

#### HTTP 远程模式（自建服务）

```python
from ksadk.memory.adk.long_term_memory import LongTermMemory

ltm = LongTermMemory(
    backend="http",
    backend_config={
        "base_url": "https://your-memory-service.com",
        "token": "your-auth-token",
    },
    app_name="my_app",
    top_k=5,
)
```

#### 从环境变量创建

```python
import os
os.environ["KSADK_LTM_BACKEND"] = "sdk"   # 或 "local" / "http"

# SDK 后端配置（复用全局 AK/SK 或单独设置）
# os.environ["KSYUN_ACCESS_KEY"] = "你的AK"
# os.environ["KSYUN_SECRET_KEY"] = "你的SK"
# os.environ["KSADK_LTM_NAMESPACE"] = "my_app_memory"
# os.environ["KSADK_LTM_ENDPOINT"] = "aicp.inner.api.ksyun.com"
# os.environ["KSADK_LTM_SCHEME"] = "http"

os.environ["KSADK_LTM_TOP_K"] = "5"

from ksadk.memory.adk.long_term_memory import LongTermMemory

ltm = LongTermMemory.from_env()
```

### 2.3 记忆体自动注入（零代码，agentengine run）

和知识库一样，只要在 `.env` 中配置了对应的环境变量，ADKRunner 会自动完成所有初始化和注入。


**agent.py：**

```python
import os
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)

# tools 为空 — load_memory 工具会被自动注入
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

**运行：**

```bash
agentengine run .
```

ADKRunner 自动完成：
1. 初始化 `ShortTermMemory` (SQLite) → 替换默认 SessionService
2. 初始化 `LongTermMemory` (local) → 注入为 Runner 的 memory_service
3. 自动注入 `load_memory` 工具到 Agent

---

## 三、知识库 + 记忆体 同时使用

知识库和记忆体可以同时启用，Agent 会同时拥有 `search_knowledge_base` 和 `load_memory` 两个工具。

### 3.1 零代码方式（推荐）

**.env（完整配置）：**

```ini
# ============ 模型 ============
OPENAI_API_KEY=你的API_KEY
OPENAI_API_BASE=http://kspmas.ksyun.com/v1
MODEL_NAME=deepseek-v3.2

# ============ 知识库 ============
KSADK_KB_DATASET_ID=你的知识库ID
KSYUN_ACCESS_KEY=你的AK
KSYUN_SECRET_KEY=你的SK
KSADK_KB_ENDPOINT=aicp.inner.api.ksyun.com
KSADK_KB_SCHEME=http


# ============ 长期记忆 ============
KSADK_LTM_BACKEND=local
KSADK_LTM_TOP_K=5
```

**agent.py：**

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
  当用户问到产品功能、操作方法、技术文档等问题时使用
- **load_memory**: 回忆之前与该用户的对话内容
  新对话开始时使用，了解用户背景和历史偏好

## 回答原则

- 知识库的信息用于回答专业问题，标注来源文档
- 记忆中的信息用于个性化服务（如称呼用户、记住偏好）
- 如果两者都没有相关信息，如实告知
""",
    tools=[],  # search_knowledge_base + load_memory 都会自动注入
)
```

**运行后，ADKRunner 自动：**


### 3.2 显式导入方式

如果你想在代码中明确控制，可以手动导入两个工具：

```python
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import load_memory
from google.genai import types

from ksadk.knowledge_base.adk_tool import search_knowledge_base
from ksadk.memory.adk.long_term_memory import LongTermMemory

model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)

# 显式添加两个工具
agent = Agent(
    name="full_agent",
    model=model,
    description="知识库+记忆的完整 Agent",
    instruction="先用 load_memory 检索记忆，再用 search_knowledge_base 检索知识库，综合回答。",
    tools=[
        search_knowledge_base,  # 知识库检索
        load_memory,            # 长期记忆检索
    ],
)

# 长期记忆
ltm = LongTermMemory.from_env(agent_name="full_agent")

# Runner — 传入 memory_service
session_service = InMemorySessionService()
runner = Runner(
    agent=agent,
    session_service=session_service,
    app_name="full_agent",
    memory_service=ltm,  # 关键: 传入长期记忆
)


async def chat(question: str):
    session = await session_service.create_session(app_name="full_agent", user_id="user1")
    msg = types.Content(role="user", parts=[types.Part(text=question)])

    final = ""
    async for event in runner.run_async(
        user_id="user1", session_id=session.id, new_message=msg
    ):
        if (
            event.content
            and event.content.parts
            and hasattr(event.content.parts[0], "text")
            and event.content.parts[0].text
        ):
            if not getattr(event.content.parts[0], "thought", False):
                final = event.content.parts[0].text.strip()

    print(f"Q: {question}")
    print(f"A: {final}")


asyncio.run(chat("如何部署 Agent？之前我问过什么？"))
```

---

## 四、环境变量速查表

### 知识库

| 环境变量 | 必填 | 默认值 | 说明 |
|----------|------|--------|------|
| `KSADK_KB_DATASET_ID` | 是 | — | 知识库 ID，设置后自动启用 |
| `KSYUN_ACCESS_KEY` | 是 | — | 金山云 AK |
| `KSYUN_SECRET_KEY` | 是 | — | 金山云 SK |
| `KSADK_KB_ENDPOINT` | 否 | `aicp.api.ksyun.com` | API 端点 |
| `KSADK_KB_SCHEME` | 否 | `https` | 协议 (`http` for 内网) |
| `KSADK_KB_REGION` | 否 | `cn-north-vip1` | 区域 |
| `KSADK_KB_TOP_K` | 否 | `5` | 返回结果数 |
| `KSADK_KB_SEARCH_METHOD` | 否 | `intelligence_search` | 检索方法 |
| `KSADK_KB_SCORE_THRESHOLD` | 否 | — | 分数阈值 |
| `KSADK_KB_RERANKING_ENABLE` | 否 | `false` | 重排序 |

### 短期记忆

| 环境变量 | 必填 | 默认值 | 说明 |
|----------|------|--------|------|
| `KSADK_STM_BACKEND` | 否 | `local` | 后端类型: `local` / `sqlite` / `database` |
| `KSADK_STM_DB_PATH` | 否 | `/tmp/ksadk_local_database.db` | SQLite 路径 |
| `KSADK_STM_DB_URL` | 否 | — | 数据库 URL (database 模式) |

### 长期记忆

| 环境变量 | 必填 | 默认值 | 说明 |
|----------|------|--------|------|
| `KSADK_LTM_BACKEND` | 否 | `local` | 后端类型: `local` / `http` / `sdk` |
| `KSADK_LTM_ACCESS_KEY` | sdk 时必填 | — | 金山云 AK (可复用 `KSYUN_ACCESS_KEY`) |
| `KSADK_LTM_SECRET_KEY` | sdk 时必填 | — | 金山云 SK (可复用 `KSYUN_SECRET_KEY`) |
| `KSADK_LTM_NAMESPACE` | sdk 时建议填 | — | 记忆库命名空间 (应用级隔离) |
| `KSADK_LTM_ENDPOINT` | 否 | `aicp.api.ksyun.com` | API 端点 (内网: `aicp.inner.api.ksyun.com`) |
| `KSADK_LTM_SCHEME` | 否 | `https` | 协议 (内网用 `http`) |
| `KSADK_LTM_REGION` | 否 | `cn-north-vip1` | API 区域 |
| `KSADK_LTM_AGENT_ID` | 否 | — | Agent ID (可选标识) |
| `KSADK_LTM_SCENE_ID` | 否 | — | 场景 ID (可选标识) |
| `KSADK_LTM_HTTP_URL` | http 时必填 | — | 远程记忆服务地址 |
| `KSADK_LTM_HTTP_TOKEN` | http 时必填 | — | 认证 Token |
| `KSADK_LTM_TOP_K` | 否 | `5` | 检索返回条数 |
| `KSADK_LTM_INDEX` | 否 | — | 索引名 (隔离不同应用) |

---

## 五、自动注入机制对照

当通过 `agentengine run .` 启动时，ADKRunner 根据环境变量自动完成初始化和工具注入：

| 环境变量 | 触发行为 | 注入的工具 |
|----------|---------|-----------|
| `KSADK_KB_DATASET_ID` 存在 | 初始化知识库客户端 | `search_knowledge_base` |
| `KSADK_STM_BACKEND` 存在 | 初始化短期记忆 SessionService | (无工具，替换 SessionService) |
| `KSADK_LTM_BACKEND` 存在 | 初始化长期记忆 MemoryService | `load_memory` |

**三者可独立使用，也可同时启用，互不冲突。**
