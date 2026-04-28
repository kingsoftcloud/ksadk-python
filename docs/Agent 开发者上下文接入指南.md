# Agent 开发者上下文接入指南

本文档面向使用 `ksadk-python` 开发 Agent 业务逻辑的开发者，重点说明：

- 通过 `/v1/responses`、Hosted UI 或 `RunAgent` 调用时，运行时会把哪些上下文喂给 Agent
- LangGraph、LangChain、ADK 这三类主路径里，开发者应该从哪里拿：
  - 当前输入
  - 多轮历史
  - 图片 / 附件上下文
  - OCR / 文档抽取结果
  - 平台上下文
  - 知识库与长期记忆上下文
  - 模型能力元数据
- 什么时候应该解析 `HumanMessage`，什么时候不应该

本文档不讨论云端部署、权限治理或外部产品逻辑；只聚焦 agent 业务代码如何接上下文。

## 1. 先给结论

如果你是 Agent 业务开发者，**最推荐的接入方式不是直接拆 `messages[-1]`，而是使用 `ksadk_prepare_state()` / `ksadk_prepare_input()` 明确接收平台传入的标准上下文。**

优先级建议：

1. `LangGraph`
   - 自定义 `ksadk_prepare_state(payload, session_context)`
2. `LangChain`
   - 自定义 `ksadk_prepare_input(payload, session_context)`
3. `ADK`
   - 使用 runner 已构造好的 `Part` / session 能力
4. 只有在你确实做 messages-native agent 时，再直接解析 `HumanMessage`

## 2. 运行时到底会给 Agent 什么

进入 runner 前，`ksadk` 会把一次请求整理成标准运行输入。核心字段包括：

- `input`
- `history`
- `input_parts`
- `attachments`
- `attachment_results`
- `model`
- `model_metadata`
- `platform_context`
- `kb_context`
- `memory_context`
- `instructions`

这些字段不是每个 framework 都以同样方式消费，但它们是当前平台提供给 Agent 的标准上下文来源。

### 2.1 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `input` | `str` | 当前这一轮的标准文本输入 |
| `history` | `list[dict]` | 当前多轮会话历史，已经过 transcript 投影 / compaction |
| `input_parts` | `list[dict]` | 原始输入片段列表，保留 `text / inlineData / fileData` 等结构 |
| `attachments` | `list[dict]` | 当前轮或当前会话有效附件列表 |
| `attachment_results` | `list[dict]` | 平台对附件做过的抽取结果，例如 OCR / 文本提取 |
| `model` | `str` | 当前请求显式使用的模型名 |
| `model_metadata` | `dict` | 模型元数据，可能来自请求显式传入，也可能来自上游 `/v1/models` 自动解析 |
| `platform_context` | `dict` | 平台上下文，例如 `agent_id / user_id / session_id` |
| `kb_context` | `dict` | 知识库召回构造的上下文 |
| `memory_context` | `dict` | 长期记忆构造的上下文 |
| `instructions` | `str` | 本轮额外系统/开发者指令 |

## 3. 多轮会话历史是怎么来的

平台不是要求前端每次把完整对话都重传回来，而是通过：

- `session_id`
- conversation event store
- transcript 投影
- 必要时 compaction

来恢复当前会话历史。

你在 Agent 业务里看到的 `history`，通常已经不是“原始数据库所有消息”，而是：

- 更早历史被压缩成 summary checkpoint
- 最近若干轮保持原始 user / assistant 消息
- 特殊事件（tool call / tool result / approval / attachment）保留成可解释文本占位

所以：

- 如果你只需要“语义历史”，用 `history`
- 如果你需要“精细结构化上下文”，看 `attachments / attachment_results / input_parts`

## 4. 图片 / 附件上下文怎么来的

### 4.1 `attachments`

`attachments` 是平台解析输入 part 后得到的附件引用列表。典型字段：

```python
{
    "display_name": "diagram.png",
    "mime_type": "image/png",
    "transport": "reference",   # 或 "inline"
    "file_uri": "ksadk-upload://...",
    "size_bytes": 1356,
    "storage_path": "/tmp/.../diagram.png",
    "is_text": False,
}
```

说明：

- `transport="inline"`：调用方直接传了 `inlineData`
- `transport="reference"`：调用方先 `UploadFile`，再传 `fileData.fileUri`

