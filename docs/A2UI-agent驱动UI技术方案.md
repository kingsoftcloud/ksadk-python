# A2UI（Agent 驱动 UI）技术方案

> 状态：Canonical 设计稿，2026-07-15 重写。
> 定位：KsADK 的声明式 UI 协议能力，不是单一前端组件库，也不是 A2A 专属功能。
> 排期：H2 P1-J，8-9 月交付 Q3 可用 MVP，10-11 月补齐完整协议能力；必须由 2 名独立 owner 推进，且不绕过 P0-C RuntimeEvent。
> 兼容策略：功能尚未上线，不兼容 A2UI 1.0 以下或现有实验接口。
> 关联方案：`agentengine-server/docs/internal/a2a-center-productization-2026-07.md`。

## 1. 结论

A2UI 采用一套内部语义、两个 transport adapter：

1. **内部 canonical**：A2UI RuntimeEvent，负责流式、持久化、回放、审计和交互状态。
2. **Responses adapter**：服务 ksadk-web、hosted-ui 和本地 `/v1/responses` 主链。
3. **A2A adapter**：服务外部 Agent、Space 内远程 Agent 和 A2UI A2A Extension。

不在“Responses 路线”和“A2A 路线”中二选一。A2A 不是 KsADK 内部事件总线，把 A2UI 只写进 A2A executor 会导致普通 Runtime run 无法产出 UI；把 A2A 再桥接回 Responses 只会重复 P0-C。

Agent-facing interface 收敛为：

```python
await display_ui(surface)
result = await request_ui_input(surface, schema=..., kind="form")
await handle_ui_action(action)
```

调用者不需要理解 A2UI wire message、runner interrupt、Responses SSE 或 A2A DataPart。

### 1.1 Transport 选择：主链 SSE，跨 Agent 用 A2A

“SSE 还是 A2A”不是同一层的严格二选一：SSE 是流式传输方式；A2A 是 Agent-to-Agent 应用协议，A2A 的 streaming binding 本身也可能使用 SSE。

本方案的明确选择是：

| 场景 | 选择 | 理由 |
|---|---|---|
| ksadk-web / hosted-ui / 本地 UI | **Responses + SessionEvents SSE** | 已有浏览器鉴权、Session、历史回放、断线续订和 Gateway 通道；覆盖所有普通 Runtime run |
| Space 内调用外部/远程 Agent | **A2A 1.0 + A2UI Extension** | 需要标准 AgentCard、task/context、extension negotiation 和跨厂商互通 |
| 内部持久化与业务逻辑 | **RuntimeEvent** | transport-neutral，避免 SSE/A2A envelope 污染交互、审计和回放状态 |

不建议让 ksadk-web 主链直接改用 A2A，原因如下：

1. UI 面向用户与 Session，A2A 面向 Agent 与 Task；直接替换会重复 Session、权限、历史和取消语义。
2. 普通 `/v1/responses` run 并不是 A2A 请求，只改 A2A executor 会漏掉主要使用场景。
3. 当前 UI 已有 SSE 增量渲染和 Gateway 无缓冲透传，新增 A2A envelope 不带来用户价值，反而增加 AgentCard/extension/task 状态处理。
4. 非阻塞 action 可以发生在原 run 结束后，session-level SSE 比绑定 A2A Task 或单 run SSE 更自然。
5. 外部 A2A Agent 返回的 A2UI 仍可通过 adapter 转为 RuntimeEvent，再经 SSE 给浏览器；不会损失跨 Agent 标准互通。

因此最终链路是：

```text
本地/托管 Agent -> RuntimeEvent -> Responses/Session SSE -> Web UI
外部 A2A Agent -> A2A DataPart -> RuntimeEvent -> Session SSE -> Web UI
Web UI action -> KOP/RuntimeEvent -> A2A DataPart（仅当 origin 是远程 A2A Agent）
```

## 2. 版本与依赖

### 2.1 固定基线

