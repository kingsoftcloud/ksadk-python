# LangGraph Agent + 自研 UI 人机交互卡片对接方案

> 面向 Agent 业务开发和自研前端开发。
>
> 代码基线：`ksadk-python 0.8.4`。本文按当前仓库实现整理，协议字段以运行时实际返回为准。
>
> 相关代码与文档：
> - LangGraph runner：`ksadk/runners/_langgraph_runner_streams.py`、`ksadk/runners/langgraph_runner.py`
> - Responses 流：`ksadk/conversations/runtime_streaming.py`
> - Responses/RunAgent 请求解析：`ksadk/conversations/runtime_payloads.py`、`ksadk/server/routes/openai_compat.py`、`ksadk/server/routes/run.py`
> - 远程接口说明：`docs/reference/远程Agent运行时接口说明.md`
> - LangGraph 业务接入：`docs/guides/LangGraph开发最佳实践.md`

## 1. 先选对接路径

| 需求 | 推荐路径 | Agent 侧 | 前端侧 | 当前状态 |
| --- | --- | --- | --- | --- |
| 工具执行前审批 | Responses `mcp_approval_request` / `mcp_approval_response` | 使用工具审批能力，或让 `interrupt()` 的 value 带 `tool_name`、参数等审批信息 | 渲染审批卡，按原 `approval_request_id` 回传决定 | 可用 |
| 业务表单、选择、人工确认，等待用户输入后继续 | Responses `response.ksadk.approval_request` + `ksadk_resume` | 节点调用 LangGraph `interrupt(value)`，value 放可渲染数据 | 渲染业务卡并回传 `ksadk_resume` | 可用 |
| 纯展示业务卡片，不暂停 Agent | `response.ksadk.tool_result` 的 `output` | 工具返回可序列化 JSON，约定业务 schema | 解析 `output` 后按业务类型渲染 | 可用，但 schema 由业务团队维护 |
| A2UI 声明式 surface | `a2ui.surface.*` / `a2ui.interaction` | 当前没有面向普通 LangGraph Agent 的自动注入和完整远程 action 接线 | 需要消费 A2UI v0.9 operation | 实验/内部能力，不作为本次客户交付主路径 |

审批卡和业务卡都走“事件流 + 独立恢复请求”。前端不构造 Python `Command`，业务代码也不查询 event store 来判断是否恢复。

### 1.1 当前能力、条件和缺口

| 能力 | 当前状态 | 对客户接入的含义 |
| --- | --- | --- |
| 文本、推理、工具调用、工具结果流式输出 | 已支持 | 前端按 Responses SSE 事件消费，并按 ID 去重 |
| 普通业务 `interrupt` + `ksadk_resume` | 已支持 | 可做表单、选择、确认和分支输入；业务 value 需要由 Agent 与前端共同约定 |
| MCP/tool approval | 已支持 | 优先使用 `mcp_approval_request` / `mcp_approval_response`，不要自行发明审批事件 |
| 历史回放、断线续订 | 已支持 | 使用 `ListSessionEvents` 和 `SubscribeRunEvents`；必须保存游标和 `InvocationId` |
| 后台运行、取消、checkpoint 恢复 | 条件支持 | 以 `GetAgentUiBootstrap` 返回的 capability 和 checkpoint descriptor 为准 |
| 持久化、多副本恢复 | 条件支持 | memory/SQLite 只适合进程内或单实例验证；生产需要共享且可恢复的后端 |
| 业务卡片字段契约 | 由业务方约定 | 当前 `interrupt(value)` 是业务自定义 JSON，KsADK 不替业务定义字段、组件或视觉样式 |
| 普通卡片提交的统一幂等键、回执和错误码 | 部分支持 | 前端要做禁重复提交、事件去重；本方案暂不承诺一个独立的卡片提交 receipt 接口 |
| 并行 interrupt 批量提交 | 待补齐 | 当前按 `interrupt_id` 定向恢复；多个并行卡应逐个提交，不能假设一次请求批量完成 |
| Responses 路径的 A2UI 远程 action | 尚未提供 | A2UI 只能做内部/AG-UI 方向验证，不能作为本次自研 UI 的主依赖 |

