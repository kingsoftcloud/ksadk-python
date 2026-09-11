# LangGraph Agent + 自研 UI 人机交互卡片对接方案

> 面向读者：**Agent 业务开发**（写 LangGraph 图的）和**前端开发**（自研聊天 UI 的）。
> 基于 ksadk-python **0.8.4**，运行时协议以本版本代码为准。
> 相关文档：`docs/guides/LangGraph开发最佳实践.md`（业务代码接入）、`docs/A2UI-agent驱动UI技术方案.md`（卡片协议 canonical 设计）。

---

## 1. 一页结论

| 你想要的效果 | 用哪条协议 | Agent 侧要做什么 | 前端侧要做什么 |
| --- | --- | --- | --- |
| 工具执行前让用户审批（approve/deny） | Responses 标准 `mcp_approval_request` | 配置审批策略，或 LangGraph `interrupt()` 返回 tool approval 语义的 value | 渲染审批卡，点选后回传 `mcp_approval_response` |
| 业务表单/选择/人工确认（等待用户输入后继续） | 平台扩展 `response.ksadk.approval_request` + `ksadk_resume` | 图节点里 `interrupt({...})`，value 里放要展示的信息 | 渲染业务卡片（表单/按钮），提交 `ksadk_resume` |
| 纯展示型业务卡片（不等待） | `tool_result` / 消息 payload 自带 JSON | 写一个"渲染工具"，返回结构化 JSON | 按 tool name 识别 payload，映射到自研组件 |
| Agent 声明式 UI（A2UI catalog 组件树） | A2UI surface 事件（0.8.4 core 已实现，agent 侧接线能力见 §6） | （能力就绪后）`display_ui` / `request_ui_input` | 实现 A2UI renderer，消费 `a2ui.surface.*` 事件 |

**核心原则：协议由 ksadk 承担，业务代码只定义"暂停点 + 暂停信息"，前端只做"渲染 + 回传"。** 双方都不需要理解 `Command(resume=...)`、checkpoint、事件存储这些内部机制。

---

## 2. 总体链路

```
┌──────────────┐   /v1/responses 或 RunAgent (SSE)   ┌──────────────────┐
│  自研前端      │ ──────────────────────────────────▶ │  ksadk 运行时      │
│  (React/Vue)  │ ◀────────────────────────────────── │  LangGraphRunner  │
│               │   response.output_text.delta /      │        │          │
│  - 文本流渲染   │   output_item.added /               │  ┌─────▼──────┐   │
│  - 审批卡      │   mcp_approval_request /            │  │ 用户的       │   │
│  - 业务卡片    │   response.ksadk.approval_request   │  │ LangGraph 图 │  │
│  - 回传 resume │ ──────── mcp_approval_response 或 ─▶ │  │ interrupt() │  │
└──────────────┘          ksadk_resume               │  └────────────┘   │
                                                     └──────────────────┘
```

恢复闭环的机制（前端无需感知，但建议理解）：

1. LangGraph 节点调 `interrupt(value)` → 图在 checkpoint 处挂起，本轮 SSE 以 `response.incomplete`（`reason=approval_required`）结束；
2. 前端渲染卡片，用户操作后回传 `mcp_approval_response` / `ksadk_resume`；
3. 运行时识别为 resume，`LangGraphRunner` 转成 `Command(resume=value)` 注入挂起的 `interrupt()` 调用点，图继续执行，SSE 恢复输出。

---

## 3. Agent 业务开发部分

### 3.1 项目结构与声明

```text
expense_agent/
  agent.py          # 组装 StateGraph，暴露 root_agent + ksadk_prepare_state
  state.py
  nodes.py
agentengine.yaml
requirements.txt
```

`agentengine.yaml`：

```yaml
name: expense-agent
framework: langgraph
entry_point: expense_agent/agent.py
agent_variable: root_agent
```

`agent.py`：

```python
from langgraph.graph import END, StateGraph

from .state import AgentState
from .nodes import ask_expense_info, submit_expense
from .state_adapter import ksadk_prepare_state  # 必须在本模块 re-export！

workflow = StateGraph(AgentState)
workflow.add_node("ask_expense_info", ask_expense_info)
workflow.add_node("submit_expense", submit_expense)
workflow.set_entry_point("ask_expense_info")
workflow.add_edge("ask_expense_info", "submit_expense")
workflow.add_edge("submit_expense", END)

root_agent = workflow.compile()
```