- A2UI：`v1.0 Candidate`，当前仍是 living document。
- 固定 upstream commit：`3f6877b8c3420cd6c6e80df8a51ebd01d7286644`（2026-07-14）。
- Python core：精确 pin `a2ui-core==0.1.1`。
- A2A transport：`a2a-sdk==1.1.0`，wire protocol `1.0`。
- A2UI A2A extension URI：`https://a2ui.org/a2a-extension/a2ui/v1.0`。

不依赖 `a2ui-agent-sdk==0.4.0`，因为它强制 `a2a-sdk<0.4.0`，与 A2A 1.0 主线冲突。KsADK 自己实现 framework tool adapter 和 A2A extension adapter。

### 2.2 Schema 和 catalog

- 直接使用 pinned upstream `specification/v1_0/json/*`。
- 直接使用官方 `specification/v1_0/catalogs/basic/catalog.json`，不从 0.9.1 手工升级。
- 构建时校验 upstream commit、schema SHA-256 和 catalog SHA-256；不从 `main` 动态下载。
- A2UI Candidate 的 prose/schema 若出现冲突，以 pinned JSON Schema、官方 conformance case 和本方案显式决策为准。
- JSON 解析必须保留字段“缺失”和显式 `null` 的差异，不能先转成丢失 field-presence 的普通对象。

## 3. 目标与非目标

### 3.1 目标

1. Agent 以声明式 JSON 产出受信组件树和数据模型。
2. 支持 create/update/data/delete 的增量渲染和完整回放。
3. 支持展示、等待用户输入、非阻塞 action 三种业务语义。
4. UI action 结构化回灌，具有幂等、授权、校验和审计。
5. approval 复用同一 renderer，但保留其安全策略语义。
6. 普通 Runtime 和远程 A2A Agent 使用同一 UI 事件模型。
7. 未知 catalog、未知组件或校验失败时安全降级，不执行任意代码。

### 3.2 非目标

- 不做可视化 UI 构建器。
- 不允许模型生成 JavaScript/React 代码并直接执行。
- 不把完整 A2UI payload 回灌模型 transcript。
- 不用 `wantResponse` 推断 run 是否阻塞。
- 不把 `surface_id`、`component_id` 或 `action_id` 复用成 runner interrupt ID。
- 一期不接受外部 Agent 提供任意 inline catalog 或 renderer bundle。

### 3.3 Q3 MVP 与 Q4 完整版边界

| 能力 | Q3 MVP（8-9 月） | Q4 完整版（10-11 月） |
|---|---|---|
| 版本 | pinned v1.0 Candidate schema/basic catalog | 继续使用同一 pin，按官方变更评估升级 |
| 交互语义 | `display_ui` + `request_ui_input` | 增加 `handle_ui_action` 非阻塞 RPC |
| transport | Responses/SessionEvents SSE 主链 | 增加 A2A 1.0 A2UI Extension |
| renderer | Card/Text/Form/Select/RadioGroup/CheckboxGroup/ApprovalBar | FilterBar、企业组件和扩展 catalog |
| framework | LangGraph + ADK 阻塞交互，其他 framework 按 capability 降级 | 补齐 non-blocking handler 和更多 framework adapter |
| 治理 | 持久化、replay、基础权限/审计、payload limit | 跨 Agent origin、企业 catalog policy、完整性能/安全基线 |

Q3 的“可用”不是只做 renderer demo：必须从 Agent 产出、RuntimeEvent、server 持久化、SSE、Hosted UI 渲染到 action 回灌和 resume 全链路打通。Q4 可延后的是协议完整度和跨 Agent 互通，不是 Q3 产品闭环。

## 4. 语义模型

### 4.1 三种交互