因此本次项目可以先交付“稳定的 JSON 交互数据协议”。KsADK 不定义卡片样式、CSS、组件树或前端渲染器；客户前端可以根据 `card_type`、业务字段和自己的设计系统自由渲染。若多个 Agent 需要共享业务字段语义，再由业务团队自行约定 `schema_version`、字段校验、错误结构和重放语义。

三方职责边界如下：

| 参与方 | 负责内容 |
| --- | --- |
| KsADK runtime | SSE/历史事件、`session_id`、`invocation_id`、`interrupt_id`、恢复、取消、checkpoint 和持久化能力声明 |
| Agent 业务代码 | 卡片需要展示的业务数据、业务校验、权限判断、外部副作用和幂等 |
| 客户前端 | 卡片布局、颜色、交互控件、响应式/无障碍体验、字段到组件的映射和错误展示 |

## 2. 运行闭环

```text
自研 UI
  │  POST /v1/responses（stream=true）
  │  或 POST /agentengine/api/v1/RunAgent（Stream=true）
  ▼
KsADK runtime ── LangGraphRunner ── 用户的 LangGraph 图
  ▲                                      │
  │ response.* SSE                       │ interrupt(value)
  │                                      ▼
  └── mcp_approval_response / ksadk_resume ── 图从挂起点继续
```

一次业务 `interrupt(value)` 的闭环如下：

1. 图节点调用 `interrupt(value)`，本轮产生审批/人工输入事件并以 `response.incomplete` 结束，`incomplete_details.reason` 为 `approval_required`。
2. 前端根据事件渲染卡片，用户提交后用同一个会话标识发送恢复请求。
3. runtime 将恢复输入转换为 LangGraph 的 `Command(resume=...)`，图从原挂起点继续输出。

恢复依赖 LangGraph checkpointer 和会话存储。内存 checkpointer 只适合进程内调试；跨进程、重启或多副本场景必须使用已声明为可恢复的持久化后端。

## 3. Agent 业务接入

### 3.1 项目声明

```text
expense_agent/
  agent.py
  state.py
  nodes.py
  state_adapter.py       # 只有自定义 State 时需要
agentengine.yaml
requirements.txt
```

```yaml
name: expense-agent
framework: langgraph
entry_point: expense_agent/agent.py
agent_variable: root_agent
```

```python
# agent.py
from langgraph.graph import END, StateGraph

from .nodes import ask_expense_info, submit_expense
from .state import AgentState
from .state_adapter import ksadk_prepare_state  # 需要自定义 State 时 re-export

workflow = StateGraph(AgentState)
workflow.add_node("ask_expense_info", ask_expense_info)
workflow.add_node("submit_expense", submit_expense)
workflow.set_entry_point("ask_expense_info")
workflow.add_edge("ask_expense_info", "submit_expense")
workflow.add_edge("submit_expense", END)

root_agent = workflow.compile()
```

`ksadk_prepare_state` 必须在 `entry_point` 对应模块顶层可见。它用于把**新一轮请求**投影成业务 State；当前 runner 在 resume 路径不会再次调用这个 hook，而是直接取恢复值并构造 `Command(resume=...)`。因此不要在 hook 里写“判断 resume 并返回 `payload["input"]`”的分支。

```python
# state_adapter.py

def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    platform = session_context.get("platform_context") or {}
    return {
        "query": payload.get("input", ""),
        "user_id": platform.get("user_id", ""),
        "session_id": platform.get("session_id", ""),
        "history": session_context.get("history") or [],
    }
```

如果图使用常见的 `messages` State，可以不定义 hook，由 runner 使用默认输入投影。无论哪种方式，`interrupt()` 的 value 都必须是可持久化、可 JSON 序列化的数据。

### 3.2 业务表单/确认卡：`interrupt` + `ksadk_resume`