> ⚠️ `ksadk_prepare_state` 只会在 `entry_point` 模块顶层被查找。放在别的文件时必须在 `agent.py` re-export，否则 resume 分支失效。

`state_adapter.py`（resume 分支的固定写法）：

```python
def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    if session_context.get("is_resume"):
        # resume 时返回值会作为 Command(resume=...) 的 value
        # 直接送回 interrupt() 调用点，不要返回完整 state
        return payload.get("input")
    return build_normal_state(payload, session_context)
```

### 3.2 模式 A：通用业务卡片（表单/确认/选择，等待输入）

节点里用 LangGraph 原生 `interrupt()`，**value 就是前端要渲染的卡片数据**：

```python
# nodes.py
from langgraph.types import interrupt

async def ask_expense_info(state: AgentState) -> dict:
    if state.get("amount"):          # 已经填过就跳过
        return {}

    decision = interrupt({
        # ---- 以下字段是给前端渲染卡片的，schema 完全由业务自定 ----
        "card_type": "expense_form",          # 业务自定义卡片类型名
        "title": "报销申请",
        "fields": [
            {"name": "amount", "label": "金额（元）", "type": "number", "required": True},
            {"name": "reason", "label": "事由", "type": "text", "required": True},
        ],
        "submit_label": "提交报销",
        # 建议附上的协议字段（前端回传时要带回去）：
        "prompt": "请填写报销信息",
        # ---- 用于非 MCP 语义的 interrupt，无需 tool approval 结构 ----
    })

    # decision 就是 ksadk_resume.value（经过 ksadk_prepare_state resume 分支）
    return {"amount": decision["amount"], "reason": decision["reason"]}
```

要点：

- `interrupt()` 的 value 里**必须**包含前端渲染所需的一切（不要指望前端再发请求补数据）；
- 恢复后 `interrupt()` 的返回值就是前端回传的 `value`，直接写业务逻辑即可；
- 并行节点有多个 interrupt 时，回传要带 `interrupt_id`（见 §5.3）。

### 3.3 模式 B：工具执行审批卡（MCP / tool approval）

如果挂起语义是"这个工具调用要不要执行"，让 interrupt value 携带 tool 审批信息，运行时会输出 Responses 标准的 `mcp_approval_request`：

```python
async def dangerous_step(state: AgentState) -> dict:
    decision = interrupt({
        "type": "mcp_approval_request",
        "tool_name": "delete_records",
        "arguments": {"ids": [1, 2, 3]},
        "description": "即将删除 3 条记录，是否继续？",
        "server_label": "ksadk",
    })
    # decision = {"type": "mcp_approval_response", "approve": True, "reason": "..."}
    if not decision.get("approve"):
        return {"cancelled": True}
    ...
```

业务代码不需要自己拼 `approval_request_id`——运行时（`stream_mapping.py` 的 interrupt 分支）会生成 id、透传 `description` / `review_configs.allowed_decisions`（如 `["approve","deny","edit"]`，前端可以据此显示"修改参数"按钮）。

### 3.4 模式 C：纯展示卡片（不等待）

最简单的方式：定义一个"渲染工具"，让模型调用它输出结构化卡片数据，前端按 tool name 识别渲染：

```python
def render_flight_card(
    flight_no: str, departure: str, arrival: str, price: float
) -> dict:
    """查询航班信息并展示为卡片。

    Args:
        flight_no: 航班号
        departure: 出发城市
        arrival: 到达城市
        price: 票价（元）
    """
    return {
        "card": {
            "type": "flight",                     # 前端按这个 type 路由到自研组件
            "flight_no": flight_no,
            "departure": departure,
            "arrival": arrival,
            "price": price,
            "actions": [                          # 可选：前端可渲染的按钮
                {"name": "book", "label": "预订"},
            ],
        }
    }
```

前端在 `tool_result` 事件里读到 `result.card.type == "flight"` 就渲染 `<FlightCard>`。**注意**：这种方式没有运行时协议约束，payload schema 由业务团队自行约定并保持前后端一致；带交互按钮时属于"装饰"，真要驱动 agent 继续执行仍需走 §3.2/§3.3 或 `render_flight_card` 之外再提供一个 `book_flight` 工具让模型再调。

### 3.5 模式 D：A2UI 声明式卡片（能力预览）

