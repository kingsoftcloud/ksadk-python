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
   - 具体项目结构、interrupt / resume、Responses approval 写法见 [LangGraph开发最佳实践](./LangGraph开发最佳实践.md)
2. `LangChain`
   - 自定义 `ksadk_prepare_input(payload, session_context)`
3. `ADK`
   - 使用 runner 已构造好的 `Part` / session 能力
4. 只有在你确实做 messages-native agent 时，再直接解析 `HumanMessage`

## 2. 运行时到底会给 Agent 什么

进入 runner 前，`ksadk` 会把一次请求整理成标准运行输入。核心字段包括：

- `input`
- `history`
- `input_content`
- `input_messages`
- `input_parts`
- `attachments`
- `attachment_results`
- `current_attachments`
- `current_attachment_results`
- `has_current_files`
- `model`
- `model_metadata`
- `platform_context`
- `kb_context`
- `memory_context`
- `instructions`

这些字段不是每个 framework 都以同样方式消费，但它们是当前平台提供给 Agent 的标准上下文来源。
`input_content` / `input_messages` 是 runner 默认 canonical 输入，使用 OpenAI Responses 风格 content blocks；`input_parts` 是 legacy/internal normalized parts，用于兼容已有 runner。`attachments`、`current_attachments`、`has_current_files` 等是 KsADK runner payload 扩展上下文，不属于 OpenAI 官方请求或响应字段。

对外协议不混写：`/v1/responses` 接收 OpenAI Responses 风格 `input_text / input_image / input_file`；`/v1/chat/completions` 保持 Chat Completions 风格 `messages`，官方多模态块优先使用 `text / image_url`。进入 runner 前，两条入口都会投影到同一套 `input_content / input_messages`，并额外生成兼容用 `input_parts`。KsADK 兼容扩展 `inlineData / fileData` 仍可用于老客户端，但不把它们声明成 OpenAI Chat 官方字段。

### 2.1 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `input` | `str` | 当前这一轮的标准文本输入 |
| `history` | `list[dict]` | 当前多轮会话历史，已经过 transcript 投影 / compaction |
| `input_content` | `list[dict]` | 当前 user turn 的 OpenAI Responses content blocks，例如 `input_text / input_image / input_file` |
| `input_messages` | `list[dict]` | OpenAI Responses 风格 message/input items；需要完整 role/content 结构的 runner 优先读这里 |
| `input_parts` | `list[dict]` | legacy/internal 归一化片段，保留 `text / inlineData / fileData`；用于兼容已有 runner，不作为 OpenAI 官方协议字段暴露 |
| `attachments` | `list[dict]` | 当前会话最近有效附件上下文，兼容历史 fallback，不应用来判断本轮是否传文件 |
| `attachment_results` | `list[dict]` | 最近有效附件理解结果，例如 OCR / 文本提取 |
| `current_attachments` | `list[dict]` | 当前最新 user turn 解析出的附件列表，不包含历史 fallback |
| `current_attachment_results` | `list[dict]` | 当前最新 user turn 的附件理解结果 |
| `has_current_files` | `bool` | 当前最新 user turn 是否包含归一化后的 `inlineData` 或 `fileData`，包括 OpenAI `input_image / input_file` |
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
- 如果你需要 OpenAI Responses 风格当前输入结构，优先看 `input_content / input_messages`
- 如果你需要兼容老 runner 的内部结构，再看 `input_parts`
- 如果你需要附件理解结果，看 `current_attachments / current_attachment_results / attachments / attachment_results`

## 4. 图片 / 附件上下文怎么来的

### 4.1 `attachments`

`attachments` 是当前会话最近有效附件上下文。它可能来自当前轮，也可能来自同一 session 中最近一次带附件的 user turn，主要用于“继续围绕上个附件追问”的兼容场景。

如果业务需要判断“本次问答是否带文件”，不要看 `attachments`，直接看 `has_current_files`；如果要当前轮附件列表，看 `current_attachments`。

典型字段：

```python
{
    "display_name": "diagram.png",
    "mime_type": "image/png",
    "transport": "reference",   # 或 "inline"
    "file_uri": "ksadk-upload://...",
    "data": "<base64-encoded-bytes>",  # 仅 transport="inline" 时存在
    "size_bytes": 1356,
    "storage_path": "/tmp/.../diagram.png",
    "is_text": False,
}
```

说明：

- `transport="inline"`：调用方直接传了 `inlineData`
- `transport="reference"`：调用方先 `UploadFile`，再传 `fileData.fileUri`

### 4.2 当前轮文件判断

推荐判断方式：

```python
has_file = bool(payload.get("has_current_files"))
current_files = payload.get("current_attachments", [])
```