```python
# nodes.py
from langgraph.types import interrupt


async def ask_expense_info(state: AgentState) -> dict:
    if state.get("amount") is not None and state.get("reason"):
        return {}

    answer = interrupt({
        "card_type": "expense_form",
        "title": "报销申请",
        "fields": [
            {"name": "amount", "label": "金额（元）", "type": "number", "required": True},
            {"name": "reason", "label": "事由", "type": "text", "required": True},
        ],
        "submit_label": "提交报销",
        "prompt": "请填写报销信息",
    })
    return {"amount": answer["amount"], "reason": answer["reason"]}
```

约定：

- `interrupt(value)` 的 value 同时承担展示数据和业务 schema；前端不应依赖额外的“查询卡片详情”请求。
- `value` 只是可序列化的业务数据载体；KsADK 不解析或渲染其中的 `card_type`、字段和样式，客户前端自行决定组件映射。
- 前端回传的 `ksadk_resume.value` 会作为 `interrupt()` 的返回值。
- 并行 interrupt 必须把事件中提供的 `interrupt_id` 一并回传；runtime 会按 ID 定向恢复。
- 未知 `card_type` 应降级为安全的 JSON/文本卡，不能执行 payload 中的代码或组件名。

### 3.3 工具审批卡：`mcp_approval_request`

当挂起的含义是“是否允许执行某个工具”，让 interrupt value 包含工具信息：

```python
from langgraph.types import interrupt


def dangerous_step(state: AgentState) -> dict:
    decision = interrupt({
        "tool_name": "delete_records",
        "arguments": {"ids": [1, 2, 3]},
        "description": "即将删除 3 条记录，是否继续？",
        "server_label": "ksadk",
    })
    if not decision.get("approve"):
        return {"cancelled": True}
    return {"deleted": True}
```

Responses 流会把包含 `tool_name` 的 interrupt 投影为 `response.output_item.added`，其中 `item.type` 为 `mcp_approval_request`。`item.id` 通常就是恢复所需的审批请求 ID；前端应把它作为 `approval_request_id` 原样回传。`description` 和 `allowed_decisions` 可能存在，也可能不存在，前端必须提供默认处理。

如果前端展示“修改参数”等扩展决定，应以事件中的 `allowed_decisions` 为准，并按对应的决定结构回传；不要只根据按钮文案推断服务端支持的决定类型。

### 3.4 纯展示卡片：工具结果

展示卡不应通过“装饰按钮”隐式驱动 Agent。工具返回 JSON 后，前端只展示；需要继续执行业务时，另走 `interrupt` 恢复或下一次明确的工具调用。

```python
def render_flight_card(flight_no: str, departure: str, arrival: str, price: float) -> dict:
    return {
        "card": {
            "type": "flight",
            "flight_no": flight_no,
            "departure": departure,
            "arrival": arrival,
            "price": price,
            "actions": [{"name": "book", "label": "预订"}],
        }
    }
```

工具结果在 Responses 流中位于 `response.ksadk.tool_result` 的 `output` 字段。`output` 可能是字符串，也可能是对象；前端先做类型判断，再决定是否 `JSON.parse`，不要读取不存在的 `result` 字段。

### 3.5 选择卡：单选、多选和业务分支

选择卡仍然是普通 `interrupt`，不要把选项点击伪装成工具调用。Agent 必须在恢复后再次校验选项是否仍然有效：

```python
from langgraph.types import interrupt


async def choose_payment_method(state: AgentState) -> dict:
    answer = interrupt({
        "schema_version": 1,
        "card_type": "single_choice",
        "title": "选择付款方式",
        "name": "payment_method",
        "options": [
            {"value": "corporate_card", "label": "公司卡"},
            {"value": "personal_reimbursement", "label": "个人垫付"},
        ],
        "required": True,
    })
    value = answer.get("value") if isinstance(answer, dict) else None
    allowed = {"corporate_card", "personal_reimbursement"}
    if value not in allowed:
        raise ValueError("付款方式无效或已失效")
    return {"payment_method": value}
```

前端回传的数据可以是：

```json
{
  "type": "ksadk_resume",
  "interrupt_id": "intr_payment_001",
  "value": {"value": "corporate_card"}
}
```

多选卡可以沿用同一协议，把 `value` 约定为字符串数组，例如 `{"value":["hotel","train"]}`；这只是业务数据约定，前端仍然可以使用自己的 checkbox、tag 或其他控件呈现。