| 语义 | Agent interface | Run 状态 | 用户上行 | 典型场景 |
|---|---|---|---|---|
| 展示 | `display_ui` | 不阻塞 | 无或普通新消息 | 卡片、图表、详情 |
| 等待输入 | `request_ui_input` | `input_required` | `SubmitA2UIAction`，命中 pending interaction 后 resume | 表单、选择、业务确认 |
| 非阻塞 action | `handle_ui_action` 注册 handler | 原 run 可已结束 | `SubmitA2UIAction`，创建独立 action execution | 展开、刷新、分页、筛选 |

`wantResponse=true` 只表示客户端期待与 `actionId` 关联的 `actionResponse`，不表达 Agent 是否等待。阻塞性由 `request_ui_input` 创建的 pending interaction 决定。

### 4.2 ID 模型

| ID | 生成方 | 用途 |
|---|---|---|
| `surface_id` | Agent/A2UICore | UI surface 的全局稳定标识 |
| `component_id` | Agent | surface 内组件标识 |
| `interaction_id` | A2UIRuntimeBridge | 一次等待输入的持久化身份 |
| `action_id` | client | 一次 action 调用与响应关联 |
| `invocation_id` | Runtime | 一次 run 执行 |
| `resume_token` | Runtime adapter | framework-specific checkpoint/interrupt 引用，仅服务端可见 |

这些 ID 可以关联，但不得互相替代。客户端只接触 surface/component/interaction/action，不接触 resume token。

### 4.3 Approval

Approval 是安全策略 contract，A2UI 是 presentation contract：

- ToolPolicy 判断某个工具调用是否需要 approval。
- policy 创建 `kind=approval` 的 pending interaction，包含不可篡改的 tool identity 和参数摘要。
- A2UI renderer 可以用 `ApprovalBar` 展示。
- `SubmitA2UIAction` 必须重新校验 actor、pending 状态、tool receipt 和 policy，不能只相信按钮 payload。
- 普通表单/选择不写成 `approval_request`，避免审计和权限语义污染。

## 5. 模块设计

### 5.1 `A2UICore`

深模块，隐藏 schema registry、catalog、surface state、字段 presence 和验证细节。

```python
class A2UICore:
    def validate_server_messages(self, messages, catalog_id): ...
    def validate_client_messages(self, messages, surface_state): ...
    def apply(self, state, messages) -> SurfaceState: ...
    def resolve_action(self, state, action) -> ResolvedAction: ...
```

实现包含：

- pinned v1.0 schema registry。
- official basic catalog 与企业 catalog registry。
- `callableFrom`、catalog function、component reference 和 JSON Pointer 校验。
- surface 全局唯一、root component、消息顺序和 replay determinism。
- payload 大小、深度、组件数量和更新频率限制。

### 5.2 `A2UIRuntimeBridge`

连接 Agent-facing interface、RuntimeEvent 和 Runtime Adapter：

```python
display_ui(surface) -> None
request_ui_input(surface, schema, kind) -> Awaitable[InputValue]
submit_action(command) -> ActionReceipt
```

它负责：

- 将 surface 转成校验后的 A2UI messages。
- 产生 canonical RuntimeEvent。
- 创建/恢复 pending interaction。
- 将通用 `interaction_id` 映射到 ADK/LangGraph 等 adapter 的 resume token。
- 调度非阻塞 action execution。
- 写审计、幂等 receipt 和失败事件。

### 5.3 `A2UITransportAdapter`

这是一个真实 seam，因为存在两个生产 adapter 和测试 adapter：

- `ResponsesA2UIAdapter`
- `A2AA2UIAdapter`
- `InMemoryA2UIAdapter`（测试）

adapter 只转换 envelope，不实现业务状态、approval 或 action handler。

### 5.4 `A2UIRendererRegistry`（ksadk-web）

```typescript
render(surfaceState, catalogId)
dispatch(action)
apply(messages)
```

registry 隐藏 React 组件映射、数据绑定、响应写回和 fallback。ChatMessage 不持有 renderer 内部结构，只持有 surface projection/ref。

## 6. Canonical RuntimeEvent

### 6.1 事件类型