A2UI 是本仓的 canonical 卡片协议：agent 用 basic catalog 组件（`Card/Text/Form/Select/RadioGroup/CheckboxGroup/ApprovalBar`）声明 surface，前端实现 renderer，live 与 replay 渲染一致。

**0.8.4 现状（请如实传达给团队）**：`ksadk/a2ui/` 的语义模型（`Surface`/`Component`/白名单校验）、`A2UICore`（`display_ui` / `request_ui_input` / `submit_action`）、事件通道（`a2ui.surface.*`）已实现并有 conformance 测试；但 **agent 业务侧的自动注入/runner 桥接尚未全量开放**（canonical 排期在后半年迭代，见 `docs/A2UI-agent驱动UI技术方案.md`）。当前生产对接请以 §3.2/§3.3/§3.4 三条路径为主，A2UI 可提前按 §6 设计前端 renderer 组件结构，届时平滑切换。

---

## 4. 前端开发部分：调用与渲染

### 4.1 三个入口

| 入口 | 方法 | 什么时候用 |
| --- | --- | --- |
| `POST /v1/responses` | SSE（`stream: true`） | 标准 OpenAI Responses 兼容；审批走标准协议 |
| `POST /agentengine/api/v1/RunAgent` | 同步 JSON 或 SSE（`Stream: true`） | 平台托管入口，带 Session/User 管理 |
| `GET /agentengine/api/v1/SubscribeRunEvents?SessionId=...` | SSE 长连接 | **自研 UI 推荐**：断线续订 + 历史回放；配合 `ListSessionEvents`（支持 `EventTypes`/`AfterSeqId` 分页）拉历史 |

`RunAgent` 请求体关键字段（`RunAgentActionRequest`）：

```json
{
  "AgentId": "expense-agent",
  "Messages": [{"role": "user", "content": "帮我提交报销"}],
  "UserId": "u_1001",
  "SessionId": null,          // 首轮不传，从响应拿；后续带上保持多轮
  "ApiFormat": "responses",
  "Stream": true
}
```

### 4.2 SSE 事件与渲染映射（/v1/responses 流）

| SSE `event` | payload 要点 | 前端动作 |
| --- | --- | --- |
| `response.created` / `response.in_progress` | response_id, session_id | 建 response 上下文；**保存 session_id** |
| `response.output_item.added` (message) | output_index | 创建文本气泡占位 |
| `response.output_text.delta` | `{"delta": "..."}` | 追加文本 |
| `response.output_item.added` (function_call) | name, call_id | 渲染"正在调用工具 xx" |
| `response.ksadk.tool_call` / `response.ksadk.tool_result` | 工具名、参数、结果 | 工具过程 UI；`tool_result` 里的业务 `card` JSON 渲染卡片（§3.4） |
| `response.output_item.added` (type=`mcp_approval_request`) | id, name, arguments, description, allowed_decisions | **渲染审批卡**（§5.2） |
| `response.ksadk.approval_request` | `interrupt_info`（通用 HITL） | **渲染业务卡片**（§5.3） |
| `response.incomplete` | `incomplete_details.reason="approval_required"`，`ksadk_interrupt` 原始信息 | 本轮结束，进入"等待用户操作"态 |
| `response.completed` / `response.failed` / `response.cancelled` | 最终 payload / 错误 | 收尾 / 报错 |

事件名前缀 `response.ksadk.*` 是平台扩展（对应代码 `_stream_responses_semantic_events`），标准字段尽量用标准事件。

### 4.3 历史与刷新

页面刷新/重新进入会话：

1. `POST ListSessionEvents`（带 `SessionId`、`AfterSeqId` 游标、可选 `EventTypes`）拉历史事件重放渲染；
2. 若最后一轮是 `approval_required` 未决状态，恢复出可交互卡片（resume 信息就在事件里）；
3. `GET SubscribeRunEvents?SessionId=...` 订阅后续增量（SSE，断线后用游标续订）。

---

## 5. 前端卡片实现（可直接用的代码）

### 5.1 SSE 消费骨架（浏览器原生 EventSource 不支持 POST，用 fetch stream）

```ts
async function runAgent(body: object, onEvent: (ev: {event: string; data: any}) => void) {
  const res = await fetch("/v1/responses", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ stream: true, ...body }),
  });
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
      let event = "message", data = "";
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent({ event, data: JSON.parse(data) });
    }
  }
}
```

### 5.2 审批卡（mcp_approval_request → mcp_approval_response）