选项、价格、库存、权限等动态数据不能只信任前端回传值；恢复后应重新从业务系统读取并校验。

### 3.6 两阶段表单：填写后再确认

一个节点可以连续触发多个 `interrupt`。LangGraph 恢复时会从原挂起点继续，业务代码不需要自己维护“当前第几步”：

```python
from decimal import Decimal
from langgraph.types import interrupt


async def collect_and_confirm_expense(state: AgentState) -> dict:
    form = interrupt({
        "schema_version": 1,
        "card_type": "expense_form",
        "title": "填写报销信息",
        "fields": [
            {"name": "amount", "type": "number", "required": True},
            {"name": "reason", "type": "text", "required": True},
        ],
    })
    amount = Decimal(str(form.get("amount", "0")))
    reason = str(form.get("reason") or "").strip()
    if amount <= 0 or not reason:
        raise ValueError("报销金额和事由不合法")

    decision = interrupt({
        "schema_version": 1,
        "card_type": "confirm",
        "title": "确认提交报销",
        "summary": {"amount": str(amount), "reason": reason},
        "actions": ["confirm", "cancel"],
    })
    if not isinstance(decision, dict) or decision.get("action") != "confirm":
        return {"cancelled": True}
    return {"amount": str(amount), "reason": reason, "confirmed": True}
```

前端必须把第二张卡当作新的 `interrupt_id` 处理。不能用第一张表单的 ID 继续提交确认结果。

### 3.7 并行人工输入：按 ID 定向恢复

如果图的并行节点同时调用 `interrupt()`，每张卡都可能有不同的 `interrupt_id`。前端需要：

1. 为每张卡保存 `(session_id, interrupt_id)`；
2. 用户提交哪张卡，就把哪张 ID 放进 `ksadk_resume.interrupt_id`；
3. 当前解析器一次只消费一个 resume item，多张并行卡逐个提交；
4. 不要只按 `card_type`、标题或工具名匹配卡片。

这条链路支持按 ID 定向恢复，但还没有“多个并行卡一次提交并返回统一批量回执”的客户协议。需要批量交互时，应在业务 Agent 内增加一个聚合 interrupt，让前端提交一个对象。

### 3.8 展示卡片上的按钮：新一轮请求还是恢复请求

展示卡片中的按钮不能默认等同于“继续当前 Agent”。按业务语义选择：

| 按钮语义 | 前端动作 | Agent 侧处理 |
| --- | --- | --- |
| 对当前挂起表单做提交/确认 | `ksadk_resume` | 当前图从 `interrupt()` 继续 |
| 对已完成航班卡发起预订 | 新的 `/v1/responses` 请求 | Agent 重新读取航班和价格并执行预订 |
| 对审批卡允许/拒绝工具 | `mcp_approval_response` | 继续工具审批链路 |
| 取消当前长任务 | `CancelRun` | 进入 cancelled，不能再当作普通文本发送 |

客户端传来的业务主键、价格和权限都不可信。新一轮请求应携带稳定的业务对象 ID，Agent 再从后端读取真实数据。

## 4. 前端入口和事件协议

### 4.1 入口选择

| 入口 | 请求格式 | 适用场景 |
| --- | --- | --- |
| `POST /v1/responses` | JSON，`stream: true` 返回 Responses SSE | 自研 UI 首选；字段采用小驼峰 |
| `POST /agentengine/api/v1/RunAgent` | JSON；`Stream: true` 返回 SSE | 需要显式传 `AgentId`、`UserId`、`SessionId` 的平台入口；字段采用 PascalCase |
| `POST /agentengine/api/v1/GetAgentUiBootstrap` | JSON | 读取 runtime、持久化、取消、checkpoint 和 transport capability |
| `POST /agentengine/api/v1/ListSessionEvents` | JSON，返回 `Data.Events` | 刷新/重进页面时按会话游标重放历史 |
| `GET /agentengine/api/v1/SubscribeRunEvents` | SSE | 后台运行、断线续订或 RunAgent 运行事件订阅；必须带 `SessionId`、`InvocationId`，可带 `AfterSeqId` |
| `POST /agentengine/api/v1/ListSessionCheckpoints` | JSON，返回 `Data.Checkpoints` | 列出可恢复 checkpoint；按钮以 `IsResumable` 为准 |
| `POST /agentengine/api/v1/GetCheckpointResumePreview` | JSON，返回恢复预览 | 恢复前展示 checkpoint 和工具 receipt 摘要 |
| `POST /agentengine/api/v1/ResumeRun` | JSON 或 SSE | 从持久化 checkpoint 继续；与 `ksadk_resume` 不是同一协议 |
| `POST /agentengine/api/v1/CancelRun` | JSON | 取消活跃 run；不要用普通文本消息代替 |