如果需要兼容旧版本 KsADK，可以退回检查 `input_parts`：

```python
has_file = any(
    isinstance(part, dict)
    and (part.get("inlineData") is not None or part.get("fileData") is not None)
    for part in payload.get("input_parts") or []
)
```

### 4.3 `attachment_results`

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

- 想拿当前轮 OCR 文本：读 `current_attachment_results[*]["text"]`
- 想支持“围绕上次附件继续追问”：读 `attachment_results[*]["text"]`
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

LangGraph 的完整开发写法已经内化到框架专属文档：

- [LangGraph开发最佳实践](./LangGraph开发最佳实践.md)

这里只保留平台上下文接入的核心边界：

- 默认 messages-based 图可以不写 hook，运行时会自动构造 `messages` state
- 自定义 state 图推荐显式暴露 `ksadk_prepare_state(payload, session_context)`
- `ksadk_prepare_state` 必须在 `agentengine.yaml` 的 `entry_point` 对应模块顶层可见
- 附件、OCR、原始输入片段优先从 `payload` 读取
- 会话历史、平台身份、知识库、长期记忆优先从 `session_context` 读取
- LangGraph `interrupt()` 的恢复由平台协议层判断，业务代码不需要自己猜下一轮是否要 `Command(resume=...)`

最小推荐写法：

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
        "platform_context": session_context.get("platform_context"),
        "kb_context": session_context.get("kb_context"),
        "memory_context": session_context.get("memory_context"),
        "model_metadata": payload.get("model_metadata", {}),
    }
```

如果涉及 human-in-the-loop / MCP 工具审批 / `interrupt()` 断点恢复，请优先阅读 [LangGraph开发最佳实践](./LangGraph开发最佳实践.md) 的 interrupt 与 Responses approval 章节。

## 7. LangChain 怎么拿上下文

推荐定义：

```python
def ksadk_prepare_input(payload: dict, session_context: dict) -> dict:
    return {
        "question": payload["input"],
        "history": session_context["history"],
        "attachments": payload.get("attachments", []),
        "attachment_results": payload.get("attachment_results", []),
        "current_attachments": payload.get("current_attachments", []),
        "current_attachment_results": payload.get("current_attachment_results", []),
        "has_current_files": payload.get("has_current_files", False),
        "input_content": payload.get("input_content", []),
        "input_messages": payload.get("input_messages", []),
        "input_parts": payload.get("input_parts", []),
        "model_metadata": payload.get("model_metadata"),
    }
```

然后业务链或 runnable 自己决定：

- 要不要把 `history` 变成 prompt
- 要不要优先消费 `attachment_results[*]["text"]`
- 要不要在支持多模态的模型上直接消费 `input_content / input_messages`
- 是否需要兼容旧 runner 的 `input_parts`

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
    print(ctx.input_content)
    print(ctx.input_messages)
    print(ctx.attachments)
    print(ctx.attachment_results)
    print(ctx.current_attachments)
    print(ctx.has_current_files)
    print(ctx.kb_context)
    print(ctx.memory_context)
```

`PlatformInvocationContext` 当前包含：

- `agent_id`
- `user_id`
- `session_id`
- `history`
- `input_content`
- `input_messages`
- `input_parts`
- `attachments`
- `attachment_results`
- `current_attachments`
- `current_attachment_results`
- `has_current_files`
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
- 更推荐直接用 `has_current_files / current_attachments / attachment_results / session_context`

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
    results = payload.get("current_attachment_results") or payload.get("attachment_results") or []
    attachment_texts = [
        item.get("text", "")
        for item in results
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
        "current_attachments": payload.get("current_attachments", []),
        "has_current_files": payload.get("has_current_files", False),
    }
```

## 12. 常见坑

### 12.1 把 `HumanMessage.content` 当成永远是字符串

这是最常见坑。多模态模型下，它可能是 `list[block]`。

### 12.2 用 `attachments` 判断当前轮是否传文件

`attachments` 是最近有效附件上下文，可能来自历史 fallback。判断当前轮是否传文件用 `has_current_files`，当前轮附件列表用 `current_attachments`。

### 12.3 想拿业务上下文，却只盯着 `messages[-1]`

更稳的做法是：

- `history` 看多轮语义
- `has_current_files / current_attachments` 看当前轮文件
- `attachments / attachment_results` 看最近有效文件上下文和理解结果
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

- [LangGraph开发最佳实践](./LangGraph开发最佳实践.md)
- [远程Agent运行时接口说明](../reference/远程Agent运行时接口说明.md)
- [ksadk使用文档](./ksadk使用文档.md)
- [ksadk技术设计](../reference/ksadk技术设计.md)