### 4.2 `attachment_results`

这是平台附件理解管线产出的结果，比 `attachments` 更适合业务逻辑消费。典型字段：

```python
{
    "display_name": "diagram.png",
    "mime_type": "image/png",
    "transport": "reference",
    "file_uri": "ksadk-upload://...",
    "size_bytes": 1356,
    "kind": "image",
    "status": "ok",
    "warnings": [],
    "extraction_method": "image_ocr",
    "text_excerpt": "KIMI E2E",
    "text": "KIMI E2E",
    "image": {"ocr_engine": "rapidocr_onnxruntime"}
}
```

推荐使用方式：

- 想拿 OCR 文本：读 `attachment_results[*]["text"]`
- 想区分图片 / 文档 / 压缩包：读 `kind`
- 想看平台有没有降级或失败：读 `status / warnings / extraction_method`

## 5. 模型能力元数据怎么来的

`model_metadata` 的来源优先级是：

1. 请求里显式传入的 `model_metadata`
2. runtime 用 `OPENAI_BASE_URL / OPENAI_API_KEY` 查询上游 `/v1/models`
3. 本地默认兜底

当前最值得关注的字段是：

```python
{
    "id": "kimi-k2.6",
    "architecture": {
        "input_modalities": ["文字", "图片", "视频"],
        "output_modalities": ["文字"]
    },
    "capabilities": {
        "multimodal_input_image": True,
        "multimodal_input_video": True,
        "multimodal_input_file": False,
        "function_calling": True,
        "structured_output": True,
        "context_caching": True
    },
    "limits": {...},
    "pricing": {...}
}
```

业务代码里最常用的判断是：

```python
supports_image = bool(
    (((model_metadata or {}).get("capabilities") or {}).get("multimodal_input_image"))
)
```

## 6. LangGraph 怎么拿上下文

### 6.0 `ksadk_prepare_state` 放在哪

`ksadk` 不会全项目扫描这个函数，它只会在 **`agentengine.yaml` 的 `entry_point` 对应模块** 上做一次 `getattr(module, "ksadk_prepare_state")`。

这意味着：

- 可以放在 `entry_point` 对应的 `agent.py` 里
- 可以放在同一个模块文件的任意位置
- 只要它最终是这个模块的**顶层可见符号**就行

正确示例：

```python
# agent.py

def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    ...

root_agent = graph.compile()
```

或者：

```python
# agent.py
from .state_adapter import ksadk_prepare_state

root_agent = graph.compile()
```

只要 `agent.py` 这个被加载的模块上，最终存在 `ksadk_prepare_state` 属性即可。

不推荐 / 不生效的放法：

- 放在别的文件里，但没有从 `entry_point` 模块 re-export
- 写在类方法里
- 写在函数内部
- 运行时动态创建，但模块导入完成后外层属性上拿不到

判断标准很简单：

```python
module.ksadk_prepare_state
```

如果这句在 `entry_point` 模块上拿不到，`ksadk` 就不会调用你的 hook。

### 6.1 默认 messages-based 图

如果你没有写 `ksadk_prepare_state()`，平台会自动把输入转成：

```python
{
  "attachments": [...],
  "attachment_results": [...],
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

其中最后一条 `HumanMessage`：

- 对纯文本模型通常是字符串
- 对支持图片输入的多模态模型，可能是多模态 content blocks 列表

### 6.2 推荐：显式写 `ksadk_prepare_state`

最推荐的写法是：

```python
def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    return {
        "query": payload["input"],
        "history": session_context["history"],
        "attachments": payload.get("attachments", []),
        "attachment_results": payload.get("attachment_results", []),
        "platform_context": session_context["platform_context"],
        "kb_context": session_context["kb_context"],
        "memory_context": session_context["memory_context"],
        "model_metadata": payload.get("model_metadata"),
    }
```

然后你的节点里就可以非常直接地拿：

```python
def agent_node(state: dict):
    query = state["query"]
    attachments = state.get("attachments", [])
    attachment_results = state.get("attachment_results", [])
    model_metadata = state.get("model_metadata", {})
    platform_context = state.get("platform_context", {})