Responses 首轮请求：

```json
{
  "input": "帮我提交报销",
  "stream": true
}
```

RunAgent 首轮请求：

```json
{
  "AgentId": "expense-agent",
  "Messages": [{"role": "user", "content": "帮我提交报销"}],
  "UserId": "u_1001",
  "ApiFormat": "responses",
  "Stream": true
}
```

RunAgent 的恢复请求不要把 Responses 的 `input` 直接放到 `Messages`；应使用 `ApiFormat: "responses"`，并把 `mcp_approval_response` 或 `ksadk_resume` 放入 `ResponsesInput`。使用 `/v1/responses` 时则放入请求体的 `input`。

### 4.2 Responses SSE 事件

Responses 事件的 JSON 是事件 data 本身，`response.incomplete` 不再包一层 `response` 字段。

| 事件 | 关键字段 | 前端处理 |
| --- | --- | --- |
| `response.created` / `response.in_progress` | `id`、`status`、`session_id` | 建立本轮上下文并保存 session ID |
| `response.output_item.added`（`message`） | `output_index`、`item.id` | 按 item ID 创建文本项 |
| `response.output_text.delta` | `item_id`、`output_index`、`delta`，可有 `replace` | 按 item ID 追加；`replace=true` 时替换当前快照 |
| `response.output_item.added`（`function_call`）及 arguments 事件 | `call_id`、`name`、arguments delta/done | 展示工具调用过程，并按 `call_id` 去重 |
| `response.ksadk.tool_call` | `name`、`args`、可选展示字段 | 可选的工具过程增强事件，不应替代标准 function_call 事件 |
| `response.ksadk.tool_result` | `name`、`output`、`run_id` | 解析业务展示数据 |
| `response.output_item.added`（`mcp_approval_request`） | `id`、`name`、`arguments`、可选 `description`/`allowed_decisions` | 展示工具审批卡 |
| `response.ksadk.approval_request` | `interrupt_info` | 展示普通业务卡 |
| `response.incomplete` | `status="incomplete"`、`incomplete_details.reason`、`incomplete_details.ksadk_interrupt` | 结束本轮流，进入等待用户操作状态 |
| `response.completed` / `response.failed` / `response.cancelled` | 终态 response | 收尾、显示错误或取消状态 |

`response.ksadk.*` 是 KsADK 扩展事件；不要把它们当成 OpenAI 官方事件以外的任意代码执行入口。

### 4.3 前端 SSE 消费骨架

```ts
type SseEvent = { event: string; data: unknown };

async function runResponses(
  body: Record<string, unknown>,
  onEvent: (event: SseEvent) => void,
): Promise<void> {
  const response = await fetch("/v1/responses", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // 生产环境按实际认证方案注入 Authorization / Cookie / CSRF。
    },
    body: JSON.stringify({ ...body, stream: true }),
  });
  if (!response.ok || !response.body) {
    throw new Error(`Responses request failed: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consume = (chunk: string) => {
    const lines = chunk.replace(/\r\n/g, "\n").split("\n");
    let event = "message";
    const data: string[] = [];
    for (const line of lines) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (!data.length || data.join("\n") === "[DONE]") return;
    onEvent({ event, data: JSON.parse(data.join("\n")) });
  };

  for (;;) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    buffer = buffer.replace(/\r\n/g, "\n");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      consume(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
    }
    if (done) {
      if (buffer.trim()) consume(buffer);
      return;
    }
  }
}
```

生产实现还应支持取消请求（`AbortController`）、请求失败提示、重复事件去重和 `replace` 快照语义。

### 4.4 审批卡回传

```ts
async function approve(item: { id: string }, sessionId: string, value: boolean) {
  await runResponses({
    session_id: sessionId,
    input: [{
      type: "mcp_approval_response",
      id: `mcprsp_${crypto.randomUUID()}`,
      approval_request_id: item.id,
      approve: value,
      reason: value ? "approved by user" : "denied by user",
    }],
  }, handleEvent);
}
```

`id` 是本次响应项 ID，`approval_request_id` 是原审批请求 ID；不要把二者混为同一个业务主键。

### 4.5 业务卡回传

```tsx
import { useState } from "react";

