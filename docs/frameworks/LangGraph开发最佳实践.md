# LangGraph 开发最佳实践

本文档面向使用 `ksadk-python` 开发 LangGraph Agent 的业务开发者，重点说明：

- LangGraph 工程应该如何暴露 `root_agent`
- `ksadk_prepare_state(payload, session_context)` 应该放在哪里、怎么写
- 平台上下文、附件、OCR、知识库、长期记忆如何进入 LangGraph state
- `interrupt()` / `Command(resume=...)` 在 AgentEngine 运行时里的职责边界
- `/v1/responses` 下 MCP approval 与通用 human-in-the-loop 的恢复写法

本文档只讨论业务代码接入方式，不展开远程部署、网关鉴权和托管 UI 协议。接口字段 contract 见 [远程Agent运行时接口说明](../远程Agent运行时接口说明.md)。

## 1. 推荐结论

LangGraph Agent 推荐按以下原则接入：

1. `agentengine.yaml` 的 `framework` 写 `langgraph`
2. `entry_point` 指向真正加载 Agent 的 Python 模块
3. 在 `entry_point` 模块顶层暴露 `root_agent`
4. 自定义 state 图优先写 `ksadk_prepare_state(payload, session_context)`
5. 业务节点只消费自己定义的 state 字段，不直接解析平台 event store
6. 如果使用 `interrupt()`，业务代码只定义暂停点和 resume payload 的业务含义
7. 是否把下一次请求转成 `Command(resume=...)` 由平台协议层决定，不由业务代码猜测

`LangGraphRunner` 的设计目标是薄适配：尽量透传 LangGraph 原生能力，只在 `resume=True` 时把输入包装成 `langgraph.types.Command(resume=...)`。

## 2. 推荐目录结构

一个最小但清晰的项目可以这样组织：

```text
my_agent/
  agent.py
  state.py
  nodes.py
  prompts.py
agentengine.yaml
requirements.txt
```

其中：

- `agent.py`：组装 `StateGraph`，暴露 `root_agent`，并 re-export `ksadk_prepare_state`
- `state.py`：定义 `TypedDict` / reducer
- `nodes.py`：放 LangGraph 节点逻辑
- `prompts.py`：放 prompt 模板或系统指令

简单项目也可以只保留一个 `agent.py`。关键不是文件数量，而是 `entry_point` 模块必须能被 `ksadk` 直接加载。

## 3. agentengine.yaml

示例：

```yaml
name: langgraph-demo
framework: langgraph
entry_point: my_agent/agent.py
agent_variable: root_agent
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `framework` | 必须为 `langgraph` |
| `entry_point` | Python 模块文件路径，运行时会加载这个模块 |
| `agent_variable` | 模块里的 LangGraph compiled graph 变量名，通常是 `root_agent` |

## 4. root_agent 暴露方式

`root_agent` 应该是 LangGraph 编译后的图：

```python
from langgraph.graph import END, StateGraph

from .state import AgentState
from .nodes import answer
from .state_adapter import ksadk_prepare_state


workflow = StateGraph(AgentState)
workflow.add_node("answer", answer)
workflow.set_entry_point("answer")
workflow.add_edge("answer", END)