| EventType | payload | 是否持久化 |
|---|---|---|
| `a2ui.surface.patch` | `messages[]`, `catalog_id`, `origin` | 是 |
| `a2ui.interaction.required` | `interaction_id`, `surface_id`, `kind`, `input_schema`, `expires_at` | 是 |
| `a2ui.action.received` | `action_id`, `surface_id`, `component_id`, `name`, `actor` | 是 |
| `a2ui.action.completed` | `action_id`, `actionResponse` 或 error | 是 |
| `a2ui.interaction.resolved` | `interaction_id`, decision/value 摘要 | 是 |
| `a2ui.error` | validation/policy/handler error | 是 |

统一 metadata：

```json
{
  "session_id": "sess-...",
  "invocation_id": "inv-...",
  "surface_id": "surface-...",
  "origin": {
    "kind": "local|hosted|a2a",
    "a2a_agent_id": "aa-...",
    "a2a_agent_version_id": "aav-..."
  },
  "audit": {
    "actor_id": "...",
    "account_id": "...",
    "request_id": "...",
    "timestamp": "..."
  }
}
```

### 6.2 Transcript 与回放

- A2UI events 落 `conversation_events`，按 `seq_id` 重放。
- renderer 从最后 surface snapshot 加后续 patch 重建；第一期可从头重放，超过阈值后增加 projection snapshot。
- A2UI payload 不进入 `TRANSCRIPT_EVENT_TYPES`，模型 transcript 只记录简短语义摘要，例如“用户提交了联系表单”。
- replay 必须使用当时的 catalog version/hash，不读取最新 catalog 后改变历史 UI。
- 删除 surface 也保留历史事件，当前 projection 中移除。

### 6.3 订阅

当前 `SubscribeRunEvents` 在 run terminal 时关闭，不能承载 run 结束后的非阻塞 action result。因此 P0-C 增加：

```http
GET /agentengine/api/v1/SubscribeSessionEvents
  ?SessionId=sess-...
  &AfterSeqId=123
```

- 订阅 session 事件，不绑定单个 invocation。
- 支持 cursor、heartbeat、重连和限时续订。
- `SubscribeRunEvents` 继续服务单 run 生命周期；两者不能混用。
- agentengine-server 消费并持久化，gateway 仅无缓冲透传。

## 7. Agent-facing interface

### 7.1 `display_ui`

```python
await display_ui(
    surface_id="order-summary",
    catalog_id=BASIC_CATALOG_ID,
    components=[...],
    data_model={...},
)
```

行为：构造 create/patch、校验、产生 `a2ui.surface.patch`。不暂停 run。

### 7.2 `request_ui_input`

```python
result = await request_ui_input(
    surface_id="approval-form",
    components=[...],
    data_model={...},
    input_schema={...},
    kind="form",
)
```

行为：

1. 发布 surface patch。
2. 创建 pending interaction 和 `a2ui.interaction.required`。
3. Runtime adapter 保存 resume token/checkpoint，run 进入 input-required。
4. 用户 action 校验通过后，bridge 用 interaction_id 恢复原执行。

不同 framework 的 interrupt 细节由 adapter 隐藏：

- LangGraph/DeepAgents：checkpoint + resume value。
- ADK：使用当前稳定的 input-required/request input 能力；无稳定能力时 capability 标 unsupported，不伪造。
- LangChain：没有 durable interrupt 时只允许 display/non-blocking action，不宣称 blocking supported。

### 7.3 `handle_ui_action`

```python
@handle_ui_action("order.refresh")
async def refresh(action: UIActionContext) -> Any: ...
```

非阻塞 action 是独立执行：

- 原 run 可以已经 completed。
- handler 受 timeout、auth、rate limit、policy 和 idempotency 控制。
- `wantResponse=true` 时产生 `a2ui.action.completed`，Responses adapter/A2A adapter 再编码成 `actionResponse`。
- 纯本地 action 不上行，应通过 catalog clientOnly function 处理。

## 8. 上行 Action 契约