type Field = { name: string; label?: string; type?: string; required?: boolean };
type InterruptInfo = {
  interrupt_id?: string;
  approval_request_id?: string;
  card_type?: string;
  title?: string;
  fields?: Field[];
  submit_label?: string;
};

function ExpenseFormCard({ info, sessionId }: { info: InterruptInfo; sessionId: string }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const interruptId = info.interrupt_id || info.approval_request_id;

  async function submit() {
    await runResponses({
      session_id: sessionId,
      input: [{
        type: "ksadk_resume",
        ...(interruptId ? { interrupt_id: interruptId } : {}),
        value: values,
      }],
    }, handleEvent);
  }

  return (
    <section className="card biz-card">
      <h3>{info.title || "请补充信息"}</h3>
      {(info.fields || []).map((field) => (
        <label key={field.name}>
          {field.label || field.name}
          <input
            type={field.type || "text"}
            required={field.required}
            value={values[field.name] || ""}
            onChange={(event) => setValues((current) => ({
              ...current,
              [field.name]: event.target.value,
            }))}
          />
        </label>
      ))}
      <button type="button" onClick={submit}>
        {info.submit_label || "提交"}
      </button>
    </section>
  );
}
```

`response.ksadk.approval_request` 和 `response.incomplete.incomplete_details.ksadk_interrupt` 可能同时携带同一份信息。前端以带 ID 的那份为准，并按 `(session_id, interrupt_id)` 去重。

### 4.6 错误、取消和终态处理

前端不要只用 HTTP 200 判断成功。至少按下面的状态收敛 UI：

| 状态 | 典型来源 | 前端动作 |
| --- | --- | --- |
| `in_progress` | `response.created` / `response.in_progress` | 显示生成中，允许取消 |
| `incomplete` | `response.incomplete`，`reason=approval_required` | 保存卡片和 ID，等待用户操作 |
| `completed` | `response.completed` | 关闭输入态，保留结果和可用的后续按钮 |
| `failed` | `response.failed` 或 HTTP 错误 | 展示可重试/联系业务的错误，不自动重复副作用 |
| `cancelled` | `response.cancelled` 或 `CancelRun` 成功 | 关闭当前运行，不把取消当成业务拒绝 |
| `409` | checkpoint 不可恢复、重复恢复等 | 停止自动重试，重新读取 capability/checkpoint 状态 |

业务 Agent 抛出的校验错误应尽量在恢复后转成下一张业务卡或明确的 `failed` 结果。前端不要把任意错误字符串当作可执行动作。

## 5. 刷新、断线和历史回放

推荐顺序：

1. 页面重进时调用 `POST /agentengine/api/v1/ListSessionEvents`，传 `SessionId` 和 `AfterSeqId`，从返回的 `Data.Events` 重建文字、工具、审批和卡片状态。
2. 如果本轮仍在运行，使用事件中的 `InvocationId` 订阅：

   ```text
   GET /agentengine/api/v1/SubscribeRunEvents
       ?SessionId=sess-123&InvocationId=inv-456&AfterSeqId=12
   ```

3. 每收到事件就推进会话级 `SeqId`；重连时从最后一个已确认的游标继续，按 `EventId` 或 `(InvocationId, SeqId)` 幂等去重。
4. 收到 `data: [DONE]` 或终态 `run_status` 后关闭订阅。

`ListSessionEvents` 是平台动作接口，返回结构是 `Data.Events`，事件字段通常为 `EventId`、`EventType`、`Content`、`Metadata`、`SeqId`、`InvocationId`。当前服务端列表可能按最新事件在前返回；前端应先按 `SeqId` 升序整理，再重放。不要把它当作 Responses SSE 的 `event/data` 格式直接复用。

审批卡能否在刷新后继续操作，取决于会话事件和 LangGraph checkpointer 是否都可恢复。UI 应读取 runtime capability；不能因为历史里出现过一条审批事件就无条件显示可提交按钮。

### 5.1 后台长任务：提交、订阅和回放

适合导出报表、批量同步、长时间检索等场景。`Background=true` 时，`RunAgent` 立即返回任务句柄，不建立当前 HTTP 响应的 SSE；前端再用 `SubscribeRunEvents` 订阅：

```json
{
  "AgentId": "report-agent",
  "SessionId": "sess-report-001",
  "InvocationId": "inv-report-001",
  "ApiFormat": "responses",
  "Background": true,
  "ResponsesInput": "导出本月销售报表"
}
```

前端处理顺序：

1. 保存返回的 `SessionId`、`RunId`、`InvocationId` 和 `SubscribeUrl`；
2. 订阅 `SessionId + InvocationId`，按 `AfterSeqId` 续接；
3. 页面刷新时先 `ListSessionEvents` 回放，再重新订阅；
4. 收到终态 `run_status` 或 `[DONE]` 后关闭订阅；
5. 需要取消时调用 `CancelRun`，不要发送一条“请停止”的普通用户消息。

后台运行是否可用、是否支持取消和跨副本回放，以 `GetAgentUiBootstrap` 返回的 `RunLifecycle`、`StopRun`、`RuntimeCapabilityMatrix` 为准。

### 5.2 checkpoint 恢复：和 `ksadk_resume` 区分

`ksadk_resume` 是 Responses 对当前 `interrupt()` 的恢复；`ResumeRun` 是平台控制接口，用于从已持久化的 checkpoint 恢复长任务或失败运行。两者不要混用：

```text
GetAgentUiBootstrap
        │  capability: CheckpointResume=true
        ▼