```

### 6.3 `session_context` 当前有哪些字段

`ksadk_prepare_state(payload, session_context)` 里，当前可稳定拿到：

`payload` 包含：

- `input`
- `attachments`
- `attachment_results`
- `input_parts`
- `model_metadata`
- `instructions`

`session_context` 包含：

- `session_id`
- `history`
- `platform_context`
- `kb_context`
- `memory_context`
- `is_resume`

如果你想拿附件、图片 OCR 结果、原始输入片段，优先从 `payload` 读取；如果你想拿会话历史、平台身份、知识库和长期记忆上下文，从 `session_context` 读取。

### 6.4 一个完整可跑的 LangGraph Demo

下面这个例子演示一个最小但完整的 LangGraph agent：

- 使用自定义 `TypedDict`
- 使用 `ksadk_prepare_state()` 接平台上下文
- 把历史、附件 OCR 结果、模型能力都投影进 state
- 节点里根据是否支持图片输入走不同分支

```python
from __future__ import annotations

from typing import TypedDict, Annotated, Any
import operator

from langgraph.graph import StateGraph, END


class AgentState(TypedDict):
    query: str
    history: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    attachment_results: list[dict[str, Any]]
    platform_context: dict[str, Any] | None
    kb_context: dict[str, Any] | None
    memory_context: dict[str, Any] | None
    model_metadata: dict[str, Any]
    messages: Annotated[list[dict[str, str]], operator.add]


def analyze(state: AgentState) -> AgentState:
    query = state["query"]
    attachment_results = state.get("attachment_results", [])
    model_metadata = state.get("model_metadata", {})
    capabilities = model_metadata.get("capabilities") or {}
    supports_image = bool(capabilities.get("multimodal_input_image"))

    ocr_texts = [
        item.get("text", "")
        for item in attachment_results
        if isinstance(item, dict) and item.get("text")
    ]

    summary_parts = [
        f"query={query}",
        f"supports_image={supports_image}",
        f"ocr_texts={ocr_texts}",
    ]

    return {
        "messages": [
            {
                "role": "assistant",
                "content": " | ".join(summary_parts),
            }
        ]
    }


def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    return {
        "query": payload["input"],
        "history": session_context["history"],
        "attachments": payload.get("attachments", []),
        "attachment_results": payload.get("attachment_results", []),
        "platform_context": session_context.get("platform_context"),
        "kb_context": session_context.get("kb_context"),
        "memory_context": session_context.get("memory_context"),
        "model_metadata": payload.get("model_metadata", {}),
        "messages": [],
    }


workflow = StateGraph(AgentState)
workflow.add_node("analyze", analyze)
workflow.set_entry_point("analyze")
workflow.add_edge("analyze", END)

root_agent = workflow.compile()
```

配套 `agentengine.yaml` 只需要保证：

```yaml
name: my-agent
framework: langgraph
entry_point: my_agent/agent.py
agent_variable: root_agent
```

这个 demo 的意义是：

- 你不必从 `messages[-1]` 猜图片上下文
- 你可以稳定地从 `payload / session_context` 投影出自己真正想要的 state
- 这样 graph 节点逻辑和平台输入 contract 是解耦的

## 7. LangChain 怎么拿上下文

推荐定义：

```python
def ksadk_prepare_input(payload: dict, session_context: dict) -> dict:
    return {
        "question": payload["input"],
        "history": session_context["history"],
        "attachments": payload.get("attachments", []),
        "attachment_results": payload.get("attachment_results", []),
        "model_metadata": payload.get("model_metadata"),
    }
```

然后业务链或 runnable 自己决定：

- 要不要把 `history` 变成 prompt
- 要不要优先消费 `attachment_results[*]["text"]`
- 要不要在支持多模态的模型上直接消费 `input_parts`

说明：

- LangChain 当前不保证所有 agent 自动原生图片直通
- 所以如果你要做稳定多模态 LangChain agent，建议自己在 hook 里显式处理

## 8. ADK 怎么拿上下文

ADK 路径下，平台会优先把附件转成底层 SDK 的 `Part`：

- 文本 -> `types.Part(text=...)`
- 图片 / 其他附件 -> `types.Part.from_bytes(...)`

所以对支持原生多模态的 ADK 模型，图片会优先按 bytes part 进入底层 SDK。

ADK 侧开发者通常更应该依赖：

- ADK 自己的 session 机制
- 当前传进来的 `new_message.parts`
- 平台附加的 state delta

## 9. 通用运行时上下文：`get_current_invocation_context()`

如果你在 tool、helper 或平台公共逻辑里，希望不通过 state/hook 也能拿到当前调用上下文，可以用：

```python
from ksadk.runtime_context import get_current_invocation_context

