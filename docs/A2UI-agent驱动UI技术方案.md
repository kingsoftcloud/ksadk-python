# A2UI（Agent 驱动 UI）技术方案：现状与演进边界

> 代码基线：`ksadk-python 0.8.4`。
>
> 本文按 2026-09-11 仓库代码和测试重新整理。它区分已实现能力、条件能力、内部实验能力和后续设计，不能把后续设计当成客户可调用的公共 API。
>
> A2UI 是声明式 UI 数据协议和事件投影能力，不是 KsADK 前端组件库。客户前端可以使用自己的组件和视觉样式；KsADK 只负责数据、事件、交互身份和运行时生命周期。

## 1. 结论先行

当前不建议把 A2UI 作为自研 UI 的主接入路径。客户如果需要自定义卡片，优先使用 Responses 的普通业务交互协议：Agent 在 LangGraph 中调用 `interrupt(value)`，前端按自己的样式渲染 `value`，再用 `ksadk_resume` 恢复。这个路径已经和当前 runtime 的 resume、事件流和历史回放链路对齐。

A2UI 在当前仓库有真实代码和测试，但边界比较窄：

| 场景 | 当前建议 | 状态 |
| --- | --- | --- |
| 自研 UI 自定义业务卡片 | `interrupt(value)` + `ksadk_resume` | 客户主路径，可用 |
| AG-UI/CopilotKit 的声明式 UI | 可选 `generate_a2ui` 工具，输出 AG-UI A2UI activity | 条件可用，依赖可选包和 AG-UI transport |
| `A2UICore.display_ui/request_ui_input/submit_action` | Python 内部 API，直接写 RuntimeEventStore | 有代码和单元测试，尚未形成完整远程产品链路 |
| `/v1/responses` 上的 A2UI 专用事件 | 当前没有稳定的专用 wire contract | 不应对客户承诺 |
| `SubmitA2UIAction` HTTP 接口 | 当前仓库没有对应 route | 未实现 |
| `SubscribeSessionEvents` | 当前仓库没有对应 route；现有的是 `SubscribeRunEvents` | 未实现 |
| A2UI A2A Extension 跨 Agent 闭环 | 未完成可验证的端到端链路 | 后续研究 |

因此，A2UI 当前适合作为 AG-UI/Studio 的实验能力和 RuntimeEvent 投影能力继续演进，不适合作为客户前端必须依赖的基础协议。

## 2. 当前代码事实

### 2.1 Python A2UI core

`ksadk/a2ui/core.py` 提供以下内部能力：

- `display_ui(surface, invocation_id)`：校验 `Surface` 后向 `RuntimeEventStore` 写入 `ItemStarted`；同一 surface 再次展示时写入 `ItemUpdated`。
- `request_ui_input(surface, schema, kind, invocation_id)`：先展示 surface，再写入 `InteractionRequested`，返回一个 `PendingInteraction`。
- `submit_action(action, invocation_id)`：无 ledger 时写入一个 A2UI action 的 `InteractionRequested`；配置 durable interaction ledger 时走 `InteractionSubmission`，并返回 `ActionReceipt`。
- `end_surface(surface_id, invocation_id)`：写入 `ItemCompleted`。

这些 API 是 Python 内部接口。`submit_action` 不是远程 HTTP route，也不会自动调度一个客户自定义的前端 action handler。

`ksadk/a2ui/models.py` 和 `renderer.py` 提供一个 Python 侧的 basic catalog、校验器和安全降级 renderer，主要用于内部验证与 conformance。它不是客户前端的组件库，也不限制客户使用 React、Vue 或自己的设计系统。

### 2.2 AG-UI/CopilotKit A2UI 路径

可选的 AG-UI 路径由以下代码组成：

- `ksadk/agui/config.py`：只有检测到可用的可选依赖时才启用 AG-UI；当前固定检查 `ag-ui-protocol==0.1.19`、`ag-ui-langgraph==0.0.42`、`copilotkit==0.1.94`。
- `ksadk/compat/copilotkit_a2ui.py`：在 CopilotKit middleware 中按需注入 `generate_a2ui` 动态工具；模型调用受超时和有限重试控制，工具结果先校验再返回。
- `ksadk/agui/agent.py`：把 canonical A2UI data item 投影为 AG-UI `ActivitySnapshotEvent`，`activity_type` 为 `a2ui-surface`。
- `ksadk/agui/a2ui_projection.py`：当前公开投影是 A2UI v0.9 风格操作，操作包含 `createSurface`、`updateComponents`、`updateDataModel`、`deleteSurface`，每条操作包含 `surfaceId`。

只有 AG-UI transport 配置启用并且依赖满足时，`GetAgentUiBootstrap` 才可能将该 transport 的 `Capabilities.A2UI` 报为 `true`。

