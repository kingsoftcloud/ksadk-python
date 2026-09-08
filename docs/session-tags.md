# 会话标签（Session Tags）开发指南

Session Tags 是 AgentEngine Server 管理的会话级业务分类。创建会话时，调用方可以写入一组字符串键值；后续通过该会话发起的托管执行会收到只读标签快照。LangChain、LangGraph 和 Google ADK 使用同一份平台数据，不需要各自维护另一套标签。

典型用途包括：

- 按业务场景选择流程，例如售前、客服、售后；
- 按渠道选择回复策略，例如 Web、企业 IM、API；
- 按客户或业务线加载配置、知识库或工具；
- 在会话列表中按精确标签筛选。

Tags 是业务数据，不能代替租户身份或权限校验。Server 始终先按认证账号、Agent 和用户范围校验会话归属，再读取或更新 Tags。

## 1. 端到端流程

一次完整调用包含以下步骤：

1. 业务服务调用 Server `CreateSession`，在 `Tags` 中写入分类。
2. 业务服务保存返回的 `SessionId`。
3. 后续调用 `RunAgent` 时传入该 `SessionId`，不再重复传 Tags。
4. Server 在受理执行时读取最新 Tags，并注入受保护的会话快照。
5. KsADK 在 LangGraph、LangChain 或 ADK 的执行上下文中暴露只读快照。
6. 如需修改分类，业务服务调用 `UpdateSession`；修改从下一次执行开始生效。

标签的写入方通常是创建会话的业务后端，Agent 只负责读取和解释。不要让 Agent 直接修改当前执行的标签快照。

## 2. 创建并使用带标签的会话

下面只展示 AgentEngine Action 的请求体。认证、签名、目标地址和错误处理继续沿用现有的 Server/KOP 调用方式。

### 2.1 创建会话

```http
POST /agentengine/api/v1/CreateSession
Content-Type: application/json
```

```json
{
  "AgentId": "ar-your-agent-id",
  "UserId": "user-001",
  "Tags": {
    "customer": "customer-a",
    "scene": "customer_service",
    "channel": "web"
  }
}
```

响应中的会话对象会原样返回 `Tags` 和初始版本号：

```json
{
  "Code": 0,
  "Data": {
    "Session": {
      "SessionId": "session-001",
      "AgentId": "ar-your-agent-id",
      "UserId": "user-001",
      "Tags": {
        "customer": "customer-a",
        "scene": "customer_service",
        "channel": "web"
      },
      "TagsRevision": 1
    }
  }
}
```

创建时未传 `Tags` 的历史用法继续有效。空标签会话的 `Tags` 为 `{}`，`TagsRevision` 为 `0`。

### 2.2 发起执行

```http
POST /agentengine/api/v1/RunAgent
Content-Type: application/json
```

```json
{
  "AgentId": "ar-your-agent-id",
  "UserId": "user-001",
  "SessionId": "session-001",
  "Messages": [
    {"role": "user", "content": "帮我查询订单状态"}
  ]
}
```

调用方不需要在 `RunAgent` 中重复传 Tags。Server 会按认证范围和 `SessionId` 加载平台保存的最新标签。调用方自行构造的 `metadata.agentengine.session_context` 不会覆盖平台快照。

## 3. 查询与过滤会话

### 3.1 获取单个会话

`GetSession` 返回 `Tags` 和 `TagsRevision`：

```json
{
  "SessionId": "session-001"
}
```

### 3.2 列出所有会话

```json
{
  "AgentId": "ar-your-agent-id",
  "UserId": "user-001",
  "Page": 1,
  "PageSize": 20
}
```

`Data.Sessions` 中的每个会话均包含：

```json
{
  "SessionId": "session-001",
  "Tags": {
    "scene": "customer_service",
    "channel": "web"
  },
  "TagsRevision": 1
}
```

### 3.3 按 Tag 过滤

`ListSessions` 支持精确键值匹配：

```json
{
  "AgentId": "ar-your-agent-id",
  "UserId": "user-001",
  "TagFilters": {
    "scene": "customer_service",
    "channel": "web"
  },
  "TagMatch": "all",
  "Page": 1,
  "PageSize": 20
}
```