root_agent = workflow.compile()
```

如果你把 `ksadk_prepare_state` 放在别的文件里，必须在 `entry_point` 模块 re-export：

```python
from .state_adapter import ksadk_prepare_state
```

`ksadk` 不会全项目扫描这个函数。它只会在 `entry_point` 对应模块上执行类似下面的查找：

```python
getattr(module, "ksadk_prepare_state", None)
```

所以以下写法不推荐：

- 放在别的文件里，但没有从 `entry_point` 模块导入
- 写成类方法
- 写在函数内部
- 运行时动态创建，但模块导入完成后顶层属性上拿不到

## 5. 平台给 LangGraph 的标准输入

进入 LangGraphRunner 前，平台会把一次请求整理成标准运行输入。常见字段如下：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `input` | `str` 或 `dict` | 当前输入；普通请求是文本，resume 请求是结构化恢复 payload |
| `history` | `list[dict]` | 多轮历史，已经过 transcript 投影和必要的 compaction |
| `input_content` | `list[dict]` | 当前 user turn 的 OpenAI Responses content blocks，例如 `input_text / input_image / input_file` |
| `input_messages` | `list[dict]` | OpenAI Responses 风格 message/input items；需要完整 role/content 结构时优先读这里 |
| `input_parts` | `list[dict]` | legacy/internal 归一化片段，保留 `text / inlineData / fileData`；用于兼容已有 runner |
| `attachments` | `list[dict]` | 当前会话最近有效附件上下文，兼容历史 fallback |
| `attachment_results` | `list[dict]` | 最近有效 OCR / 文本抽取 / 附件理解结果 |
| `current_attachments` | `list[dict]` | 当前最新 user turn 的附件列表，不包含历史 fallback |
| `current_attachment_results` | `list[dict]` | 当前最新 user turn 的附件理解结果 |
| `has_current_files` | `bool` | 当前最新 user turn 是否包含归一化后的 `inlineData` 或 `fileData`，包括 OpenAI `input_image / input_file` |
| `model` | `str` | 本轮显式模型名 |
| `model_metadata` | `dict` | 模型能力元数据 |
| `platform_context` | `dict` | `agent_id / user_id / session_id` 等平台身份 |
| `kb_context` | `dict` | 知识库召回上下文 |
| `memory_context` | `dict` | 长期记忆上下文 |
| `instructions` | `str` | 请求级系统/开发者指令 |
| `resume` | `bool` | 平台判断本轮是否为断点恢复 |

`input_content` / `input_messages` 是 runner 默认 canonical 输入，沿用 OpenAI Responses content block 形态；`input_parts` 是 legacy/internal normalized parts。`attachments / current_attachments / has_current_files` 等字段是 KsADK 提供给 LangGraph runner 的运行时上下文扩展，不属于 OpenAI 官方请求或响应字段。

对外协议不混写：`/v1/responses` 按 Responses 语义接收 `input_text / input_image / input_file`；`/v1/chat/completions` 保持 Chat Completions 语义，官方图片块使用 `text / image_url`。进入 runner 前，两条入口都会转换为 `input_content / input_messages`，同时生成兼容用 `input_parts`。KsADK 仍兼容 `inlineData / fileData` 老输入，但不要把它们当成 OpenAI Chat 官方字段。

## 6. 默认 messages-based 图

如果没有定义 `ksadk_prepare_state()`，运行时会自动把请求转换成 LangGraph 常见的 messages state：

```python
{
    "attachments": [...],
    "attachment_results": [...],
    "current_attachments": [...],
    "current_attachment_results": [...],
    "has_current_files": True,
    "input_content": [...],
    "input_messages": [...],
    "input_parts": [...],
    "model_metadata": {...},
    "messages": [
        SystemMessage(...),
        HumanMessage(...),
        AIMessage(...),
        HumanMessage(...),
    ],
}
```

说明：

- `messages` 是默认主上下文
- `input_content / input_messages / input_parts / attachments / attachment_results / current_attachments / current_attachment_results / has_current_files` 保留在 state 顶层
- 如果模型支持原生图片输入，最后一条 `HumanMessage.content` 可能是多模态 block 列表
- 如果模型不支持原生图片输入，最后一条 `HumanMessage.content` 通常是字符串

messages-based 图适合快速迁移。但如果你的业务需要稳定消费附件、OCR、平台身份、知识库或长期记忆，推荐显式写 `ksadk_prepare_state()`。

## 7. 推荐：自定义 ksadk_prepare_state

推荐在 `ksadk_prepare_state(payload, session_context)` 里把平台输入投影成业务 state。

```python
def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    if session_context.get("is_resume"):
        return payload.get("input")

    return {
        "query": payload["input"],
        "history": session_context["history"],
        "attachments": payload.get("attachments", []),
        "attachment_results": payload.get("attachment_results", []),
        "current_attachments": payload.get("current_attachments", []),
        "current_attachment_results": payload.get("current_attachment_results", []),
        "has_current_files": payload.get("has_current_files", False),
        "input_content": payload.get("input_content", []),
        "input_messages": payload.get("input_messages", []),
        "input_parts": payload.get("input_parts", []),
        "platform_context": session_context.get("platform_context"),
        "kb_context": session_context.get("kb_context"),
        "memory_context": session_context.get("memory_context"),
        "model_metadata": payload.get("model_metadata", {}),
    }