### 2.3 RuntimeEvent 和 Studio 投影

`ksadk/runtime/_runner_adapter/stream_mapping.py` 会识别经过校验的 `generate_a2ui` 工具结果：

```json
{
  "a2ui_operations": [
    {
      "version": "v0.9",
      "createSurface": {
        "surfaceId": "status-1",
        "catalogId": "https://a2ui.org/specification/v0_9/basic_catalog.json"
      }
    }
  ]
}
```

该结果会转成 canonical data item：

- `item_kind="data"`；
- `source.protocol="a2ui"`；
- `source.metadata.surface_id` 保存 surface 身份；
- `DataContent.data` 保存操作列表；
- 当前动态工具批次以 `ItemStarted + ItemCompleted` 表示一个已关闭的 operation batch。

`ksadk/studio/run_service.py` 再把 canonical 事件投影为 Studio 事件，公开字段主要是：

```json
{
  "Type": "a2ui.surface.update",
  "Content": {
    "surfaceId": "status-1",
    "a2uiOperations": [
      {
        "version": "v0.9",
        "updateDataModel": {
          "surfaceId": "status-1",
          "path": "/",
          "value": {"status": "ready"}
        }
      }
    ]
  }
}
```

这是 Studio/AG-UI 的投影形态，不等于 `/v1/responses` 已经提供了同名的官方事件。

## 3. 版本和协议边界

当前仓库存在两套容易混淆的版本信号：

1. `ksadk/a2ui/models.py` 的内部模型带有 `a2ui-core==0.1.1` 和 v1.0 Candidate 相关注释，并默认使用一个 v1.0 风格的 catalog ID。
2. `ksadk/agui/a2ui_projection.py`、CopilotKit 生成提示和相关测试实际输出 `version: "v0.9"`，默认 catalog 也是 `https://a2ui.org/specification/v0_9/basic_catalog.json`。

因此当前不能对外声称“KsADK 已冻结 A2UI v1.0”。在版本收敛前，所有公共文档应使用下面的说法：

- canonical RuntimeEvent 的协议标识是 `a2ui`，用于内部事件归一化、持久化和回放；
- AG-UI 对外操作投影当前按 v0.9 风格输出；
- A2UI v1.0 Candidate 只能作为升级目标，不能作为当前客户 wire contract；
- v0.9 到 v1.0 的 catalog、字段 presence、组件命名和 action 语义需要一次兼容性评估后再冻结。

旧方案中的 `a2ui.surface.patch`、`a2ui.interaction.required`、`a2ui.action.completed`、`SubmitA2UIAction` 和 `SubscribeSessionEvents` 是目标设计名，不是当前仓库已经稳定暴露的完整公共协议。后续实现前需要为每个名称增加 route、projection、golden fixture 和端到端证据。

## 4. 当前实际链路

### 4.1 AG-UI/CopilotKit 链路

```text
AG-UI client
   │  RunAgentInput + optional injectA2UITool
   ▼
KsadkAGUIAgent
   ▼
RuntimeAdapter / LangGraph runner
   │  optional generate_a2ui tool result
   ▼
canonical RuntimeEvent
   │  item_kind=data, source.protocol=a2ui
   ▼
AG-UI ActivitySnapshotEvent
   │  activity_type=a2ui-surface
   ▼
客户自己的 renderer / CopilotKit renderer
```

这条链路能验证“工具产出 → canonical event → AG-UI activity → 前端渲染”。它不等于所有普通 LangGraph Agent 都自动拥有 A2UI 工具；需要 AG-UI transport、可选依赖和 `inject_a2ui_tool` 配置。

### 4.2 A2UICore 内部链路

```text
Python 业务代码
   │ A2UICore.display_ui/request_ui_input/submit_action
   ▼
RuntimeEventStore
   │ ItemStarted/Updated/Completed 或 InteractionRequested
   ▼
Studio / canonical replay / 内部测试投影
```

这条链路适合继续建设内部 runtime foundation，但目前缺少面向客户前端的 action route、统一响应和跨进程交互恢复闭环。

### 4.3 Responses 链路

当前 Responses transport 的 bootstrap 明确返回：

```json
{
  "Protocol": "responses",
  "Capabilities": {
    "A2UI": false,
    "Interrupt": true,
    "Cancel": true
  }
}
```

因此自研 UI 使用 `/v1/responses` 时，不应等待 A2UI 专用事件；需要自定义卡片时使用普通 `interrupt(value)` 的业务 JSON，并通过 `response.ksadk.approval_request` / `response.incomplete` / `ksadk_resume` 完成闭环。

## 5. 客户前端应该如何使用

客户不需要采用 KsADK 的 Python `Surface`、`Component` 或 renderer。前端只需要消费稳定的业务数据和交互身份：