`TagMatch` 的语义如下：

| 值 | 含义 |
| --- | --- |
| `all` | 会话必须同时匹配 `TagFilters` 中的全部键值，默认值 |
| `any` | 会话匹配任意一个键值即可 |

例如，`scene=customer_service` 且 `channel=web`：

- `all` 只返回同时满足两个条件的会话；
- `any` 返回满足其中任意一个条件的会话。

当前过滤仅支持字符串精确相等，不支持前缀、模糊、范围或复杂表达式。分页的 `Total` 是过滤后的总数。账号、Agent 和用户范围仍然优先于 Tag 条件，不能通过 Tag 查询其他租户的会话。

## 4. 更新标签

```http
POST /agentengine/api/v1/UpdateSession
Content-Type: application/json
```

```json
{
  "SessionId": "session-001",
  "SetTags": {
    "scene": "after_sales",
    "priority": "high"
  },
  "RemoveTags": ["channel"],
  "ExpectedTagsRevision": 1
}
```

- `SetTags` 新增标签，或覆盖同名标签的值；
- `RemoveTags` 删除指定键，不存在的键可安全忽略；
- 同一个键不能同时出现在 `SetTags` 和 `RemoveTags`；
- `ExpectedTagsRevision` 可选，用于防止并发覆盖；版本不一致时返回 409；
- 不传 `ExpectedTagsRevision` 时，Server 仍使用数据库 CAS 合并并发补丁；
- 只有标签实际发生变化时，`TagsRevision` 才递增。

更新不会改变正在执行中的快照。当前执行继续使用受理时的 Tags，下一次 `RunAgent` 或恢复执行会读取新版本。

## 5. LangGraph 教程

### 5.1 最小读取方式

现有 LangGraph Agent 无需修改 `context_schema`。在节点、工具或回调中调用通用 helper：

```python
from ksadk.runtime_context import (
    get_current_session_context,
    get_current_session_tags,
)


def route_by_scene(state):
    tags = get_current_session_tags()
    revision = get_current_session_context().revision

    if tags.get("scene") == "customer_service":
        route = "support"
    elif tags.get("scene") == "after_sales":
        route = "after_sales"
    else:
        route = "default"

    return {"route": route, "tags_revision": revision}
```

`get_current_session_tags()` 返回只读 `Mapping[str, str]`。如果当前没有托管调用上下文，或会话没有标签，则返回空映射。

### 5.2 使用 LangGraph 原生 `Runtime.context`

如果希望在节点签名中显式声明会话上下文，可以使用 `SessionContext`：

```python
from dataclasses import dataclass

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.runtime import Runtime

from ksadk.session_context import SessionContext


class AgentState(MessagesState):
    route: str


@dataclass
class GraphContext:
    session: SessionContext


def route_by_scene(state: AgentState, runtime: Runtime[GraphContext]):
    scene = runtime.context.session.tags.get("scene", "default")
    return {
        "route": scene,
        "messages": [AIMessage(content=f"selected route: {scene}")],
    }


workflow = StateGraph(AgentState, context_schema=GraphContext)
workflow.add_node("route_by_scene", route_by_scene)
workflow.add_edge(START, "route_by_scene")
workflow.add_edge("route_by_scene", END)

root_agent = workflow.compile()
```

KsADK 会在托管调用时填充 `runtime.context.session`。只有字段类型明确为 `SessionContext` 或 `SessionContext | None` 时才启用该原生注入。已有 Agent 如果使用 `session: str`、自定义对象或没有 `context_schema`，仍保持原有行为。

## 6. Google ADK 完整教程

ADK Agent 不需要在 Google ADK 的 `Session.state` 中手动保存平台 Tags。AgentEngine Server 是标签的唯一持久化来源，KsADK 在每次托管执行期间通过运行上下文提供快照。

### 6.1 项目结构

```text
tagged_adk_agent/
├── agent.py
├── agentengine.yaml
└── requirements.txt
```

`agentengine.yaml`：