```

字段来源建议：

| 你想拿什么 | 推荐来源 |
| --- | --- |
| 当前用户输入 | `payload["input"]` |
| 当前输入 OpenAI canonical content | `payload["input_content"]` |
| 当前输入 OpenAI canonical messages | `payload["input_messages"]` |
| 当前输入 legacy/internal parts | `payload["input_parts"]` |
| 当前轮是否带文件 | `payload["has_current_files"]` |
| 当前轮附件引用 | `payload["current_attachments"]` |
| 当前轮 OCR / 文档抽取结果 | `payload["current_attachment_results"]` |
| 最近有效附件上下文 | `payload["attachments"]` |
| 最近有效 OCR / 文档抽取结果 | `payload["attachment_results"]` |
| 会话历史 | `session_context["history"]` |
| 平台身份 | `session_context["platform_context"]` |
| 知识库上下文 | `session_context["kb_context"]` |
| 长期记忆上下文 | `session_context["memory_context"]` |
| 是否断点恢复 | `session_context["is_resume"]`，只建议在 adapter 中判断，用来返回 resume payload |

不要在业务代码里读取平台内部 event store 来拼 history。平台已经把可喂给模型的历史投影成 `history`。

如果你的图使用 LangGraph `interrupt()`，`session_context["is_resume"]` 为 `True` 时，`ksadk_prepare_state` 的返回值会作为 `Command(resume=...)` 的值传回 interrupt 调用点，而不是作为新的 graph state 注入。因此推荐在 resume 分支直接返回 `payload["input"]`，不要继续返回完整业务 state。

## 8. 附件、OCR 和图片

`attachments` 更接近原始引用，但语义是最近有效附件上下文；如果只判断当前轮是否传了文件，请使用 `has_current_files` 或 `current_attachments`。

典型字段：

```python
{
    "display_name": "diagram.png",
    "mime_type": "image/png",
    "transport": "reference",
    "file_uri": "ksadk-upload://abc123.png",
    "data": "<base64-encoded-bytes>",  # 仅 transport="inline" 时存在
    "size_bytes": 1356,
    "storage_path": "/tmp/.../diagram.png",
    "is_text": False,
}
```

`attachment_results` 更适合业务逻辑消费，典型字段：

```python
{
    "display_name": "diagram.png",
    "mime_type": "image/png",
    "kind": "image",
    "status": "ok",
    "extraction_method": "image_ocr",
    "text": "KIMI E2E",
    "text_excerpt": "KIMI E2E",
}
```

推荐模式：

```python
def collect_attachment_texts(state: dict) -> list[str]:
    results = state.get("current_attachment_results") or state.get("attachment_results") or []
    return [
        item.get("text", "")
        for item in results
        if isinstance(item, dict) and item.get("text")
    ]
```

如果你要根据模型能力决定是否走原生多模态：

```python
def supports_image_input(state: dict) -> bool:
    model_metadata = state.get("model_metadata") or {}
    capabilities = model_metadata.get("capabilities") or {}
    return bool(capabilities.get("multimodal_input_image"))
```

如果只是需要当前轮图片 OCR 文本，不建议拆 `HumanMessage.content`，直接用 `current_attachment_results[*]["text"]` 更稳定；需要支持“继续分析上次附件”时再 fallback 到 `attachment_results`。

## 9. 知识库和长期记忆

平台可能按策略把知识库和长期记忆上下文注入：

```python
kb_context = state.get("kb_context") or {}
memory_context = state.get("memory_context") or {}

kb_text = kb_context.get("formatted_text", "")
memory_text = memory_context.get("formatted_text", "")
```

建议把它们当作外部上下文材料，而不是持久化状态源。业务图如果要写自己的记忆，应明确区分：

- 平台长期记忆召回：`memory_context`
- LangGraph 图内部状态：你的 `AgentState`
- 业务数据库：你自己的外部存储

## 10. interrupt 与断点恢复

LangGraph 原生支持在图节点中调用 `interrupt()` 暂停，并在下一次 `invoke` / `stream` 时用 `Command(resume=...)` 恢复。

在 AgentEngine 运行时里，职责边界是：

| 层级 | 职责 |
| --- | --- |
| LangGraph 业务代码 | 调用 `interrupt()`，定义暂停信息和 resume payload 的业务语义 |
| Responses / Hosted UI / API 层 | 接收用户审批或恢复输入，判断这是一次 resume |
| conversation runtime | 记录 `approval_request / approval_response`，向 runner 传 `resume=True` |
| LangGraphRunner | 薄适配：把 `resume=True` 转成 `Command(resume=...)` |

如果你定义了 `ksadk_prepare_state`，resume 请求也会经过这个 hook。此时 hook 的返回值就是 `Command(resume=...)` 里的 `resume` 值。推荐写法是：

```python
def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    if session_context.get("is_resume"):
        return payload.get("input")
    return build_normal_state(payload, session_context)