```tsx
function ApprovalCard({ item, sessionId, onResponse }: {
  item: McpApprovalRequestItem; sessionId: string; onResponse: () => void;
}) {
  const decisions = (item as any).allowed_decisions ?? ["approve", "deny"];
  const send = async (approve: boolean) => {
    await runAgent({
      session_id: sessionId,                       // resume 必须带同一 session_id
      input: [{
        type: "mcp_approval_response",
        id: item.id,
        approval_request_id: item.id,
        approve,
        reason: approve ? "approved by user" : "denied by user",
      }],
      stream: true,
    }, handleEvent);                               // resume 后继续收流式输出
    onResponse();
  };
  return (
    <div className="card approval">
      <b>{item.name}</b>
      <pre>{item.arguments}</pre>
      {item.description && <p>{item.description}</p>}
      <button onClick={() => send(true)}>允许</button>
      <button onClick={() => send(false)}>拒绝</button>
    </div>
  );
}
```

事件分发：

```ts
switch (ev.event) {
  case "response.output_item.added":
    if (ev.data.item?.type === "mcp_approval_request")
      setPendingApproval(ev.data.item);            // 渲染审批卡
    else if (ev.data.item?.type === "message")
      appendBubble(ev.data.item);
    break;
  case "response.output_text.delta":
    appendText(ev.data.delta); break;
  case "response.ksadk.approval_request":
    setPendingBizCard(ev.data.interrupt_info); break;
  case "response.incomplete":
    if (ev.data.response?.incomplete_details?.reason === "approval_required")
      setWaiting(true);                            // 本轮挂起，等卡片操作
    break;
  case "response.completed":
    finalize(ev.data); break;
}
```

### 5.3 业务卡片（response.ksadk.approval_request → ksadk_resume）

```tsx
function ExpenseFormCard({ interrupt, sessionId, onDone }: {
  interrupt: KsadkInterrupt; sessionId: string; onDone: () => void;
}) {
  // interrupt_info 里的字段 = agent interrupt() 的 value
  // （response.ksadk.approval_request 的 interrupt_info，或
  //   response.incomplete.incomplete_details.ksadk_interrupt）
  const { card_type, title, fields, submit_label, interrupt_id } = interrupt as any;

  const submit = async (values: Record<string, any>) => {
    await runAgent({
      session_id: sessionId,
      input: [{
        type: "ksadk_resume",
        interrupt_id,                              // 并行 interrupt 时必带；单一 interrupt 可省
        value: values,                             // agent 端 interrupt() 的返回值
      }],
      stream: true,
    }, handleEvent);
    onDone();
  };

  return (
    <div className="card biz">
      <h3>{title}</h3>
      {fields?.map(f => (
        <label key={f.name}>{f.label}
          <input type={f.type} required={f.required}
                 onChange={e => values[f.name] = e.target.value} />
        </label>
      ))}
      <button onClick={() => submit(values)}>{submit_label ?? "提交"}</button>
    </div>
  );
}
```

> `interrupt_info` 的具体字段就是 agent `interrupt({...})` 里写的对象加上运行时补充的 `interrupt_id` 等。前端按业务约定（如 `card_type`）做组件路由，未知 `card_type` 渲染兜底 JSON 树。

### 5.4 纯展示卡片（tool_result）

```tsx
// response.ksadk.tool_result 事件
case "response.ksadk.tool_result": {
  const card = ev.data.result?.card;   // agent render 工具返回的 JSON
  if (card?.type === "flight") appendCard(<FlightCard data={card} />);
  else if (card) appendCard(<GenericJsonCard data={card} />);
  break;
}
```

---

## 6. A2UI 前端 renderer 预设计（为协议升级铺路）

A2UI 的前端契约（canonical §5.4，`ksadk/a2ui/renderer.py` 是参考实现）：

1. **消费** `a2ui.surface.begin / update / end` 事件，用 `payload["surface"]` 重建 `Surface`（`surface_id` + `components` 组件树 + `data_model`）；同一 surface_id 重复出现按 `item.updated`（replace）整体替换状态。
2. **组件映射**：catalog 类型 → 自研组件，props 白名单如下（0.8.4 `BASIC_CATALOG`）：

   | 类型 | 允许 props | 建议交互 |
   | --- | --- | --- |
   | `Card` | title, body, footer, children | 容器卡 |
   | `Text` | text, variant | 文本（variant 区分标题/正文） |
   | `Form` | title, fields, submit_label | 表单提交 |
   | `Select` | label, options, value | 下拉单选 |
   | `RadioGroup` | label, options, value | 单选组 |
   | `CheckboxGroup` | label, options, value | 多选组 |
   | `ApprovalBar` | tool_name, summary, approve_label, deny_label | 审批条（approve/deny 动作） |