控制台和 ksadk-web 使用统一 KOP：

```http
POST /agentengine/api/v1/SubmitA2UIAction
```

```json
{
  "SessionId": "sess-...",
  "SurfaceId": "surface-...",
  "InteractionId": "interaction-...",
  "Action": {
    "version": "v1.0",
    "action": {
      "name": "order.submit",
      "sourceComponentId": "submit",
      "timestamp": "2026-07-15T12:00:00Z",
      "context": {},
      "wantResponse": true,
      "actionId": "action-..."
    }
  },
  "IdempotencyKey": "..."
}
```

服务端处理顺序：

1. 从用户身份和 Session 推导 account/tenant，禁止 body 覆盖。
2. 查 surface projection 和 pending interaction。
3. 按 pinned catalog/schema 校验 action、context 和 component。
4. 校验 action 是否允许 remote、是否需要 policy approval。
5. 幂等写 `a2ui.action.received`。
6. blocking 命中 interaction 后 resume；non-blocking 调 handler。
7. 写 completed/resolved/error，并由 session event stream 推送。

响应为 `202 ActionReceipt`，不假设 handler 同步完成。简单同步 handler 也使用同一状态机。

## 9. Responses adapter

### 9.1 下行

RuntimeEvent 映射为 Responses extension events：

| RuntimeEvent | SSE event |
|---|---|
| `a2ui.surface.patch` | `response.ksadk.a2ui.surface.patch` |
| `a2ui.interaction.required` | `response.ksadk.a2ui.interaction.required` |
| `a2ui.action.completed` | `response.ksadk.a2ui.action.completed` |
| `a2ui.error` | `response.ksadk.a2ui.error` |

SSE 名字只是 adapter contract，不是持久化事件名。ksadk-web 的 live 和 replay 都先还原 canonical event，再交给 renderer。

### 9.2 上行

- 所有远端 action 调 `SubmitA2UIAction`。
- 不再把通用 A2UI input 伪装为 `ksadk_resume`。
- Resume 的 framework 细节只在 `A2UIRuntimeBridge` 内部。
- run 后 action result 通过 `SubscribeSessionEvents` 返回，而不是尝试复用已经关闭的 run SSE。

## 10. A2A adapter

### 10.1 能力声明

AgentCard `capabilities.extensions`：

```json
{
  "uri": "https://a2ui.org/a2a-extension/a2ui/v1.0",
  "required": false,
  "params": {
    "supportedCatalogIds": [
      "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"
    ],
    "acceptsInlineCatalogs": false
  }
}
```

client 通过 `X-A2A-Extensions` 激活，并在 Message metadata 提供 `a2uiClientCapabilities`。

### 10.2 DataPart

- MIME：`application/a2ui+json`。
- `data` 必须是 A2UI message 数组。
- server-to-client 和 client-to-server 分别使用对应 pinned schema。
- 最新 A2A SDK 使用 protobuf `Part/DataPart` 表示，不能复用 0.3 Pydantic `Part(root=DataPart(...))` 代码。
- adapter 保留 origin Agent/version，转换成 canonical RuntimeEvent。

### 10.3 Space 内外部 Agent

当 `A2ASpaceClient` 调用外部 Agent：

1. discovery 从 AgentCard 判断 A2UI extension 和 catalog。
2. ksadk 声明自己支持的 catalog，默认不接受 inline catalog。
3. 收到 A2UI DataPart 后校验，再转换为当前 Session 的 RuntimeEvent。
4. ksadk-web 仍消费 Responses/session events，不直接成为完整 A2A 客户端。
5. 用户 action 反向转换为 client-to-server DataPart，关联原 remote task/context。

未知 catalog 只显示安全 fallback 和原 Agent 信息，不下载 renderer 代码。

## 11. Catalog 与 renderer

### 11.1 Basic catalog

直接采用 pinned upstream basic catalog。在 ksadk package 内带只读副本和 manifest：