ctx = get_current_invocation_context()
if ctx:
    print(ctx.agent_id)
    print(ctx.user_id)
    print(ctx.session_id)
    print(ctx.model)
    print(ctx.attachments)
    print(ctx.attachment_results)
    print(ctx.kb_context)
    print(ctx.memory_context)
```

`PlatformInvocationContext` 当前包含：

- `agent_id`
- `user_id`
- `session_id`
- `history`
- `input_parts`
- `attachments`
- `attachment_results`
- `runner_type`
- `model`
- `kb_context`
- `memory_context`

## 10. `HumanMessage` 什么时候需要自己解析

只有在你明确做的是 messages-native graph / prompt pipeline 时，才建议自己拆 `HumanMessage`。

### 10.1 纯文本模型

```python
HumanMessage(content="请分析这张图片\\n\\n[上传文件引用: ...]")
```

### 10.2 原生多模态模型

```python
HumanMessage(
  content=[
    {"type": "text", "text": "请分析这张图片"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
  ]
)
```

### 10.3 推荐解析方式

```python
def parse_human_message_content(content):
    result = {"texts": [], "images": []}

    if isinstance(content, str):
        result["texts"].append(content)
        return result

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                result["texts"].append(str(block.get("text") or ""))
            elif block.get("type") == "image_url":
                result["images"].append(str((block.get("image_url") or {}).get("url") or ""))
    return result
```

但再次强调：

- 如果你只是想拿图片 OCR 文本、附件摘要、平台上下文
- 不推荐优先拆 `HumanMessage`
- 更推荐直接用 `attachments / attachment_results / session_context`

## 11. 常见接入模式

### 模式 A：只关心最终文本输入

适合简单问答 agent：

```python
def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    return {"query": payload["input"]}
```

### 模式 B：同时关心附件 OCR

适合简历、票据、截图理解类 agent：

```python
def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    attachment_texts = [
        item.get("text", "")
        for item in payload.get("attachment_results", [])
        if isinstance(item, dict) and item.get("text")
    ]
    return {
        "query": payload["input"],
        "attachment_texts": attachment_texts,
    }
```

### 模式 C：按模型能力分支

适合既支持多模态模型、又要兼容纯文本模型的 agent：

```python
def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    model_metadata = payload.get("model_metadata") or {}
    capabilities = model_metadata.get("capabilities") or {}
    return {
        "query": payload["input"],
        "supports_image": bool(capabilities.get("multimodal_input_image")),
        "attachments": payload.get("attachments", []),
        "attachment_results": payload.get("attachment_results", []),
    }
```

## 12. 常见坑

### 12.1 把 `HumanMessage.content` 当成永远是字符串

这是最常见坑。多模态模型下，它可能是 `list[block]`。

### 12.2 只看 `attachments`，不看 `attachment_results`

`attachments` 更像原始引用；真正适合业务消费的是 `attachment_results`。

### 12.3 想拿业务上下文，却只盯着 `messages[-1]`

更稳的做法是：

- `history` 看多轮语义
- `attachments / attachment_results` 看文件上下文
- `platform_context` 看平台身份
- `kb_context / memory_context` 看召回上下文

### 12.4 以为客户端每轮都会重传完整历史

不会。多轮历史恢复主要靠 `session_id + server 侧 event store`。

### 12.5 以为图片一定会原生直通

不会。是否走原生图片输入取决于：

- 请求显式 `model_metadata`
- 或 runtime 自动查到的模型目录能力

纯文本模型会走附件/OCR/文本降级。

## 13. 推荐实践

1. 对 LangGraph / LangChain，优先写 `ksadk_prepare_state()` / `ksadk_prepare_input()`
2. 只有 messages-native agent 才去直接拆 `HumanMessage`
3. 想理解图片内容时，优先读 `attachment_results`
4. 想做多模态分流时，读 `model_metadata.capabilities`
5. 想依赖多轮历史时，确保前端持续复用 `session_id`

## 14. 相关文档

- [远程Agent运行时接口说明](./远程Agent运行时接口说明.md)
- [ksadk使用文档](./ksadk使用文档.md)
- [ksadk技术设计](./ksadk技术设计.md)