3. **安全规则（协议要求，必须遵守）**：未知组件类型/未知 catalog → 渲染 placeholder；props 是纯数据，**不执行任何动态代码**。
4. **回传**：用户点选产生 `{"action_id", "surface_id", "name", "component_id", "interaction_id"?}` 提交到 `SubmitA2UIAction`；命中 pending interaction（`request_ui_input` 产生）则 agent 在挂起点拿到结果并 resume。

如果 §5.3 的业务卡片组件按"props 驱动、数据进组件"的方式写（上面示例已经是），将来接 A2UI 只需要加一层事件到组件的路由，组件本身可复用。

---

## 7. 端到端案例：报销审批 Agent

**场景**：用户说"帮我提交报销" → agent 卡片收集金额/事由 → 提交前人工确认 → 执行完成。

Agent 侧（`nodes.py` 关键节点）：

```python
from langgraph.types import interrupt

async def collect_info(state: AgentState) -> dict:
    info = interrupt({
        "card_type": "expense_form",
        "title": "报销申请",
        "fields": [
            {"name": "amount", "label": "金额（元）", "type": "number", "required": True},
            {"name": "reason", "label": "事由", "type": "text", "required": True},
        ],
        "submit_label": "下一步",
    })
    return {"amount": info["amount"], "reason": info["reason"]}

async def confirm_and_submit(state: AgentState) -> dict:
    ok = interrupt({
        "card_type": "confirm",
        "title": "确认提交",
        "prompt": f"金额 ¥{state['amount']}，事由：{state['reason']}，确认提交？",
        "options": [{"name": "confirm", "label": "确认"}, {"name": "cancel", "label": "取消"}],
    })
    if ok.get("name") != "confirm":
        return {"status": "cancelled"}
    result = submit_to_erp(state["amount"], state["reason"])   # 业务调用
    return {"status": "done", "ticket_id": result["ticket_id"]}
```

前端时序：

1. `POST /v1/responses`，`input="帮我提交报销"`；
2. 收到 `response.ksadk.approval_request`（interrupt_info 即 `expense_form` 数据）+ `response.incomplete` → 渲染表单卡；
3. 用户填写提交 → `input=[{type:"ksadk_resume", interrupt_id, value:{amount, reason}}]` 续传；
4. agent 走到确认节点再次 interrupt → 第二张确认卡（同 session 内重复 2-3）;
5. 用户点"确认" → resume → `response.completed`，最终文本里带 ticket_id。

---

## 8. 常见问题

**Q：多轮卡片后 session 怎么维持？**
`/v1/responses` 首轮响应里带 `session_id`，之后每次调用（普通消息和 resume）都带上它。`previous_response_id` 可选补充，不能替代 `session_id`。

**Q：refresh 后卡片还能操作吗？**
能。未决 interaction 是持久化的（durable ledger），通过 `ListSessionEvents` 重放即可恢复出可交互卡片，提交 resume 走正常通道。

**Q：resume 请求发了但没反应？**
按顺序检查：① `session_id` 是否和挂起轮一致；② 是否有并行 interrupt 没带 `interrupt_id`；③ `ksadk_prepare_state` 的 resume 分支是否返回了 `payload.get("input")` 而不是完整 state。

**Q：并行节点同时挂起多个 interrupt？**
LangGraph 支持定向恢复：resume value 带 `interrupt_id` 时按 id 定向投递，不带时投给单个 interrupt。前端若检测到同一轮出现多个 `approval_request`，应把所有卡片渲染出来并让用户分别操作。

**Q：卡片样式怎么定制？**
协议只约束事件 schema 和回传格式，样式完全自由。建议组件按"数据驱动 props"实现（§5 示例风格），同时兼容 §6 的 A2UI 组件形态。

**Q：想升级到 A2UI 协议怎么办？**
当前（0.8.4）先按 §5 实现，保持组件数据驱动；A2UI agent 侧接线开放后（关注 `docs/A2UI-agent驱动UI技术方案.md` 与后续 release plan），新增 `a2ui.surface.*` 事件路由即可，catalog 组件与 §5.3 卡片一一对应。