```

业务代码不应该：

- 自己判断“上一轮是不是暂停了”
- 自己构造平台 event store 查询
- 依赖 LangGraph 内部 `state.tasks[*].interrupts` 结构
- 要求客户端直接传 Python `Command`

业务代码应该：

- 在节点中用 `interrupt(value)` 暂停
- 让 `value` 包含前端或调用方需要展示的信息
- 在恢复后从 `Command(resume=...)` 传回的值继续执行业务逻辑

## 11. MCP 工具审批：Responses 标准语义

当 interrupt 表示 MCP/tool approval 时，运行时会按 OpenAI Responses 风格输出 `mcp_approval_request`，并以 `response.incomplete` 结束本轮。

客户端恢复时，应调用 `/v1/responses`，传入同一个 `session_id`，并把 `input` 写成 `mcp_approval_response`：

```json
{
  "session_id": "sess_xxx",
  "previous_response_id": "resp_xxx",
  "input": [
    {
      "type": "mcp_approval_response",
      "id": "mcprsp_xxx",
      "approval_request_id": "appr_xxx",
      "approve": true,
      "reason": "approved by user"
    }
  ],
  "stream": true
}
```

运行时会把它转换成 runner 输入：

```python
{
    "session_id": "sess_xxx",
    "resume": True,
    "input": {
        "type": "mcp_approval_response",
        "id": "mcprsp_xxx",
        "approval_request_id": "appr_xxx",
        "approve": True,
        "reason": "approved by user",
    },
}
```

LangGraphRunner 随后调用：

```python
Command(resume={
    "type": "mcp_approval_response",
    "id": "mcprsp_xxx",
    "approval_request_id": "appr_xxx",
    "approve": True,
    "reason": "approved by user",
})
```

注意：

- `session_id` 是当前运行时定位 LangGraph thread 的关键字段
- `previous_response_id` 按 Responses 语义保留到 metadata，但当前不能替代 `session_id`
- 客户端不需要知道 Python `Command`
- 业务代码只关心 resume payload 的业务含义

## 12. 泛化 human-in-the-loop：ksadk_resume

如果 interrupt 不是 MCP/tool approval，而是普通人工确认、补充信息或业务分支选择，运行时会使用平台扩展事件：

- 流式事件：`response.ksadk.approval_request`
- 结束事件：`response.incomplete`
- `incomplete_details.reason`: `approval_required`
- `incomplete_details.ksadk_interrupt`: 原始 interrupt 信息

恢复请求可以使用 `ksadk_resume`：

```json
{
  "session_id": "sess_xxx",
  "input": [
    {
      "type": "ksadk_resume",
      "interrupt_id": "intr_xxx",
      "value": {
        "approved": true,
        "answer": "继续"
      }
    }
  ],
  "stream": true
}
```

runner 收到的输入会是：

```python
{
    "session_id": "sess_xxx",
    "resume": True,
    "input": {
        "type": "ksadk_resume",
        "interrupt_id": "intr_xxx",
        "value": {
            "approved": True,
            "answer": "继续",
        },
    },
}
```

业务节点恢复后应按自己约定解析 `value`。

## 13. 完整可跑示例

下面示例演示：

- 自定义 `AgentState`
- 使用 `ksadk_prepare_state`
- 消费附件 OCR、平台上下文和模型能力
- 支持人工确认 interrupt / resume

```python
from __future__ import annotations

from typing import Annotated, Any, TypedDict
import operator

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt


class AgentState(TypedDict):
    query: str | dict[str, Any]
    history: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    attachment_results: list[dict[str, Any]]
    current_attachments: list[dict[str, Any]]
    current_attachment_results: list[dict[str, Any]]
    has_current_files: bool
    input_content: list[dict[str, Any]]
    input_messages: list[dict[str, Any]]
    input_parts: list[dict[str, Any]]
    platform_context: dict[str, Any] | None
    kb_context: dict[str, Any] | None
    memory_context: dict[str, Any] | None
    model_metadata: dict[str, Any]
    messages: Annotated[list[dict[str, str]], operator.add]