```text
ksadk/a2ui/spec/v1_0/*.json
ksadk/a2ui/catalogs/basic/catalog.json
ksadk/a2ui/catalogs/manifest.json
```

manifest 记录 upstream commit、catalog ID、hash、支持的 renderer version。

### 11.2 企业 catalog

- catalog 注册是平台受管动作，不允许普通 Agent 任意添加。
- 后端 schema 与前端 renderer 必须作为同一 release 单元发布。
- catalog ID/version/hash 三者共同决定兼容性。
- renderer registry 没有匹配实现时拒绝渲染，不回退到动态代码。

### 11.3 首批 renderer

优先复用官方 basic catalog 组件，不重复发明同义类型：

- Text、Card、Row、Column、Divider、Icon、Button。
- TextField、CheckBox、ChoicePicker、DateTimeInput、Slider。
- List、Tabs、Modal、Image、Video、AudioPlayer。

KsADK 企业扩展：

- `ApprovalBar`：安全审批表现层。
- `FilterBar`：仅在官方 catalog 无等价组合且证明可降低复杂度后加入。
- 复杂 Form 优先由 Column/TextField/ChoicePicker/Button 组合，不先造大而全 `Form` 组件。

## 12. 安全与审计

- 所有 server/client messages 先 schema 校验再进入状态机。
- 限制 surfaces/session、components/surface、payload bytes、patch rate 和 handler duration。
- action context 是不可信输入，必须按 interaction input schema 再校验。
- `callableFrom=clientOnly` 的 function 禁止远端调用。
- catalog instructions 只用于生成指导，不获得额外权限。
- A2UI 不改变 ToolPolicy、sandbox、network 或 credential policy。
- audit 至少记录 actor、account、session、origin Agent/version、surface/component/action、request_id、结果和时间。
- 敏感表单值不写普通日志；事件按字段 classification 做脱敏/加密/保留期。
- replay 读取权限与原 Session 一致，share link 不自动获得高风险 action 权限。

## 13. 三仓交付

| 仓/端 | 交付 |
|---|---|
| ksadk-python | `A2UICore`、RuntimeBridge、agent-facing tools、framework adapters、RuntimeEvent、Responses/A2A adapters |
| ksadk-web | renderer registry、basic renderer、surface projection、SubmitA2UIAction、session event replay |
| agentengine-server | conversation event 持久化、pending interaction/action receipt、KOP action、session subscription facade、审计查询 |
| agentengine-gateway | SSE/A2A header 透传、body/stream limit、鉴权和无缓冲，不解析 A2UI |

`agentengine-hosted-ui` 与 ksadk-web 共用 renderer package；不能长期复制两套 parser/renderer。

## 14. 实施顺序与工作量

完整方案仍是 7-10 周净工作量。为了在 Q3 落地，不是压缩必需工作，而是在 8-9 月投入 2 名独立 owner 并行后端/前端，同时将 non-blocking RPC、A2A Extension 和企业 catalog 后置 Q4。

### 14.1 Q3 MVP（8/1-9/30）

| 时间 | 后端/协议 owner | 前端/Hosted UI owner | 退出条件 |
|---|---|---|---|
| 8/1-8/7 | pinned spec/catalog、`A2UICore`、conformance fixtures | renderer registry/types、fixture viewer | schema/catalog hash 冻结，六类 wire message 可验证 |
| 8/8-8/14 | RuntimeEvent 映射、surface projection、Responses adapter | Card/Text 流式渲染 | live 事件能在本地 UI 稳定渲染 |
| 8/15-8/28 | event persist/list/replay、payload limits | Form/Select/Radio/Checkbox/Approval + replay | live 与 replay 得到一致 SurfaceState |
| 9/1-9/7 | pending interaction、`SubmitA2UIAction`、幂等 | 表单校验、提交/重试/过期状态 | LangGraph 暂停/提交/恢复闭环 |
| 9/8-9/14 | ADK adapter、approval policy、actor/origin audit | Hosted UI 权限/capability/fallback | ADK 暂停/恢复，越权或篡改 action 被拒绝 |
| 9/15-9/23 | session subscription、重启恢复、redaction | Hosted UI 整合、未知 catalog 安全降级 | 服务重启后 pending interaction 仍可提交 |
| 9/24-9/30 | 安全/性能/灰度、Q4 升级清单 | 回归、文档、灰度 flag | Q3 MVP 验收全部通过，Hosted UI 受控 Beta |

