# KsADK 平台级 Knowledge Base / Long-term Memory 示例

> 主入口已迁移：完整使用说明请先看 [ksadk_usage_guide.md](./ksadk_usage_guide.md)。
>
> 本文保留为 KB / LTM 示例参考，重点放在跨框架示例和工具调用样例，而不是完整主线说明。

本文档对应当前平台语义：

- `KSADK_KB_*` 是平台知识库配置。
- `KSADK_LTM_*` 是平台长期记忆配置。
- ADK 默认走 native path。
- LangChain / LangGraph / DeepAgents 默认走 ambient path，且默认策略是 `on_demand`。
- 显式工具导入始终可用：`search_knowledge_base`、`load_memory`、`save_memory`。

## 环境变量

```ini
# 知识库
KSADK_KB_DATASET_ID=your_kb_id
KSADK_KB_ENDPOINT=aicp.inner.api.ksyun.com
KSADK_KB_SCHEME=http

# 长期记忆
KSADK_LTM_BACKEND=sdk
KSADK_LTM_NAMESPACE=your_namespace
KSADK_LTM_ENDPOINT=aicp.inner.api.ksyun.com
KSADK_LTM_SCHEME=http

# 可显式指定，也可回退到 KSYUN_REGION
KSADK_KB_REGION=cn-beijing-6
KSADK_LTM_REGION=cn-beijing-6
KSYUN_REGION=cn-beijing-6
```

说明：

- 未显式设置 `KSADK_KB_REGION` / `KSADK_LTM_REGION` 时，会回退到 `KSYUN_REGION`。
- endpoint 是 `*.inner.api.ksyun.com` 且未显式设置 `KSADK_*_SCHEME` 时，会默认使用 `http`。
- `KSADK_KB_AMBIENT_POLICY` / `KSADK_LTM_AMBIENT_POLICY` 默认是 `on_demand`。
- 兼容旧行为时可设置为 `always`；彻底关闭 ambient 路径时可设置 `KSADK_KB_AMBIENT_ENABLED=false` / `KSADK_LTM_AMBIENT_ENABLED=false`。

## 1. LangGraph / DeepAgents: 仅配 env 即生效

不改 agent 代码也可以。平台会在调用前按需处理：

- 信息查询会触发 KB 检索，生成 `kb_context`
- 显式回忆历史/偏好类输入会触发 LTM 检索，生成 `memory_context`
- 通过 runner 适配层注入到 LangGraph / DeepAgents 调用中

```python
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

model = ChatOpenAI(model="gpt-4o-mini")

root_agent = create_react_agent(
    model=model,
    tools=[],
)
```

运行：

```bash
agentengine run .
# 或
agentengine web
```

如果项目是 DeepAgents，同样适用，因为 `DeepAgentsRunner` 复用 `LangGraphRunner`。

## 2. LangChain / LangGraph / DeepAgents: 显式工具导入

当你希望模型通过显式 tool call 访问 KB / LTM 时，可以直接导入平台工具。

```python
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from ksadk.knowledge_base import search_knowledge_base
from ksadk.memory import load_memory, save_memory

model = ChatOpenAI(model="gpt-4o-mini")

root_agent = create_react_agent(
    model=model,
    tools=[
        search_knowledge_base,
        load_memory,
        save_memory,
    ],
)
```

建议：

- env-only ambient path 和 manual tool path 二选一作为主路径。
- v1 不会自动帮你对 ambient / manual 做黑盒去重。
- 如果你的远端环境到 KB/LTM 网络不稳定，优先使用 `on_demand` 或直接关闭 ambient，再保留显式工具路径。

## 3. ADK: 保持原生注入路径

ADK 项目继续保持原有体验。只配 env 即可自动获得：

- `search_knowledge_base`
- `load_memory`
- `save_memory`

```python
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

model = LiteLlm(model="openai/gpt-4o-mini")

root_agent = Agent(
    name="assistant",
    model=model,
    instruction=(
        "当问题需要外部知识时使用 search_knowledge_base；"
        "需要用户长期偏好时使用 load_memory；"
        "当用户明确要求记住某件事时使用 save_memory。"
    ),
    tools=[],
)
```

运行：

```bash
agentengine run .
```

## 4. 平台 Service 直连

如果你不想走 tool path，也可以直接使用平台 service。

### 4.1 Knowledge Base

```python
from ksadk.knowledge_base.service import KnowledgeBaseService

service = KnowledgeBaseService.from_env()
print(service.search_text("查一下云主机现在有哪些机型"))
```

### 4.2 Long-term Memory

```python
from ksadk.memory import LongTermMemoryService

service = LongTermMemoryService.from_env()

service.save_text(
    user_id="user-1",
    content="用户偏好高 CPU 机型",
    metadata={"agent_id": "demo-agent", "session_id": "sess-1"},
)

print(service.search_text(user_id="user-1", query="用户偏好什么机型"))
```

## 5. 运行时工具语义

平台长期记忆工具定义如下：

```python
from ksadk.memory.tool import load_memory, save_memory

load_memory("用户之前提到过什么")
save_memory("用户喜欢标准型实例")
```

行为说明：

- `load_memory(query)` 从当前运行时上下文中自动解析 `user_id`。
- `save_memory(content)` 会自动带上 `agent_id`、`session_id`、`runner_type` metadata。
- 如果缺少运行时上下文，会返回明确诊断，而不是静默失败。