```python
# Agent 业务侧：只产生数据，不定义客户的 CSS 或 React 组件
from langgraph.types import interrupt


def ask_order_info(state):
    answer = interrupt({
        "card_type": "order_form",
        "schema_version": 1,
        "title": "订单信息",
        "fields": [
            {"name": "quantity", "type": "number", "required": True},
            {"name": "remark", "type": "text", "required": False},
        ],
        "business_ref": {"order_id": state["order_id"]},
    })
    # 这里重新做业务校验，不能直接相信客户端值。
    return validate_order_input(answer)
```

客户前端可以把上述数据渲染为任何形式：卡片、抽屉、弹窗、移动端底部面板或公司内部设计系统组件。KsADK 只要求：

- `interrupt(value)` 可 JSON 序列化、可持久化；
- 前端保留事件中的 `interrupt_id`/`approval_request_id`；
- 恢复请求把业务值放入 `ksadk_resume.value`；
- 业务 Agent 对值重新校验，并对外部副作用做幂等；
- 未知业务字段以数据处理，不执行字符串中的代码。

如果客户希望用 A2UI operation，前端需要自行消费 AG-UI 的 `ActivitySnapshotEvent.content.a2ui_operations`，并自行决定 renderer。当前 KsADK 不提供客户可直接调用的通用 action HTTP 接口。

## 6. 交互语义和 ID

### 6.1 展示、等待和 action

当前可验证的语义应分开描述：

| 语义 | 当前实现 | 说明 |
| --- | --- | --- |
| 展示 surface | `A2UICore.display_ui` 或 `generate_a2ui` 工具结果 | 写入 data item，不自动暂停普通 Responses run |
| 等待输入 | `A2UICore.request_ui_input` 或普通 LangGraph `interrupt` | 需要 pending interaction/checkpoint 和恢复链路 |
| 非阻塞 action | `A2UICore.submit_action` 可写 action 事实；业务 handler/远程 route 尚未统一 | 不能对客户承诺 run 结束后自动执行并回传 actionResponse |

不要用 `wantResponse`、按钮名称或某个 operation 名字推断 run 是否暂停。暂停能力由 runtime 的 interaction/checkpoint capability 决定。

### 6.2 ID 规则

| ID | 当前含义 | 前端是否保存 |
| --- | --- | --- |
| `surface_id` / `surfaceId` | A2UI surface 的业务身份 | 是 |
| `component_id` / `componentId` | surface 内组件身份，具体命名由 operation/前端协议决定 | 是，可选 |
| `interaction_id` / `interrupt_id` | 一次等待输入的身份 | 是，恢复必须带上 |
| `action_id` | 一次 action 请求身份；A2UICore 会生成或接受 | 是，当前用于 receipt/审计关联 |
| `session_id` / `thread_id` | 会话身份 | 是 |
| `invocation_id` / `run_id` | 一次运行身份 | 是 |
| checkpoint ID | framework/runtime 恢复点 | 由 runtime 控制，前端只透传服务端返回值 |

`surface_id`、`component_id`、`interaction_id`、`action_id` 和 checkpoint ID 不能互相替代。当前 AG-UI resume 要求提交集合与 pending interrupt 集合一致，并且支持同一批多个 interrupt；Responses 的 `extract_responses_resume_input` 当前一次只提取一个 resume item，两个 transport 不要混用这个细节。

## 7. 安全边界

当前已经有的安全约束：

- `Component.validate` 对 basic catalog 类型和 prop 白名单做校验；未知类型和可执行 prop 会失败。
- Python renderer 对未知组件安全降级为占位，不执行动态代码。
- `generate_a2ui` 使用固定工具定义、有限尝试和超时；工具结果必须是可解析的 `a2ui_operations`。
- A2UI surface 进入 canonical event 后按 session/run scope 持久化，不能绕过 RuntimeEvent。
- A2UI 不改变 ToolPolicy、sandbox、network 或 credential policy。

仍需补齐的生产安全能力：

- 对外统一的 catalog/version/hash 和 schema 校验；
- action actor、session、tenant、surface、component 和 pending interaction 的服务端鉴权；
- action context 的字段级校验、过期和重放保护；
- payload 大小、组件数量、patch 频率和 handler 超时限制；
- 敏感表单值的日志脱敏、保留期、历史回放和导出权限；
- 未知 catalog、版本不匹配和 action 越权的标准错误码。

这些能力缺失时，不能把 A2UI 作为跨租户或高风险审批链路的生产协议。

## 8. 与 A2A 的关系

当前仓库有 A2A runtime/event adapter，但没有验证完成的 A2UI A2A Extension 客户闭环。旧方案中关于以下内容的描述应视为后续设计，而不是现状：