```yaml
name: tagged-adk-agent
version: "1.0.0"
framework: adk
entry_point: agent.py
agent_variable: root_agent
```

`requirements.txt`：

```text
ksadk
google-adk
```

实际发布时应按团队的依赖锁定策略固定版本。

### 6.2 完整 `agent.py`

下面的 Agent 在动态 instruction 和工具中分别读取同一份标签快照：

```python
from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext

from ksadk.runtime_context import (
    get_current_session_context,
    get_current_session_tags,
)


SCENE_INSTRUCTIONS = {
    "customer_service": "你是客服助手，优先查询订单和服务记录。",
    "after_sales": "你是售后助手，优先处理退换货和故障排查。",
    "sales": "你是售前助手，优先介绍产品能力和适用范围。",
}


def instruction_for_session(_: ReadonlyContext) -> str:
    """每次 ADK 调用模型前，根据平台会话标签选择固定指令。"""
    scene = get_current_session_tags().get("scene", "")
    return SCENE_INSTRUCTIONS.get(
        scene,
        "你是通用业务助手；无法确定业务场景时先向用户澄清。",
    )


def get_session_classification() -> dict[str, object]:
    """供 ADK 模型调用的工具，返回当前执行的只读分类。"""
    context = get_current_session_context()
    return {
        "tags": dict(context.tags),
        "tags_revision": context.revision,
    }


root_agent = Agent(
    name="tagged_adk_agent",
    model="gemini-2.5-flash",
    instruction=instruction_for_session,
    tools=[get_session_classification],
)
```

这个例子有两个读取点：

1. `instruction_for_session` 在 ADK 准备模型请求时读取 `scene`，将枚举值映射为固定业务指令；
2. `get_session_classification` 作为普通 ADK function tool，返回当前 Tags 和 revision。

建议把允许的 Tag 值映射到预定义指令、工具集或业务配置，不要把任意 Tag 值直接拼进系统指令。Tags 本身不会自动进入模型 prompt；只有业务代码像上例一样主动使用时，模型才会看到相应内容。

### 6.3 在 ADK 工具中选择业务实现

实际项目通常不需要把全部 Tags 返回给模型，可以只在工具内部选择实现：

```python
from ksadk.runtime_context import get_current_session_tags


def query_order(order_id: str) -> dict[str, str]:
    tags = get_current_session_tags()
    customer = tags.get("customer", "default")

    if customer == "customer-a":
        return query_customer_a_order(order_id)
    if customer == "customer-b":
        return query_customer_b_order(order_id)
    return query_default_order(order_id)
```

真实的租户鉴权仍应使用平台认证身份和服务端权限检查。这里的 `customer` 只负责选择已经授权范围内的业务配置，不能作为访问其他客户数据的凭据。

### 6.4 在 ADK callback 中读取

ADK 的 agent/model/tool callback 也运行在同一次 KsADK 调用上下文中，可以使用同一个 helper：

```python
from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

from ksadk.runtime_context import get_current_session_context


def before_tool(
    tool: BaseTool,
    args: dict,
    tool_context: Context,
) -> None:
    session = get_current_session_context()
    audit_business_route(
        tool_name=tool.name,
        scene=session.tags.get("scene", "default"),
        tags_revision=session.revision,
    )


root_agent = Agent(
    name="tagged_adk_agent",
    model="gemini-2.5-flash",
    tools=[query_order],
    before_tool_callback=before_tool,
)
```

Callback 中获得的也是当前执行快照。不要修改 `session.tags`；它是只读映射。

### 6.5 从创建会话到 ADK 读取

调用顺序与 LangGraph 相同：

1. 业务服务调用 `CreateSession`，写入 `Tags`；
2. 保存返回的 `SessionId`；
3. 使用同一 `SessionId` 调用 `RunAgent`；
4. KsADK 的 ADK Runner 建立调用上下文；
5. `instruction_for_session`、ADK 工具或 callback 调用 helper 读取快照；
6. 业务服务调用 `UpdateSession` 后，下一次 ADK 执行读取新 revision。