def _attachment_texts(state: AgentState) -> list[str]:
    return [
        item.get("text", "")
        for item in state.get("attachment_results", [])
        if isinstance(item, dict) and item.get("text")
    ]


def _is_approved(resume_value: Any) -> bool:
    if not isinstance(resume_value, dict):
        return False
    if resume_value.get("type") == "mcp_approval_response":
        return bool(resume_value.get("approve"))
    if resume_value.get("type") == "ksadk_resume":
        value = resume_value.get("value") or {}
        return isinstance(value, dict) and bool(value.get("approved"))
    return bool(resume_value.get("approved"))


def answer(state: AgentState) -> AgentState:
    query = state["query"]
    attachment_texts = _attachment_texts(state)
    model_metadata = state.get("model_metadata") or {}
    supports_image = bool(
        ((model_metadata.get("capabilities") or {}).get("multimodal_input_image"))
    )

    if "删除" in str(query):
        resume_value = interrupt(
            {
                "id": "confirm-delete",
                "message": "检测到删除操作，请确认是否继续。",
                "operation": "delete",
            }
        )
        if not _is_approved(resume_value):
            return {"messages": [{"role": "assistant", "content": "已取消删除操作。"}]}

    content = (
        f"query={query}; "
        f"supports_image={supports_image}; "
        f"attachment_texts={attachment_texts}"
    )
    return {"messages": [{"role": "assistant", "content": content}]}


def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    if session_context.get("is_resume"):
        return payload.get("input")

    return {
        "query": payload.get("input", ""),
        "history": session_context.get("history", []),
        "attachments": payload.get("attachments", []),
        "attachment_results": payload.get("attachment_results", []),
        "current_attachments": payload.get("current_attachments", []),
        "current_attachment_results": payload.get("current_attachment_results", []),
        "has_current_files": payload.get("has_current_files", False),
        "input_content": payload.get("input_content", []),
        "input_messages": payload.get("input_messages", []),
        "input_parts": payload.get("input_parts", []),
        "platform_context": session_context.get("platform_context"),
        "kb_context": session_context.get("kb_context"),
        "memory_context": session_context.get("memory_context"),
        "model_metadata": payload.get("model_metadata", {}),
        "messages": [],
    }


workflow = StateGraph(AgentState)
workflow.add_node("answer", answer)
workflow.set_entry_point("answer")
workflow.add_edge("answer", END)

root_agent = workflow.compile()
```

## 14. 常见反模式

### 14.1 在业务代码里猜是否 resume

不要通过读取数据库、检查上一轮输出文本、解析 event store 来判断是否恢复。平台会把恢复请求转成 `resume=True`。

### 14.2 让客户端直接传 LangGraph Command

外部协议应该是 JSON。`Command(resume=...)` 是 Python / LangGraph runner 内部调用形态，不应该暴露给客户端。

### 14.3 依赖 LangGraph 内部状态结构

不要依赖 `state.tasks[*].interrupts` 这类内部结构做业务判断。LangGraph 版本升级后这些结构可能变化。

### 14.4 把 `HumanMessage.content` 当成永远是字符串

多模态模型下它可能是 content block 列表。除非你明确在做 messages-native agent，否则优先使用 `payload / session_context`。

### 14.5 用 attachments 判断当前轮是否传文件

`attachments` 是最近有效附件上下文，可能来自历史 fallback。当前轮是否传文件看 `has_current_files`，当前轮附件列表看 `current_attachments`；OCR、文档抽取、压缩包摘要仍优先看对应的 `current_attachment_results` 或 `attachment_results`。

## 15. 检查清单

上线前建议确认：

- `agentengine.yaml` 的 `framework` 是 `langgraph`
- `entry_point` 指向包含 `root_agent` 的模块
- `root_agent` 是 compiled graph
- `ksadk_prepare_state` 在 `entry_point` 模块顶层可见
- 自定义 state 明确包含业务需要的上下文字段
- 当前轮文件判断使用 `has_current_files / current_attachments`
- 附件理解优先读取 `current_attachment_results / attachment_results`
- 多模态分支读取 `model_metadata.capabilities`
- interrupt 恢复只依赖 resume payload，不依赖平台内部事件结构
- `/v1/responses` 恢复调用传同一个 `session_id`