- AgentCard 宣布 A2UI extension；
- `application/a2ui+json` DataPart 的 server/client 双向校验；
- 外部 Agent 的 A2UI surface 转换到当前 Session；
- 用户 action 反向转换为远程 task/context 的 A2UI DataPart；
- catalog negotiation、origin 审计和跨 Agent action 权限。

如果未来推进 A2A，顺序应是：先冻结一套实际使用的 A2UI 版本，再做单向 DataPart 接收和安全降级，最后才做 action 回传和跨 Agent 恢复。不能先把 A2A executor 改成“看起来支持 A2UI”而缺少 RuntimeEvent、Session、权限和回放证据。

## 9. 需要继续补齐的 runtime 能力

| 优先级 | 能力 | 验收证据 |
| --- | --- | --- |
| P0 | 冻结 canonical A2UI data/interaction/action projection 的公开字段 | projection contract + golden fixture + replay 测试 |
| P0 | 冻结版本和 catalog 选择，解决内部 v1.0 标识与公开 v0.9 操作不一致 | 版本矩阵、升级/降级策略和兼容测试 |
| P0 | 为 AG-UI 之外的客户 UI 提供稳定的 A2UI 下行事件或明确保持 JSON card 路径 | Responses/Session event E2E，不能只依赖内部 event store |
| P1 | 提供受鉴权、幂等和 durable ledger 保护的 action route，或明确不做远程 A2UI action | route contract、重复点击、越权、过期和重放测试 |
| P1 | 提供 session 级事件订阅，覆盖 run terminal 后 action/result | route、cursor、heartbeat、断线和多副本测试 |
| P1 | 将 pending interaction 与 checkpoint 的恢复能力统一投影给前端 | bootstrap capability、重启、多副本和过期测试 |
| P2 | A2A A2UI Extension 单向接收、catalog negotiation 和 origin 审计 | AgentCard/DataPart/Session replay E2E |
| P2 | A2A action 回传、远程 task 关联和跨 Agent 幂等 | 正向、拒绝、超时、重试和安全测试 |

在 P0/P1 完成前，客户项目应继续使用“业务 JSON 卡片 + `ksadk_resume`”。这条路径不依赖 A2UI renderer，也不妨碍客户完全自定义 UI。

## 10. 建议的实施边界

### 10.1 客户项目

- 入口：`/v1/responses` 或 `RunAgent(ApiFormat=responses)`。
- 卡片：Agent 自定义 JSON 数据，前端自定义组件和样式。
- 暂停：LangGraph `interrupt(value)`。
- 恢复：`ksadk_resume`，保留 `interrupt_id`。
- 长任务：使用 `GetAgentUiBootstrap`、`ListSessionEvents`、`SubscribeRunEvents` 和条件可用的 checkpoint route。
- A2UI：不作为依赖，不要求客户安装 AG-UI/A2UI 包。

### 10.2 AG-UI 试验

- 先确认可选依赖版本和 bootstrap transport capability。
- 显式开启 `inject_a2ui_tool`，验证 `generate_a2ui` 工具结果和 A2UI v0.9 operations。
- 前端只依赖 `ActivitySnapshotEvent` 的公开 projection，不读取 runner 私有字段。
- 失败时回退到文本/普通业务卡，不阻塞主聊天链路。

### 10.3 Runtime 演进

先把版本、projection、action、session subscription 和 durable interaction ledger 收敛，再考虑 A2A。每完成一层都要有 source、wire、replay 和端到端证据；单元测试通过不代表客户链路已可用。

## 11. 当前验证入口

以下测试证明了已有局部能力，但不等于完整客户 E2E：

```bash
uv run --no-sync pytest -q \
  tests/a2ui/test_a2ui_core.py \
  tests/a2ui/test_a2ui_renderer.py

uv run --no-sync pytest -q \
  tests/agui/test_copilotkit_a2ui_compat.py \
  tests/agui/test_runtime_adapter.py

uv run --no-sync pytest -q \
  tests/events/test_canonical_runtime_event.py \
  tests/events/test_mixed_schema_replay.py
```

交付前还必须增加真实浏览器/HTTP 验证：

1. AG-UI 开启与关闭时 bootstrap capability 正确；
2. `generate_a2ui` 生成的 v0.9 operation 能 live 渲染并在历史重放后得到相同结果；
3. 未知组件、坏 operation、超时和模型生成失败安全回退；
4. 普通 Responses 自研 UI 不误依赖 A2UI，仍能用 JSON card + `ksadk_resume` 完成表单交互；
5. 如果实现 action route，再验证鉴权、幂等、过期、重复点击、重连和多副本恢复。

在这些证据完成前，本文的 A2UI 部分应被客户阅读为“当前边界与后续路线”，而不是已经冻结的 v1.0 产品协议。