不要把标签写入 `google.adk.sessions.InMemorySessionService` 并期待平台会话列表能够查询到。ADK SessionService 保存的是 ADK 自身状态；AgentEngine Session Tags 由 Server 保存，两者职责不同。

### 6.6 本地运行与托管运行的差异

直接使用 Google ADK Runner、`adk run`，或独立启动本地 Runtime 时，没有 AgentEngine Server 注入的平台标签，helper 会安全返回空映射和 revision `0`。因此业务代码必须提供默认分支。

经过 AgentEngine Server `RunAgent`、`ResumeRun` 或 Kernel admission 的托管调用，才会获得 Server 校验后的标签快照。不要在公网 `/v1/responses` 或 `/v1/chat/completions` 的 Metadata 中自行伪造 `session_context`；Gateway 和 Runtime 会移除该保留字段。

## 7. 生命周期与恢复语义

- Server 在一次执行被受理时确定标签快照；
- 运行中调用 `UpdateSession` 只影响后续执行；
- `ResumeRun` 会重新读取当前标签版本；
- 相同幂等键的重试保留首次受理的快照，避免一次逻辑执行读到两套分类；
- 删除最后一个标签后，下一次执行读取空映射和递增后的 revision；
- Tags 不自动写入 Agent state、模型输入、LangChain tracing tags 或公开 Responses Metadata；
- Agent 主动把 Tags 放入回复或工具结果时，它们才会出现在业务输出中。

## 8. 约束与错误

| 项目 | 约束 |
| --- | --- |
| 单会话标签数 | 最多 32 个 |
| Key | 1–64 位，只允许字母、数字、下划线、点和连字符 |
| Value | 字符串，最长 256 个字符 |
| 保留前缀 | `agentengine.`，业务方不能使用 |
| 过滤方式 | 字符串精确相等，`all` 或 `any` |
| 并发更新 | 可使用 `ExpectedTagsRevision`；冲突返回 409 |
| 越权访问 | 返回 404，不泄露会话是否存在 |

无标签请求与历史 Agent 保持兼容：

- 未传 `Tags` 的 `CreateSession` 继续工作；
- 未使用 helper 的历史 LangGraph、LangChain 和 ADK Agent 无需修改；
- 无标签时 helper 返回空映射；
- 旧 Runtime 不会获得新 helper，需要重新构建或部署 Agent 才能读取平台标签；
- Server、Gateway 和 KsADK Runtime 必须配套升级，才能形成完整链路。

## 9. 会话列表 UI 状态

Server `GetSession`、`ListSessions` 已返回 `Tags` 和 `TagsRevision`，并支持 `TagFilters`/`TagMatch`。当前 `ksadk-web` 会话侧栏尚未把 Tags 渲染成可见标签，也没有把筛选条件传给 `ListSessions`。

如果产品需要在 Studio 或 Hosted UI 中显示和筛选，应在 `ksadk-web` 统一实现：

1. 为前端 `Session` 类型增加 `Tags`、`TagsRevision`；
2. 为 `createSession` 增加 Tags 请求参数；
3. 为 `listSessions` 增加 `TagFilters`、`TagMatch` 参数；
4. 在会话侧栏渲染标签，并提供筛选条件；
5. Studio 和 Hosted UI 仅作为宿主消费同一套组件与行为。

## 10. 验证

安装开发与框架依赖：

```sh
uv sync --extra dev --extra langgraph --extra langchain --extra adk
```

运行 SDK 的上下文、真实框架与恢复测试：

```sh
uv run pytest -q \
  tests/test_session_tags_context.py \
  tests/test_session_tags_framework_resume.py
```

`tests/e2e/session_tags_runtime.py` 包含真实 LangGraph、LangChain、ADK 和历史 Agent fixture。Server 的 `tests/test_session_tags_e2e.py` 会启动独立 TCP Server 与 Runtime 进程，使用确定性工具调用模型验证：

- 创建、读取、更新和删除 Tags；
- `ListSessions` 精确过滤；
- LangGraph、LangChain 和 ADK 的真实工具读取；
- 更新只影响下一次执行；
- 恢复执行重新读取快照；
- 历史 Agent 保持兼容；
- 调用方不能伪造平台标签快照。