ListSessionCheckpoints(SessionId, OnlyResumable=true)
        ▼
GetCheckpointResumePreview(SessionId, RunId, CheckpointId)
        ▼
ResumeRun(AgentId, SessionId, RunId, CheckpointId, Stream/Background)
        ▼
SubscribeRunEvents(SessionId, InvocationId, AfterSeqId)
```

前端只使用服务端返回的 `IsResumable`、`ResumeStatus`、`ReplayAllowed`、`ExpiresAt` 和 `ResumeDisabledReason`。不要自己根据“历史上有 checkpoint”推导按钮是否可用。终态 checkpoint 返回 `noop` 时不是失败，不要无限重试。

### 5.3 普通卡片提交的可靠性边界

当前 runtime 对 checkpoint resume 提供同一 session/run 的并发互斥；普通 `/v1/responses` 的 `ksadk_resume` 仍需要前端和业务 Agent 一起保证可靠性：

- 卡片提交按钮在请求完成或失败前禁用；
- 为提交请求生成客户端 request ID，并写入业务日志/业务幂等键；
- 收到超时后先回放 `ListSessionEvents`，不要直接再次执行外部副作用；
- Agent 写 ERP、订单、退款等副作用前，使用业务主键或 receipt 做幂等检查；
- 如果需要服务端统一的 resume receipt、重复提交结果和批量提交协议，应作为 runtime 后续能力补齐。

## 6. A2UI 当前边界

仓库中的 `ksadk/a2ui/` 已有 `Surface`、`Component`、`A2UICore` 和 renderer/conformance 测试，但它不是普通 LangGraph Agent 自动获得的工具，也没有在本方案的远程 API 上提供一个可直接调用的通用 `SubmitA2UIAction` 路由。

当前 `GetAgentUiBootstrap` 对 Responses transport 明确返回 `Capabilities.A2UI=false`；只有配置了 AG-UI/CopilotKit transport 时，bootstrap 才可能返回 `A2UI=true`。这表示 A2UI 不是当前 Responses 自研 UI 的默认能力开关。

在 RuntimeEvent / SessionEvent / Studio projection 路径中，当前已观察到的对外投影是：

- `a2ui.surface.begin/update/end` 事件；
- payload 中的 `surfaceId` 和 `a2uiOperations`；
- operation 使用 A2UI v0.9 风格的 `createSurface`、`updateComponents`、`updateDataModel`、`deleteSurface`；
- 结构化输入使用 `a2ui.interaction` / `a2ui.action` 兼容投影。

因此本次客户交付不要按“直接读取 `payload.surface`、调用 `SubmitA2UIAction`”实现。若后续启用 A2UI，应先冻结远程 action 接口、catalog/version、权限校验和重放契约，再单独做端到端联调。未知组件只做安全占位，不执行动态代码。

## 7. 端到端报销示例

1. 前端向 `/v1/responses` 发送 `input="帮我提交报销"`、`stream=true`。
2. Agent 在 `collect_info` 节点调用 `interrupt({...})`。
3. 前端收到 `response.ksadk.approval_request`，并在 `response.incomplete` 中看到 `incomplete_details.reason="approval_required"`，渲染表单。
4. 前端回传：

   ```json
   {
     "session_id": "sess_xxx",
     "input": [{
       "type": "ksadk_resume",
       "interrupt_id": "intr_xxx",
       "value": {"amount": "128.50", "reason": "客户拜访交通费"}
     }],
     "stream": true
   }
   ```

5. 图继续执行；如果下一节点再次 `interrupt`，前端按新的 ID 渲染第二张卡。
6. 用户确认后，图执行 ERP 写入，最终收到 `response.completed`。外部副作用必须在业务侧做幂等，不能把“前端重复点击”当作不会发生。

## 8. 联调检查清单

Agent 业务开发：

- `framework: langgraph`、`entry_point` 和 `agent_variable` 与实际包结构一致。
- 自定义 State 时，`ksadk_prepare_state` 在 entry point 模块顶层可见，并只负责新一轮 State 投影。
- `interrupt(value)` 的 value 是 JSON 可序列化对象，包含前端需要的展示字段和版本化的业务字段。
- 如果业务方需要跨 Agent 复用数据协议，可自行约定 `schema_version`、`card_type` 和字段校验规则；这些字段不代表 KsADK 定义了前端组件或样式。
- 对 `ksadk_resume.value` 做业务校验，不能直接信任客户端提交的数据。
- 并行 interrupt 使用稳定的 `interrupt_id`；需要批量收集时在 Agent 内聚合成一个 interrupt。
- 工具副作用有幂等键、权限校验和重试策略。
- checkpointer 的持久化/共享能力满足目标部署拓扑。

前端开发：

- 保存 `session_id`、`response.id`、`InvocationId` 和最后确认的 `SeqId`。
- 以 `item_id`/`call_id`/`approval_request_id` 去重，不能只按工具名去重。
- `response.incomplete` 读取 `data.incomplete_details`，不是 `data.response.incomplete_details`。
- 工具结果读取 `data.output`，按字符串/对象分别处理。
- 审批响应分开填写新响应 `id` 与原请求 `approval_request_id`。
- 业务卡提交期间禁用重复提交，收到终态或错误后恢复状态。
- 首次进入页面先读取 `GetAgentUiBootstrap`；后台任务、取消和 checkpoint 按 capability 显示，不按接口名称猜测可用性。
- 后台任务保存 `RunId`、`InvocationId`、`SubscribeUrl` 和最后确认的 `SeqId`；刷新时先回放再续订。
- `ksadk_resume` 与 `ResumeRun` 分开实现，不能把普通 interrupt 的 ID 当成 checkpoint ID。
- 未知卡片类型、组件类型和字段类型安全降级，不执行动态脚本。
- 刷新时先回放历史，再按 `SessionId + InvocationId + AfterSeqId` 续订；把 Responses SSE 和 ListSessionEvents 的两种 wire shape 分开解析。