### 14.2 Q4 完整化（10-11 月）

| 阶段 | 交付 |
|---|---|
| 10 月上半月 | `handle_ui_action`、non-blocking handler、`callFunction/actionResponse`、run 结束后 action execution |
| 10 月下半月 | A2A adapter、AgentCard extension、DataPart 校验、Space external Agent A2UI E2E |
| 11 月 | FilterBar/企业 catalog、跨 Agent origin 审计、完整性能/安全基线、受控发布 |

不可通过“只改 A2A executor”隐藏 Q3 主链工作；那不构成产品能力。如果 8 月没有 2 名独立 owner，不得保留“Q3 MVP”承诺而悄悄删减 replay、审计或 action 恢复。

## 15. 验收

### 15.1 Q3 MVP 发布门禁

- pinned v1.0 官方 conformance cases 通过；create/update/data/delete 的 live 与 replay 得到相同 SurfaceState。
- Card/Text/Form/Select/RadioGroup/CheckboxGroup/ApprovalBar 全部可渲染，未知 catalog 安全降级。
- `display_ui` 不阻塞；`request_ui_input` 在 LangGraph/ADK 能暂停、持久化、重启后提交并恢复。
- approval 重新经过 ToolPolicy，actor/context/tool receipt 篡改被拒绝，重复提交幂等。
- Responses/SessionEvents SSE 主链可流式、回放和断线续订，历史 1,000 条事件通过 projection 回放。
- 基础 payload/component/rate limit、敏感字段脱敏、session 权限和审计查询通过。

Q3 MVP 不以 non-blocking action、A2A Extension、FilterBar 或企业 catalog 作为发布门禁。

### 15.2 完整协议验收

#### 15.2.1 协议与渲染

- pinned v1.0 官方 conformance cases 全部通过。
- create/update/data/delete 流式与 replay 得到相同 SurfaceState。
- 缺失/null、重复 surface、坏引用、未知组件、超限 payload 有确定结果。
- renderer 未知 catalog 安全降级，不执行动态代码。

#### 15.2.2 交互

- display 不阻塞 run。
- form/choice 使用 interaction_id 正确暂停和恢复，重启后仍可提交。
- approval 重新经过 ToolPolicy，篡改 tool/context 被拒。
- non-blocking action 在原 run 完成后仍能收到 actionResponse。
- action 幂等、重复点击和乱序响应测试通过。

#### 15.2.3 transport

- 同一 canonical event 经 Responses 和 A2A adapter 编码后语义一致。
- 外部 A2A Agent 的 A2UI DataPart 能在当前 Session 渲染并回传 action。
- 未激活 extension、catalog 不匹配、A2A task 丢失有明确 fallback。

#### 15.2.4 安全和性能

- action 越权、catalog 注入、clientOnly remote call、敏感字段泄漏测试通过。
- 单 Session surface/component 上限和 patch rate 生效。
- 典型 20-50 组件 surface 首次渲染和增量更新不阻塞文本流。
- 历史 1,000 条事件回放使用 projection 后保持可接受延迟。

## 16. 待产品确认但不阻塞架构

- 首批允许的企业 catalog 清单与 owner。
- 表单敏感字段的默认保留期和导出权限。
- action handler 的计费与配额归属。
- A2UI Candidate 升为 stable 后的升级门禁。

这些决策通过 catalog/policy/config 落地，不改变 RuntimeEvent、RuntimeBridge 或 transport seam。
