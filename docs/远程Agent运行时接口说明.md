# 远程Agent运行时接口说明

本文档基于当前 `master` 分支的真实代码实现整理，目标是说明：

- Agent 部署到远程 K8s / Serverless Pod 之后，最终通过 `PublicEndpoint` 对外暴露哪些接口
- 不同运行时类型的接口差异：通用 Agent、Hermes、OpenClaw
- 公共鉴权、公共 Header、流式行为、WebSocket 约束
- 各接口的请求体 / 响应体 shape

本文档只把当前代码里可以确认的 contract 写出来；对仓库中未完整定义、但依赖上游项目的 OpenClaw 原生接口，不做超出代码证据的推断。

## 1. 事实来源

本文档主要依据以下代码与文档：

- `agentengine-server/app/api/v1/actions/agent_actions.py`
- `agentengine-server/app/api/v1/actions/chat_actions.py`
- `agentengine-server/app/api/v1/actions/feedback_actions.py`
- `agentengine-server/app/gateway/api.py`
- `agentengine-server/app/gateway/router_service.py`
- `agentengine-server/docs/技术设计.md`
- `agentengine-server/docs/网关鉴权说明.md`
- `ksadk-python/ksadk/server/app.py`
- `ksadk-python/ksadk/server/api_models.py`
- `ksadk-python/ksadk/conversations/runtime.py`
- `ksadk-python/ksadk_runtime_common/workspace_files/*.py`
- `ksadk-python/deploy/hermes/runtime/app.py`
- `ksadk-python/deploy/hermes/README.md`
- `ksadk-python/deploy/openclaw/bootstrap.sh`
- `ksadk-python/deploy/openclaw-user-template/Dockerfile`

## 2. 入口模型

### 2.1 公网入口

远程 Agent 部署成功后，控制面 `GetAgent` 会返回：

- `QuickAccess.PublicEndpoint`

这个地址就是外部调用运行时接口时应使用的根地址。例如：

```text
http://ar-20260506162108-d30283cd.agent-pre.kspmas.ksyun.com
```

说明：

- 对外看到的是 `PublicEndpoint`
- 实际请求先进入 Ingress / Gateway，再由 `agentengine-server` 的 router 做鉴权和转发
- 因此“部署后暴露的接口”应以公网入口经过网关后可访问的路径为准，而不是简单把 Pod 内部监听端口当成外部 contract

### 2.2 内网入口

`GetAgent` 也可能返回：

- `QuickAccess.PrivateEndpoint`

这类地址用于内网访问，不作为本文主线。本文默认描述通过 `PublicEndpoint` 暴露的接口。

## 3. 鉴权与公共 Header

## 3.1 外部访问鉴权

当前数据面统一通过网关校验，外部调用主要有两种认证方式：

1. `Authorization: Bearer <api_key>`
2. `ae_ui_session` Cookie

其中：

- API/SDK/CLI 直连运行时接口时，使用 `Authorization: Bearer <api_key>`
- 浏览器经 dashboard share link 或 hosted UI 访问时，通常使用 `ae_ui_session` Cookie

代码证据：

- `agentengine-server/docs/网关鉴权说明.md`
- `agentengine-server/app/gateway/api.py`

### 3.1.1 Bearer Token 的含义

Bearer Token 有两种来源：

1. AgentEngine 为该 Agent 签发的 API Key，通常是 `ak-...` 或 `sk-...`
2. OpenClaw 在 `token` 模式下使用的 shared secret

对绝大多数自动化调用，推荐理解为：

```http
Authorization: Bearer <GetAgent 返回的 api_key>
```

### 3.1.2 Cookie 会话的适用场景

`ae_ui_session` 主要用于：

- `https://<PublicEndpoint>/chat`
- `https://<PublicEndpoint>/`
- share link 跳转后的浏览器会话

它不是给通用脚本调用运行时 API 设计的主接口。

## 3.2 公共请求 Header

### 3.2.1 通用 HTTP Header

建议按以下方式构造：

| Header | 是否必填 | 说明 |
| --- | --- | --- |
| `Authorization: Bearer <api_key>` | 外部 API 调用必填 | 由网关校验 |
| `Content-Type: application/json` | JSON 请求推荐 | `POST /v1/*`、`POST /agentengine/api/v1/*` 常用 |
| `Accept: application/json` | 非流式请求推荐 | 返回 JSON |
| `Accept: text/event-stream` | 流式请求推荐 | `stream=true` 时推荐显式声明 |

说明：

- 对于 `multipart/form-data` 上传，如 `UploadFile` / `AddWorkspaceFile`，`Content-Type` 由客户端自动生成 boundary
- 运行时应用本身没有在 `ksadk.server.app` 内显式校验 Bearer；鉴权发生在网关层

### 3.2.2 WebSocket Header

Hermes 终端 WebSocket 额外要求：

| Header | 是否必填 | 说明 |
| --- | --- | --- |
| `Authorization: Bearer <api_key>` | 公网访问建议携带 | 网关鉴权 |
| `Sec-WebSocket-Protocol: ks-terminal.v1` | 必填 | Hermes 终端子协议 |

如果缺少 `ks-terminal.v1`，Hermes runtime 会直接拒绝连接。

## 3.3 内部 Header 与外部调用边界

以下 Header 会在网关和运行时之间使用，但**不应由外部调用方手工构造**：

| Header | 用途 |
| --- | --- |
| `X-Auth-Agent-Id` | 网关鉴权后注入的 Agent ID |
| `X-Auth-Account-Id` | 网关鉴权后注入的账号 ID |
| `X-Auth-Framework` | 网关鉴权后注入的 framework |
| `X-Auth-Openclaw-Gateway-Mode` | OpenClaw 模式透传 |
| `X-Forwarded-Host` | 原始 Host 透传 |
| `x-forwarded-user` | OpenClaw trusted-proxy / workspace 代理链路使用 |
| `X-Hermes-Session-Token` | Hermes dashboard 内部 fetch shim 使用 |

外部用户应只关心：

- Bearer API Key
- Cookie Session
- WebSocket 子协议

## 4. 运行时类型矩阵

当前主线下，公网可见接口按运行时分为三类：

| 运行时类型 | 典型 framework | 主入口实现 | 对外特征 |
| --- | --- | --- | --- |
| 通用 Agent 运行时 | `adk` / `langchain` / `langgraph` / `deepagents` | `ksadk.server.app` | `/v1/*` + workspace files；公网 `/chat` 由独立 hosted UI 服务承载并调用 Hosted UI action 接口 |
| Hermes 托管运行时 | `hermes` | `deploy/hermes/runtime/app.py` 外层 wrapper | `/` dashboard、`/v1/*`、`/_ksadk/terminal/ws`、workspace files；公网 `/chat` 同样由独立 hosted UI 服务承载 |
| OpenClaw 托管运行时 | `openclaw` | OpenClaw gateway + ksadk 补丁 | 以 OpenClaw gateway 为主，平台额外挂出 workspace files |

## 5. 公网暴露范围总览

### 5.1 通用 Agent 运行时

公网入口可确认的主路径：

- `GET /health`
- `POST /v1/responses`
- `POST /v1/chat/completions`
- `GET /chat`
- `GET /build`
- `GET /deploy`
- `GET /agentengine/api/v1/AttachmentContent`
- `GET /agentengine/api/v1/GetWorkspaceFileContent`
- `POST /agentengine/api/v1/GetAgentUiBootstrap`
- `POST /agentengine/api/v1/CreateSession`
- `POST /agentengine/api/v1/GetSession`
- `POST /agentengine/api/v1/ListSessions`
- `POST /agentengine/api/v1/DeleteSession`
- `POST /agentengine/api/v1/ListSessionEvents`
- `GET /agentengine/api/v1/SubscribeRunEvents`
- `POST /agentengine/api/v1/RunAgent`
- `POST /agentengine/api/v1/ListSessionCheckpoints`
- `POST /agentengine/api/v1/PreviewCheckpointResume`
- `POST /agentengine/api/v1/ListToolReceipts`
- `POST /agentengine/api/v1/ResumeRun`
- `POST /agentengine/api/v1/CancelRun`
- `POST /agentengine/api/v1/UploadFile`
- `POST /agentengine/api/v1/ListWorkspaceFiles`
- `POST /agentengine/api/v1/AddWorkspaceFile`
- `POST /agentengine/api/v1/DeleteWorkspaceFile`
- `POST /agentengine/api/v1/ListAgentModels`
- `GET /agentengine/api/v1/ExportWorkspaceZip`
- `POST /run_sse`
- `GET/POST/DELETE /apps/{app_name}/users/{user_id}/sessions*`

注意：

- 并不是所有 `/agentengine/api/v1/*` 都会通过公网数据面暴露
- 网关只放行 Hosted UI 所需的那一小组 action
- 对 `PublicEndpoint` 而言，`POST /agentengine/api/v1/*` 这组 Hosted UI action 实际会被 router 代理回 `agentengine-server`，不是直接命中 runtime pod 的本地同名路由

### 5.2 Hermes 运行时

公网入口可确认的主路径：

- `GET /`
- `GET /health`
- `GET/POST/PUT/PATCH/DELETE/OPTIONS /v1/{path}`
- `GET/POST/PUT/PATCH/DELETE/OPTIONS /{path}`  
  这部分本质是 Hermes dashboard 与其 API 的代理入口
- `GET/HEAD/POST/DELETE /_ksadk/workspace/v1/*`
- `WS /_ksadk/terminal/ws`
- `GET /chat`

### 5.3 OpenClaw 运行时

当前代码中可以**准确确认**的平台追加 contract 只有：

- `/_ksadk/workspace/v1/*`：通过 ksadk sidecar / proxy 增加的文件接口

此外还可以确认：

- OpenClaw gateway 默认跑在 `8080`
- 鉴权模式支持 `trusted-proxy | token | none`
- 健康检查使用的是上游 gateway 的 `/healthz`

但 OpenClaw gateway 原生完整 API 面不是本仓当前代码独立定义的，因此本文不把其所有原生端点逐条列为平台 contract。

## 6. 通用 Agent 运行时详细接口

本节适用于原始 runtime 服务本身：

- `adk`
- `langchain`
- `langgraph`
- `deepagents`

底层实现：`ksadk-python/ksadk/server/app.py`

重要边界：

- 本节里的 `/v1/*`、`/health`、`/run_sse`、`/apps/.../sessions*` 是 runtime pod 自身实现
- 但对公网 `PublicEndpoint` 来说，`/agentengine/api/v1/*` Hosted UI action 以 `agentengine-server` facade 为准
- 因此本文后续会把“runtime 原始接口”和“公网 Hosted facade”拆开写

## 6.1 健康检查

### `GET /health`

用途：

- 检查运行时是否启动
- 返回当前 runner 识别出的 framework 和 agent 名

请求示例：

```bash
curl -H "Authorization: Bearer <api_key>" \
  "https://<PublicEndpoint>/health"
```

响应示例：

```json
{
  "status": "ok",
  "framework": "langgraph",
  "agent": "demo-agent"
}
```

## 6.2 OpenAI Responses 兼容接口

### `POST /v1/responses`

说明：

- 非流式返回 OpenAI Responses 风格 JSON
- 流式返回 `text/event-stream`

请求体字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `input` | `string | array` | 是 | 用户输入；字符串或 KOP 风格消息数组 |
| `model` | `string` | 否 | 本次调用显式模型 |
| `model_metadata` | `object` | 否 | 模型元数据 |
| `instructions` | `string` | 否 | 额外系统指令 |
| `metadata` | `object` | 否 | 请求级 metadata |
| `conversation` | `string | object` | 否 | OpenAI Responses 会话绑定字段；可传 `"conv_xxx"` 或 `{ "id": "conv_xxx" }`，runtime 会映射为内部会话 ID |
| `previous_response_id` | `string` | 否 | OpenAI Responses 上一轮 response id；不能和 `conversation` 同时使用 |
| `safety_identifier` | `string` | 否 | OpenAI 推荐的最终用户稳定标识；runtime 会映射为内部 user id 和 Langfuse UserID，建议传 hash 后值 |
| `prompt_cache_key` | `string` | 否 | OpenAI prompt cache 路由提示；runtime 当前保留到请求 metadata，不作为用户身份 |
| `user` | `string` | 否 | OpenAI deprecated 用户字段；仅在未传 `safety_identifier` 时作为兼容兜底 |
| `store` | `boolean` | 否 | OpenAI Responses 存储开关；runtime 当前保留到请求 metadata |
| `stream` | `boolean` | 否 | 是否流式 |
| `session_id` | `string` | 否 | ksadk legacy extension；兼容旧客户端。新接入应优先使用 `conversation` |

最小请求示例：

```json
{
  "input": "你好",
  "stream": false
}
```

带会话与模型示例：

```json
{
  "input": [
    {
      "role": "user",
      "content": [
        {
          "text": "请总结一下这份设计"
        }
      ]
    }
  ],
  "model": "glm-5.2",
  "stream": true,
  "conversation": "conv_customer_001",
  "safety_identifier": "hash_user_001"
}
```

会话字段边界：

- 官方兼容路径：连续对话传 `conversation`；最终用户标识传 `safety_identifier`。
- `previous_response_id` 只表达 Responses 链式上下文，不能和 `conversation` 同时使用。
- `session_id` 是 ksadk 早期扩展字段，仅为旧客户端保留；不要在新代码中把它当作 OpenAI 官方字段。
- 不要通过 `metadata.user_id`、`metadata.session_id` 或其他私有 metadata 约定传用户身份和会话身份。

推荐请求示例：

```json
{
  "model": "deepseek-v4-pro",
  "input": "帮我分析这张账单",
  "conversation": "conv_bill_20260525_001",
  "safety_identifier": "user_hash_001",
  "stream": false
}
```

图片与附件输入：

推荐写法：

- `/v1/responses` 推荐使用 OpenAI Responses content blocks：`input_text` / `input_image` / `input_file`
- runner 业务代码推荐读取 `payload["input_content"]` / `payload["input_messages"]`，这是 KsADK 默认 canonical 输入
- 判断当前轮是否传了图片或文件，推荐使用 `payload["has_current_files"]` 和 `payload["current_attachments"]`
- 读取当前轮 OCR、文档抽取、压缩包摘要，推荐使用 `payload["current_attachment_results"]`

兼容写法：

- 老客户端仍可使用 KsADK 兼容扩展 part 数组：`text` / `inlineData` / `fileData`
- runner 里仍保留 `payload["input_parts"]`，用于兼容已有 `text / inlineData / fileData` 业务代码
- `payload["attachments"]` / `payload["attachment_results"]` 仍保留，但语义是最近有效附件上下文，可能来自历史 fallback；不要用它判断当前最新 user turn 是否上传了文件
- `/v1/chat/completions` 对外仍保持 Chat Completions 语义，官方图片块使用 `text` / `image_url`；`inlineData` / `fileData` 在 Chat 入口只属于 KsADK 兼容扩展，不是 OpenAI Chat 官方能力

字段细节：

- `input_image.image_url` 支持远程图片 URL 或 `data:image/...;base64,...`，运行时会归一化为内部附件上下文
- `input_file.file_data` 会归一化为内部 `inlineData`；`input_file.file_url` / `input_file.file_id` 会归一化为内部 `fileData` 引用
- `inlineData` 适合旧客户端直接内联 base64 内容
- `fileData` 适合旧客户端先调用 `UploadFile`，再引用返回的 `ksadk-upload://...`
- 远程图片 URL 会作为引用保留，并可在支持原生图片输入的 LangGraph 路径下继续传给模型；KsADK 不会主动拉取远程图片或远程文件做 OCR / 文本提取。需要平台提取、OCR 或本地附件内容时，请使用 data URL、`file_data`、`inlineData` 或 `fileData`

图片示例（OpenAI Responses 风格 data URL）：

```json
{
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "请分析这张图片"
        },
        {
          "type": "input_image",
          "image_url": "data:image/png;base64,<base64-encoded-image-bytes>"
        }
      ]
    }
  ],
  "model": "glm-5.2",
  "stream": false
}
```

业务代码获取图片信息：

```python
def ksadk_prepare_input(payload, session_context):
    # 当前轮是否真的上传了图片/文件。不要用 attachments 判断当前轮，
    # attachments 可能是历史最近一次有效附件上下文。
    has_current_files = payload.get("has_current_files", False)
    current_attachments = payload.get("current_attachments", [])

    images = [
        item
        for item in current_attachments
        if str(item.get("mime_type", "")).startswith("image/")
    ]

    # OpenAI Responses canonical content，适合直接转给支持原生多模态的模型。
    input_content = payload.get("input_content", [])
    image_blocks = [
        block
        for block in input_content
        if block.get("type") == "input_image"
    ]

    return {
        "input": payload.get("input", ""),
        "images": images,
        "image_blocks": image_blocks,
    }
```

如果业务 agent 使用 LangGraph / LangChain 并且模型支持原生多模态，优先从 `input_content` 或 `input_messages` 读取 `input_image`，按底层模型 SDK 需要的消息格式继续传递；如果需要读取平台归一化后的附件元信息、OCR / 文档抽取结果，则读取 `current_attachments` 和 `current_attachment_results`。`input_parts`、`inlineData`、`fileData` 是 legacy/internal 兼容输入，仍可作为老客户端兜底。

多模态模型“看图”和平台 OCR 是两条不同链路：推荐让支持图片的模型直接消费 `input_image` / `input_content`，这样不需要在代码包里安装本地 OCR 依赖。平台本地 OCR 只用于需要把图片预先转成 `current_attachment_results[*].text` 的场景；源码构建默认不打包 OCR 二进制栈，如需启用请在构建环境设置 `KSADK_BUILD_ENABLE_ATTACHMENT_OCR=true`，或在项目 `requirements.txt` 中显式加入 OCR 相关依赖。

图片 data URL 或 `inlineData.data` 本身就是 base64 字符串，payload 可能很大，这是内联传图时的正常现象。业务日志不要直接打印完整 `payload`、`input_content`、`input_parts` 或 `current_attachments`；建议只记录字段摘要，例如文件名、MIME、大小、transport、data URL 前缀和长度：

```python
def summarize_attachment(item):
    data = item.get("data") or ""
    return {
        "display_name": item.get("display_name"),
        "mime_type": item.get("mime_type"),
        "transport": item.get("transport"),
        "file_uri": item.get("file_uri"),
        "size_bytes": item.get("size_bytes"),
        "has_inline_data": bool(data),
        "inline_data_length": len(data),
    }

logger.info(
    "ksadk_prepare_state attachments=%s has_current_files=%s",
    [summarize_attachment(item) for item in payload.get("current_attachments", [])],
    payload.get("has_current_files", False),
)
```

旧客户端图片示例（先上传，再引用）：

```json
{
  "input": [
    {
      "role": "user",
      "content": [
        {
          "text": "请分析这张图片"
        },
        {
          "fileData": {
            "fileUri": "ksadk-upload://abc123.png",
            "displayName": "diagram.png",
            "mimeType": "image/png"
          }
        }
      ]
    }
  ],
  "model": "glm-5.2",
  "stream": false
}
```

旧客户端图片示例（直接内联）：

```json
{
  "input": [
    {
      "role": "user",
      "content": [
        {
          "text": "请分析这张图片"
        },
        {
          "inlineData": {
            "data": "<base64-encoded-image-bytes>",
            "displayName": "diagram.png",
            "mimeType": "image/png"
          }
        }
      ]
    }
  ]
}
```

当前附件类型支持矩阵：

| 类型 | 典型扩展名 / MIME | 传输支持 | 平台提取支持 | 原生多模态直通 |
| --- | --- | --- | --- | --- |
| 文本 | `.txt` `.md` `.json` `.yaml` `.yml` `.csv` `.tsv` `.log` | 支持 | 支持 | 不适用 |
| 文档 | `.pdf` `.docx` `.pptx` `.xlsx` `.html` `.htm` | 支持 | 部分支持：文本提取 / OCR | 不适用 |
| 图片 | `.png` `.jpg` `.jpeg` `.webp` / `image/*` | 支持 | 元信息提取默认支持；OCR 需构建时显式启用 | 部分支持，见下方框架差异 |
| 压缩包 | `.zip` | 支持 | 支持：目录/可读文件抽样提取 | 不适用 |
| 其他二进制 | 其他后缀或 `application/octet-stream` | 支持 | 通常仅保留为附件引用 | 不支持 |

框架差异：

- `ADK`
  - 图片附件会优先以 bytes 形式构造成底层 SDK `Part`
  - 若底层模型支持原生多模态，可直接消费图片
- `LangGraph`
  - 简化输入路径下，若模型支持图片输入，图片附件会自动转换为多模态 `HumanMessage.content` blocks
  - 非图片附件仍保留为普通附件上下文
- `LangChain`
  - 当前没有对所有 agent 统一做“自动图片直通”
  - 如需原生多模态，建议在 `ksadk_prepare_input(payload, session_context)` 中优先消费 `input_content / input_messages`，必要时再兼容 `input_parts / current_attachments / attachments`
  - 判断当前轮是否传文件用 KsADK runner payload 扩展字段 `has_current_files`；该字段不是 OpenAI Responses API 官方字段

模型能力判断优先级：

1. 请求里显式传入的 `model_metadata`
2. runtime 通过 `OPENAI_BASE_URL` / `OPENAI_API_KEY` 查询上游 `/v1/models` 返回的 `architecture.input_modalities`
3. 本地默认兜底（按文本模型处理）

多轮会话历史：

- `/v1/responses` 本身不要求客户端每轮重传完整历史
- 新客户端应持续传同一个 `conversation`，runtime 会从服务端会话存储里恢复该会话的历史 transcript
- 旧客户端只传 `session_id` 时仍可恢复同一会话，但这是 ksadk legacy extension
- 进入 runner 前，`ksadk` 会把历史、附件上下文、知识库上下文和长期记忆上下文统一重建成标准运行输入
- `safety_identifier` 会作为内部 user id，并用于 Langfuse UserID；未传时 deprecated `user` 字段可作为兜底
- `previous_response_id` 按 OpenAI Responses 语义接收并保留；当使用 `conversation` 时不要同时传 `previous_response_id`

### Responses approval / interrupt 恢复

如果流式执行遇到工具审批或人工确认，runtime 不会把本轮包装成 completed，而是返回 incomplete：

- `status`: `incomplete`
- `incomplete_details.reason`: `approval_required`
- MCP/tool approval 场景会输出 `mcp_approval_request`
- 非 MCP 的通用 interrupt 会输出 `response.ksadk.approval_request`

#### MCP approval 恢复

MCP/tool approval 场景按 OpenAI Responses 标准语义恢复。客户端应传同一个 `conversation` 或 legacy `session_id`，并把 `input` 写成 `mcp_approval_response`：

```json
{
  "conversation": "conv_customer_001",
  "input": [
    {
      "type": "mcp_approval_response",
      "id": "mcprsp_123",
      "approval_request_id": "appr_123",
      "approve": true,
      "reason": "approved by user"
    }
  ],
  "stream": true
}
```

运行时处理方式：

- 记录一条 `approval_response` 会话事件
- 向 runner 传入 `resume=True`
- `input` 原样保留为 `mcp_approval_response`
- LangGraphRunner 在内部转换成 `Command(resume=...)`

调用方不需要、也不应该直接传 Python `Command`。

#### 通用 interrupt 恢复

如果 interrupt 不是 MCP/tool approval，而是普通人工确认、补充信息或业务分支选择，客户端可以使用平台扩展 `ksadk_resume`：

```json
{
  "conversation": "conv_customer_001",
  "input": [
    {
      "type": "ksadk_resume",
      "interrupt_id": "intr_123",
      "value": {
        "approved": true,
        "answer": "继续"
      }
    }
  ],
  "stream": true
}
```

这类事件属于 `ksadk` 扩展，不伪装成 OpenAI MCP approval。

### Agent 开发者如何在业务代码中拿到上下文

这部分不属于远程 API 调用 contract。不同框架的业务代码接入方式已经内化到框架专属文档：

- LangGraph: [LangGraph开发最佳实践](./frameworks/LangGraph开发最佳实践.md)
- 平台公共上下文总览: [Agent 开发者上下文接入指南](./Agent 开发者上下文接入指南.md)

调用方只需要理解：

- `/v1/responses` 不要求每轮重传完整历史
- 同一会话应持续传同一个 `conversation`；旧客户端传 `session_id` 也能继续兼容
- runtime 会在进入 runner 前重建历史、附件、知识库和长期记忆上下文
- 框架业务代码如何消费这些上下文，由对应框架最佳实践文档说明

### 历史压缩（compaction）是怎么做的

长会话不会无限把所有历史原样塞进模型。

当前策略是：

1. transcript 按 API round / `invocation_id` 分组
2. 保留最近若干轮原始消息
3. 把更早历史压成一条 `context_checkpoint`
4. 后续模型看到的是：
   - 一条 `Earlier conversation summary: ...`
   - 最近若干轮原始 user / assistant 消息

重要特性：

- 原始事件不会物理删除，compaction 是 append-only
- 工具调用、审批请求、附件引用等关键信息不会简单丢弃，会以 summary 或占位文本形式保留
- 压缩阈值会结合 `model_metadata` 的上下文窗口能力自动调整

非流式响应字段：

| 字段 | 说明 |
| --- | --- |
| `id` | response ID |
| `object` | 固定 `response` |
| `created_at` | Unix 时间戳 |
| `status` | 默认 `completed` |
| `model` | 模型名 |
| `output` | 输出条目数组 |
| `output_text` | 文本聚合结果 |
| `usage` | 简化 token 统计 |
| `session_id` | ksadk 返回的内部会话 ID；当请求传了 `conversation` 时与其 id 一致 |

非流式响应示例：

```json
{
  "id": "resp_123",
  "object": "response",
  "created_at": 1710000000,
  "status": "completed",
  "error": null,
  "incomplete_details": null,
  "instructions": null,
  "metadata": {},
  "model": "glm-5.2",
  "parallel_tool_calls": true,
  "temperature": null,
  "top_p": null,
  "tools": [],
  "output": [
    {
      "id": "msg_abc",
      "type": "message",
      "status": "completed",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "你好，我可以帮你分析代码。"
        }
      ]
    }
  ],
  "output_text": "你好，我可以帮你分析代码。",
  "usage": {
    "input_tokens": 0,
    "output_tokens": 12,
    "total_tokens": 12
  },
  "session_id": "conv_customer_001"
}
```

流式行为：

- `Content-Type: text/event-stream`
- 每个事件格式为：

```text
event: <event_name>
data: <json>

```

当前可能出现的主要事件：

- `response.created`
- `response.in_progress`
- `response.output_text.delta`
- `response.reasoning.delta`
- `response.tool_call`
- `response.tool_result`
- `response.output_item.added` / `response.output_item.done`：MCP approval request 等结构化 output item
- `response.ksadk.approval_request`：非 MCP 的通用 interrupt 扩展事件
- `response.compaction.start`
- `response.compaction.done`
- `response.incomplete`
- `response.completed`

## 6.3 OpenAI Chat Completions 兼容接口

### `POST /v1/chat/completions`

请求体字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `messages` | `array<object>` | 是 | OpenAI 风格消息数组 |
| `model` | `string` | 否 | 模型名 |
| `model_metadata` | `object` | 否 | 模型元数据 |
| `stream` | `boolean` | 否 | 是否流式 |
| `session_id` | `string` | 否 | 会话 ID |
| `temperature` | `number` | 否 | 当前代码接受，但不保证下游一定使用 |
| `max_tokens` | `integer` | 否 | 当前代码接受，但不保证下游一定使用 |

`messages[].content` 支持：

1. 字符串
2. OpenAI Chat content parts：`text` / `image_url`
3. KsADK 兼容扩展 part 数组：`text` / `inlineData` / `fileData`

OpenAI Chat 图片块示例：

```json
[
  {
    "role": "user",
    "content": [
      {
        "type": "text",
        "text": "请分析这张图片"
      },
      {
        "type": "image_url",
        "image_url": {
          "url": "data:image/png;base64,<base64-encoded-image-bytes>"
        }
      }
    ]
  }
]
```

KsADK 兼容扩展附件示例：

```json
[
  {
    "role": "user",
    "content": [
      {
        "text": "请分析附件"
      },
      {
        "fileData": {
          "fileUri": "ksadk-upload://abc123.txt",
          "displayName": "report.txt",
          "mimeType": "text/plain"
        }
      }
    ]
  }
]
```

非流式响应示例：

```json
{
  "id": "chatcmpl-123",
  "object": "chat.completion",
  "created": 1710000000,
  "model": "glm-5.2",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "这是分析结果。"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 6,
    "total_tokens": 6
  },
  "session_id": "sess-123"
}
```

内部转换规则：

- 字符串消息会转换为 runner `input_content: [{ "type": "input_text", ... }]`
- Chat 官方 `text` / `image_url` 会转换为 runner `input_text` / `input_image`
- `inlineData` / `fileData` 只作为 KsADK 兼容扩展处理，不声明为 OpenAI Chat 官方能力
- 响应对象仍保持 Chat Completions 语义，非流式 `object` 为 `chat.completion`

KsADK 扩展图片引用示例：

```json
[
  {
    "role": "user",
    "content": [
      {
        "text": "请分析这张图片"
      },
      {
        "fileData": {
          "fileUri": "ksadk-upload://abc123.png",
          "displayName": "diagram.png",
          "mimeType": "image/png"
        }
      }
    ]
  }
]
```

流式说明：

- 返回仍然是 SSE
- 事件名沿用 ksadk 统一事件，不是 OpenAI 官方 `chat.completion.chunk`
- 因此客户端若按 OpenAI 官方 chunk parser 逐字节兼容，需要先确认是否接受该事件形态

## 6.4 公网 Hosted UI Facade 说明

通过 `PublicEndpoint` 访问 `POST /agentengine/api/v1/*` 时，应以 `agentengine-server` 的 facade 为准，而不是以 runtime pod 本地 `ksadk.server.app` 的同名实现为准。

当前网关公开放行的 Hosted UI action 白名单包括：

- `GetAgentUiBootstrap`
- `CreateSession`
- `GetSession`
- `ListSessions`
- `DeleteSession`
- `ListSessionEvents`
- `SubscribeRunEvents`
- `GetResponseFeedback`
- `UpsertResponseFeedback`
- `DeleteResponseFeedback`
- `RunAgent`
- `ListSessionCheckpoints`
- `PreviewCheckpointResume`
- `ListToolReceipts`
- `ResumeRun`
- `CancelRun`
- `UploadFile`
- `ListWorkspaceFiles`
- `AddWorkspaceFile`
- `DeleteWorkspaceFile`
- `ListAgentModels`

另外两个 GET 下载路径也会通过 Hosted/UI 侧转发：

- `GET /agentengine/api/v1/AttachmentContent`
- `GET /agentengine/api/v1/GetWorkspaceFileContent`

本地 runtime 还提供 `ExportWorkspaceZip`、`/agentengine/api/v1/ws/{agent_id}/{file_path}` 等 UI 辅助接口。公网 `PublicEndpoint` 是否放行这些接口，以 `agentengine-gateway` 的 Hosted UI 白名单和独立 facade 实现为准；不要把任意 runtime 本地路由都当成公网稳定 contract。

长任务恢复相关 action 的公网链路是：

`agentengine-hosted-ui / ksadk-web -> agentengine-gateway 白名单 -> agentengine-server Hosted facade -> runtime/router -> runtime 本地同名 action`

因此，公网 contract 以 gateway 白名单和 `agentengine-server` facade 为准；runtime 本地实现是最终执行方，但不是浏览器直接依赖的入口。

能力门控以 `GetAgentUiBootstrap.Data.Capabilities.RunLifecycle` 为准。`RunLifecycle.Resume` 只表示普通运行生命周期可继续交互；checkpoint 恢复必须同时看到 `RunLifecycle.Checkpoints=true` 和 `RunLifecycle.CheckpointResume=true`。当前 `adk`、`langchain`、`langgraph`、`deepagents` 可声明 checkpoint lifecycle；`hermes` 虽然有 Hosted Chat、原生 dashboard 和 terminal，但其 Hermes runtime 壳只代理 `/v1/*` 与原生管理路由，不提供 `ListSessionCheckpoints` / `ResumeRun` / `CancelRun` 本地同名 action，因此不应默认点亮 checkpoint 恢复能力。

## 6.5 Hosted UI Bootstrap

### `POST /agentengine/api/v1/GetAgentUiBootstrap`

说明：

- 这是 hosted chat / hosted workbench 初始化时的核心 bootstrap 接口
- 对公网数据面，这个 action 会被网关显式放行

请求体字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 否 | 与 `Name` 二选一，优先使用 |
| `Name` | `string` | 否 | Agent 名称 |
| `SessionId` | `string` | 否 | 当前会话 ID |

响应外层统一包裹：

```json
{
  "Code": 0,
  "Message": "Success",
  "RequestId": "req-xxxxxxxxxxxx",
  "Action": "GetAgentUiBootstrap",
  "Data": { "...": "..." }
}
```

`Data` 关键字段：

| 字段 | 说明 |
| --- | --- |
| `Agent.AgentId` | Agent ID |
| `Agent.Name` | Agent 名 |
| `Agent.Framework` | framework 名 |
| `Modules` | 当前固定 `["Chat","Build","Deploy"]` |
| `Capabilities.Attachments` | 固定 `true` |
| `Capabilities.WorkspaceFiles` | 是否开启 workspace |
| `Capabilities.Approval` | 当前公网 Hosted facade 为 `false` |
| `Capabilities.Thinking` | 固定 `true` |
| `Capabilities.HostedRuntime` | 当前公网 Hosted facade 为 `true` |
| `Capabilities.SlashCommands` | 当前固定 `["/new","/clear","/stop","/help","/attach"]` |
| `WorkspaceFiles` | 工作区能力描述 |
| `AccessMode` | `Owner / Private / Share` |
| `SharePermissions.DefaultPath` | 默认 UI 路径；通常为 `/chat`，Hermes 管理页可为 `/` |
| `SharePermissions.SharePath` | 分享默认路径 |
| `ApiFormats` | `hermes` 为 `["chat_completions"]`，其余通常为 `["responses","chat_completions"]` |
| `Stream` | 当前固定 `true` |
| `SessionId` | 请求传入的会话 ID |
| `HostedRuntime` | runtime 摘要对象，可能为 `null` |
| `Model` | 当前模型摘要，可能为 `null` |

`WorkspaceFiles` 字段在启用时结构为：

```json
{
  "Enabled": true,
  "MaxUploadBytes": 104857600,
  "SupportsDelete": true,
  "RootLabel": "workspace",
  "EntryAction": "ListWorkspaceFiles",
  "UploadAction": "AddWorkspaceFile",
  "ContentPath": "/agentengine/api/v1/GetWorkspaceFileContent"
}
```

重要限制：

- share link 场景下，`WorkspaceFiles.Enabled` 会被关闭
- 当前服务端只对 `adk / langchain / langgraph / deepagents / hermes` 开启 workspace files

## 6.6 会话 Action 接口

### `POST /agentengine/api/v1/CreateSession`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 是 | Agent ID |
| `UserId` | `string` | 否 | 可选用户 ID |
| `SessionId` | `string` | 否 | 显式指定 session ID |
| `ExpiresHours` | `integer` | 否 | 兼容旧字段，当前忽略 |

### `POST /agentengine/api/v1/ListSessions`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 是 | Agent ID |
| `UserId` | `string` | 否 | 可选用户 ID |
| `Page` | `integer` | 否 | 默认 `1` |
| `PageSize` | `integer` | 否 | 默认 `20`，最大 `200` |

### `POST /agentengine/api/v1/GetSession`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `SessionId` | `string` | 条件 | 与 `Id` 二选一 |
| `Id` | `string` | 条件 | 兼容旧字段 |

### `POST /agentengine/api/v1/DeleteSession`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `SessionId` | `string` | 条件 | 与 `Id` 二选一 |
| `Id` | `string` | 条件 | 兼容旧字段 |

### `POST /agentengine/api/v1/ListSessionEvents`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `SessionId` | `string` | 是 | Session ID |
| `Offset` | `integer` | 否 | 起始偏移，`>= 0` |
| `Limit` | `integer` | 否 | 返回条数，`>= 1` |

会话响应中 `Session` 的主要字段：

| 字段 | 说明 |
| --- | --- |
| `SessionId` | 会话 ID |
| `AgentId` | Agent ID |
| `UserId` | 用户 ID |
| `Title` | 当前标题 |
| `TitleSource` | 标题来源 |
| `Summary` | 摘要 |
| `FirstPrompt` | 第一条 prompt |
| `LastPrompt` | 最近一条 prompt |
| `State` | 会话状态字典 |
| `CreatedAt` | 创建时间 |
| `UpdatedAt` | 更新时间 |
| `Version` | 版本号 |

事件响应中 `Events[]` 的主要字段：

| 字段 | 说明 |
| --- | --- |
| `EventId` | 事件 ID |
| `SessionId` | 会话 ID |
| `Author` | 作者 |
| `EventType` | 事件类型 |
| `Content` | 事件内容 |
| `Timestamp` | 时间戳 |
| `SeqId` | 序号 |
| `Metadata` | 元数据 |
| `InvocationId` | 可选，本轮运行 ID |

分页返回补充：

- `ListSessions` 的 `Data` 额外包含 `Total`
- `ListSessions` 的 `Data` 还会包含服务端回显的 `Page` 和 `PageSize`
- `ListSessionEvents` 的 `Data` 额外包含请求透传的 `Offset` 和 `Limit`
- `ListSessionEvents` 的 `Data` 还会包含 `Total`，便于客户端按需回加载更早的事件窗口

### `GET /agentengine/api/v1/SubscribeRunEvents`

说明：

- 这是 AgentEngine Hosted UI / 本地 Web UI 的运行生命周期扩展接口，用于刷新页面、SSE 断开或切换会话后，按同一个 `SessionId + InvocationId` 继续订阅已经持久化的运行事件
- 它不是 OpenAI Responses API 或 Chat Completions 官方接口，不改变 `/v1/responses`、`/v1/chat/completions` 的对外协议语义
- 订阅返回的是 SSE，事件内容与 `ListSessionEvents.Events[]` 的事件 payload 形态一致
- 当前本地 runtime 订阅窗口为 5 分钟；如果订阅期间看到 terminal `run_status`，服务端会发送 `data: [DONE]` 并结束流

查询参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `SessionId` | `string` | 是 | 会话 ID |
| `InvocationId` | `string` | 是 | 本轮运行 ID，通常来自已回放事件的 `InvocationId` |
| `AfterSeqId` | `integer` | 否 | 只推送 `SeqId > AfterSeqId` 且 `InvocationId` 匹配的事件，默认 `0` |

请求示例：

```http
GET /agentengine/api/v1/SubscribeRunEvents?SessionId=sess-123&InvocationId=inv-abc&AfterSeqId=12
Accept: text/event-stream
```

SSE 数据示例：

```text
data: {"EventId":"evt-13","SessionId":"sess-123","EventType":"assistant_delta","SeqId":13,"InvocationId":"inv-abc","Content":{"text":"继续输出"}}

data: {"EventId":"evt-14","SessionId":"sess-123","EventType":"run_status","SeqId":14,"InvocationId":"inv-abc","Content":{"status":"completed"}}

data: [DONE]
```

## 6.7 文件上传与附件内容

### `POST /agentengine/api/v1/UploadFile`

请求：

- `multipart/form-data`
- 表单字段：`file`

响应示例：

```json
{
  "Code": 0,
  "Message": "Success",
  "RequestId": "req-xxxx",
  "Action": "UploadFile",
  "Data": {
    "FileData": {
      "fileUri": "ksadk-upload://abc123.txt",
      "displayName": "report.txt",
      "mimeType": "text/plain",
      "sizeBytes": 1024
    }
  }
}
```

### `GET /agentengine/api/v1/AttachmentContent?FileUri=<uri>`

请求参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `FileUri` | 是 | `UploadFile` 返回的 `ksadk-upload://...` URI，或 Hosted/runtime 持久化的 `ae-upload://...` URI |

返回：

- 原始文件内容
- `Content-Type` 依据文件类型推断
- `Content-Disposition: inline`
- 当 `FileUri` 是 `ae-upload://...` 时，服务端会先解析 Hosted 上传元数据，再返回原始文件内容

## 6.8 Workspace Files Action 接口

这组接口是对 runtime 内部 `/_ksadk/workspace/v1/*` 的 action 包装。

重要限制：

- share link 场景下，这组接口会被拒绝，返回 `403`
- 这些接口会先根据 `AgentId` 或 `Name` 解析目标 Agent，再由 `agentengine-server` 代理到对应 runtime

### `POST /agentengine/api/v1/ListWorkspaceFiles`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 否 | 与 `Name` 二选一，优先用于解析 Agent |
| `Name` | `string` | 否 | 与 `AgentId` 二选一 |
| `Path` | `string` | 否 | 默认 `"."` |
| `Recursive` | `boolean` | 否 | 默认 `false` |

响应 `Data` 示例：

```json
{
  "Root": "workspace",
  "Path": ".",
  "Entries": [
    {
      "Name": "outputs",
      "Path": "outputs",
      "Type": "directory",
      "SizeBytes": null,
      "MimeType": null,
      "ModifiedAt": "2026-04-27T10:00:00Z"
    }
  ]
}
```

### `POST /agentengine/api/v1/AddWorkspaceFile`

请求：

- `multipart/form-data`
- 表单字段：
  - `file`
  - `Path`
  - `AgentId`（可选）
  - `Name`（可选）

成功响应 `Data` 示例：

```json
{
  "Entry": {
    "Name": "report.txt",
    "Path": "uploads/report.txt",
    "Type": "file",
    "SizeBytes": 1024,
    "MimeType": "text/plain",
    "ModifiedAt": "2026-04-27T10:00:00Z"
  }
}
```

### `POST /agentengine/api/v1/DeleteWorkspaceFile`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 否 | 与 `Name` 二选一 |
| `Name` | `string` | 否 | 与 `AgentId` 二选一 |
| `Path` | `string` | 是 | 待删除文件相对路径 |

响应：

```json
{
  "Deleted": true
}
```

### `GET /agentengine/api/v1/ExportWorkspaceZip?Path=<path>&AgentId=<id>`

说明：

- 这是本地 Web UI / Workspace 面板使用的目录导出辅助接口
- 它会读取指定 workspace 目录及其子文件，并返回 zip 文件
- share link 场景和公网数据面是否可用，以 Hosted UI facade / gateway 白名单为准

请求参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `Path` | 否 | 待导出的 workspace 相对目录，默认 `"."` |
| `AgentId` | 否 | 与 `Name` 二选一 |
| `Name` | 否 | 与 `AgentId` 二选一 |

返回：

- `application/zip`
- 文件名通常为 `workspace.zip`

### `GET /agentengine/api/v1/GetWorkspaceFileContent?FilePath=<path>&AgentId=<id>`

请求参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `FilePath` | 是 | 文件相对路径 |
| `AgentId` | 否 | 与 `Name` 二选一 |
| `Name` | 否 | 与 `AgentId` 二选一 |

返回：

- 原始文件内容
- 透传上游 runtime 的响应 Header（会过滤掉 `content-encoding` / `transfer-encoding` / `connection` / `content-length`）
- `Content-Type` 透传自 runtime

### `GET /agentengine/api/v1/ws/{agent_id}/{file_path}`

说明：

- 这是 Workspace HTML 预览和相对资源解析使用的本地辅助路径，不是 WebSocket
- HTML 文件会注入预览运行所需的 base href / CSP，便于页面内相对 CSS、JS、图片资源继续从 workspace 读取
- 它不建议作为业务 API 直接依赖；公网可用性以 Hosted UI facade / gateway 白名单为准

## 6.9 模型目录

### `POST /agentengine/api/v1/ListAgentModels`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 否 | 与 `Name` 二选一 |
| `Name` | `string` | 否 | 与 `AgentId` 二选一 |

响应 `Data` 结构：

```json
{
  "Models": [
    {
      "id": "glm-5.2",
      "display_name": "glm-5.2"
    }
  ],
  "Current": "glm-5.2",
  "Source": "OPENAI_MODEL_NAME"
}
```

说明：

- 服务端会优先尝试请求 runtime 侧模型目录 `GET <api_base>/v1/models`
- 若失败，则回退到当前 Agent 的模型配置推断结果

## 6.10 响应反馈 Action 接口

这组接口用于 hosted UI 或自研 WebUI 对某条 assistant 输出做通用点赞 / 点踩反馈。

重要边界：

- 当前正式 contract 是 Hosted Action，不是 runtime 原生 `POST /v1/responses/{response_id}/feedback`
- 调用地址为 `https://<PublicEndpoint>/agentengine/api/v1/<Action>`
- 主反馈事实源是平台的 `response_feedback` 表
- Langfuse score 是异步镜像链路，不能作为业务主存储或业务主键
- 客户端不需要也不应该持有 Langfuse key

### 如何绑定一次回复

自研 WebUI 调用 Agent 后，需要保存同一轮回复的两个字段：

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `SessionId` | `/v1/responses` 请求中传入的 `conversation` 或 legacy `session_id`，或响应中返回的 `session_id`；`RunAgent` 则使用 `SessionId` | 会话 ID。连续对话和反馈查询都应使用同一个值 |
| `ResponseId` | Responses payload 的 `id` | assistant 回复对应的 `resp_xxx` |

不同入口的取值方式：

- 直接调用 `/v1/responses`
  - 非流式：使用响应 JSON 顶层 `id` 和 `session_id`
  - 流式：从 `response.created` 或 `response.completed` 事件的 `data.id` 取 `ResponseId`；`SessionId` 使用请求里传入的 `conversation` 或 legacy `session_id`
- 调用 `RunAgent`
  - 建议 `ApiFormat=responses`
  - 非流式：外层是 `ActionResponse`，使用 `Data.id` / `Data.session_id`
  - 流式：解析 Responses 风格 SSE，使用事件里的 `data.id`；`SessionId` 使用请求里传入的 `SessionId`

只有已落库的 assistant message 才能反馈。服务端会校验：

- `SessionId` 属于当前账号和 `AgentId`
- `ResponseId` 能匹配该会话里的 assistant event metadata `response_id`
- 如传入 `EventId`，还会校验该 event 与 `ResponseId` 一致

### `POST /agentengine/api/v1/UpsertResponseFeedback`

创建或更新当前 response 的反馈。

请求体字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 是 | Agent ID |
| `SessionId` | `string` | 是 | 会话 ID |
| `ResponseId` | `string` | 是 | `/v1/responses` 的 response ID，通常为 `resp_xxx` |
| `Rating` | `string` | 是 | `up` 或 `down` |
| `Comment` | `string` | 否 | 文字反馈，点踩时建议填写 |
| `EventId` | `string` | 否 | 内部 assistant event ID；通常不用传 |
| `TraceId` | `string` | 否 | 可选 trace 覆盖值；通常不用传 |
| `RootSpanId` | `string` | 否 | 可选 root span 覆盖值；通常不用传 |

请求示例：

```bash
curl -X POST "https://<PublicEndpoint>/agentengine/api/v1/UpsertResponseFeedback" \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "AgentId": "ar-demo",
    "SessionId": "sess-123",
    "ResponseId": "resp_123",
    "Rating": "down",
    "Comment": "太啰嗦"
  }'
```

成功响应：

```json
{
  "Code": 0,
  "Message": "Success",
  "RequestId": "req-xxxx",
  "Action": "UpsertResponseFeedback",
  "Data": {
    "Feedback": {
      "AgentId": "ar-demo",
      "SessionId": "sess-123",
      "ResponseId": "resp_123",
      "EventId": "evt-123",
      "Rating": "down",
      "Comment": "太啰嗦",
      "TraceId": "79b770fc81ad583640721b288462f1bd",
      "RootSpanId": "",
      "CreatedAt": "2026-05-08T10:00:00Z",
      "UpdatedAt": "2026-05-08T10:00:00Z"
    }
  }
}
```

说明：

- 再次提交同一个 `AgentId + SessionId + ResponseId` 会覆盖原反馈
- 点赞可以不传 `Comment`
- 点踩建议传 `Comment`
- 如果该回复已有 trace metadata，服务端会 best-effort 写入 Langfuse `hosted_ui_feedback` score
- 如果 trace 还不可用，反馈仍会先落平台表；服务端日志会记录 score 镜像跳过或失败原因

### `POST /agentengine/api/v1/GetResponseFeedback`

查询某条 response 当前反馈。

请求体字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 是 | Agent ID |
| `SessionId` | `string` | 是 | 会话 ID |
| `ResponseId` | `string` | 是 | response ID |

请求示例：

```bash
curl -X POST "https://<PublicEndpoint>/agentengine/api/v1/GetResponseFeedback" \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "AgentId": "ar-demo",
    "SessionId": "sess-123",
    "ResponseId": "resp_123"
  }'
```

返回：

- `Data.Feedback` 为反馈对象
- 没有反馈时 `Data.Feedback` 为 `null`

### `POST /agentengine/api/v1/DeleteResponseFeedback`

删除某条 response 的反馈。

请求体字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 是 | Agent ID |
| `SessionId` | `string` | 是 | 会话 ID |
| `ResponseId` | `string` | 是 | response ID |

请求示例：

```bash
curl -X POST "https://<PublicEndpoint>/agentengine/api/v1/DeleteResponseFeedback" \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "AgentId": "ar-demo",
    "SessionId": "sess-123",
    "ResponseId": "resp_123"
  }'
```

成功响应：

```json
{
  "Code": 0,
  "Message": "Success",
  "RequestId": "req-xxxx",
  "Action": "DeleteResponseFeedback",
  "Data": {
    "Deleted": true
  }
}
```

### 自研 WebUI 推荐调用顺序

1. 创建或复用一个 `SessionId`
2. 调用 `/v1/responses`，或调用 `RunAgent` 且设置 `ApiFormat=responses`
3. 从本轮 assistant 回复拿到 `ResponseId`
4. 渲染点赞 / 点踩按钮
5. 页面刷新或历史回放时，对每条 assistant 回复调用 `GetResponseFeedback` 回显状态
6. 用户点赞或点踩时调用 `UpsertResponseFeedback`
7. 用户取消反馈时调用 `DeleteResponseFeedback`

## 6.11 Hosted 运行入口

### `POST /agentengine/api/v1/RunAgent`

说明：

- 这是 hosted UI 直接调用的运行入口
- 它内部会根据 `ApiFormat` 转到：
  - `responses`
  - `chat_completions`

请求体字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 是 | Agent ID |
| `Messages` | `array<object>` | 否 | 兼容旧 UI / 旧客户端的消息数组 |
| `ResponsesInput` | `string \| array<object>` | 否 | `ApiFormat=responses` 时优先使用的 OpenAI Responses 风格输入；Hosted UI 默认使用它 |
| `SessionId` | `string` | 否 | 会话 ID |
| `ApiFormat` | `string` | 否 | 默认 `responses`；可选 `responses` / `chat_completions` |
| `Stream` | `boolean` | 否 | 是否流式 |
| `Model` | `string` | 否 | 本次显式模型 |
| `ModelMetadata` | `object` | 否 | 模型元数据 |

请求示例：

```json
{
  "AgentId": "ar-demo",
  "SessionId": "sess-123",
  "ApiFormat": "responses",
  "Stream": true,
  "ResponsesInput": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "帮我总结今天的变更"
        }
      ]
    }
  ],
  "Messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "帮我总结今天的变更"
        }
      ]
    }
  ]
}
```

流式返回：

- `ApiFormat=responses` 时：Responses 风格 SSE
- `ApiFormat=chat_completions` 时：透传 runtime 的流式返回，实践中通常仍是 ksadk 统一 SSE 事件

## 6.12 长任务恢复与运行时取消 Action

这组接口用于 Hosted UI / 本地 Web UI 展示 checkpoint、预览恢复、恢复运行和取消运行。公网 `PublicEndpoint` 调用时，请求先经过 `agentengine-gateway` Hosted UI action 白名单，再由 `agentengine-server` 按 `AgentId` 解析目标 runtime 并代理到 runtime/router。前端是否展示入口必须依赖 bootstrap capability，不要仅凭 action 是否在白名单内判断可用性。

公网链路验收应使用 `scripts/validate_hosted_long_task_e2e.py`，而不是只跑本地 runtime / ASGI 脚本。该脚本不需要 PG DSN，只访问 `PublicEndpoint`：

```bash
python scripts/validate_hosted_long_task_e2e.py \
  --endpoint "https://<PublicEndpoint>" \
  --agent-id "<AgentId>" \
  --api-key "$AGENTENGINE_RUNTIME_API_KEY"
```

如果通过 private/share 短链接打开 Hosted UI，也可以传入 `--cookie "ae_ui_session=<sid>"`。脚本默认覆盖 bootstrap capability、`RunAgent`、`ListSessionCheckpoints`、`ResumeRun(Stream=true)` 和 `ListSessionEvents`；运行时取消可用 `--mode cancel-active --session-id <SessionId> --invocation-id <InvocationId>` 对仍活跃的流式 run 验证 `CancelRun`。

### `POST /agentengine/api/v1/ListSessionCheckpoints`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 是 | Agent ID |
| `SessionId` | `string` | 是 | 会话 ID |
| `RunId` | `string` | 否 | 只返回指定 run 的 checkpoint |

响应 `Data.Checkpoints` 为 checkpoint 列表。checkpoint 来自 runtime session event 中的 `run_checkpoint`，不是客户端传入的状态。

### `POST /agentengine/api/v1/PreviewCheckpointResume`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 是 | Agent ID |
| `SessionId` | `string` | 是 | 会话 ID |
| `RunId` | `string` | 是 | 原 run ID |
| `CheckpointId` | `string` | 是 | 要恢复的 checkpoint ID |

响应 `Data.Preview` 返回恢复预览信息，用于 UI 在真正恢复前展示将从哪个 checkpoint 继续、可能涉及哪些 tool receipt。

### `POST /agentengine/api/v1/ListToolReceipts`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 是 | Agent ID |
| `SessionId` | `string` | 是 | 会话 ID |
| `RunId` | `string` | 否 | 只返回指定 run 的 tool receipt |
| `CheckpointId` | `string` | 否 | 只返回指定 checkpoint 关联的 tool receipt |

响应 `Data.ToolReceipts` 为已记录的工具执行 receipt，用于恢复时展示和幂等治理。

### `POST /agentengine/api/v1/ResumeRun`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 是 | Agent ID |
| `SessionId` | `string` | 是 | 会话 ID |
| `RunId` | `string` | 是 | 原 run ID。恢复语义是同一 run 续跑，不是新建 run |
| `CheckpointId` | `string` | 是 | 要恢复的 checkpoint ID |
| `ResumeAttemptId` | `string` | 否 | 本次恢复尝试 ID；不传由 runtime 生成 |
| `InvocationId` | `string` | 否 | 本次流式恢复的 invocation ID；用于 `SubscribeRunEvents` / `CancelRun` |
| `Stream` | `boolean` | 否 | 是否流式返回 |
| `Model` | `string` | 否 | 可选模型名 |
| `ModelMetadata` | `object` | 否 | 可选模型 metadata |
| `ModelOptions` | `object` | 否 | 可选模型调用参数 |

`Stream=true` 时返回 SSE，gateway 和 server 都按流式代理处理。runtime 只信任服务端已保存的 checkpoint 事件来解析 `framework_ref`，不会信任客户端传入的 framework 状态。

### `POST /agentengine/api/v1/CancelRun`

说明：

- 这是 Hosted UI / 本地 Web UI 的运行取消接口
- 公网 `PublicEndpoint` 调用时由 gateway 放行到 `agentengine-server`，再代理到 runtime 本地同名 action
- runtime 会尝试调用当前 active runner 的 `request_cancel(InvocationId)`，并取消 detached streaming task
- 如果 runner 不支持真正取消，接口仍可能返回 `Cancelled=true`，语义是“已请求取消”；前端仍应以后续 `run_status` 或事件流终态为准

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `AgentId` | `string` | 是 | Agent ID |
| `InvocationId` | `string` | 是 | 需要取消的运行 ID |

响应示例：

```json
{
  "Code": 0,
  "Message": "Success",
  "RequestId": "req-xxxx",
  "Action": "CancelRun",
  "Data": {
    "Cancelled": true
  }
}
```

非流式返回：

- 外层仍是 `ActionResponse`
- `Data` 直接放 runtime 返回的 payload
- 服务端会补齐 `session_id`

## 6.13 Legacy ADK Web 兼容接口

### `POST /run_sse`

请求体模型来自 `ksadk/server/api_models.py`：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `appName` | `string` | 是 | app 名 |
| `userId` | `string` | 是 | 用户 ID |
| `sessionId` | `string` | 否 | 会话 ID |
| `newMessage` | `object` | 是 | 新消息 |
| `streaming` | `boolean` | 否 | 是否流式 |
| `invocationId` | `string` | 否 | 调用 ID |
| `stateDelta` | `object` | 否 | 状态增量 |
| `functionCallEventId` | `string` | 否 | 函数调用事件 ID |
| `model` | `string` | 否 | 模型 |

`newMessage` 结构：

```json
{
  "role": "user",
  "parts": [
    {
      "text": "hello"
    }
  ]
}
```

### `/apps/{app_name}/users/{user_id}/sessions*`

这组接口是 legacy session 兼容层，主要面向 ADK Web。

## 6.14 Runtime 本地前端壳路径

### `GET /chat`

- 在 SDK 本地 `agentengine web` 或 runtime 镜像内置静态文件场景下，返回统一 Agent UI 的 `index.html`
- 生产公网 `PublicEndpoint` 的 `/chat` 不再由 `agentengine-server` 或 runtime 本地静态文件承载；Ingress 会优先路由到独立 `agentengine-hosted-ui` Service
- 前端仍通过 `/agentengine/api/v1/*` 调用 `agentengine-server` 的 Hosted UI action 接口

### `GET /build`

- SDK 本地前端壳路径，返回同一前端壳

### `GET /deploy`

- SDK 本地前端壳路径，返回同一前端壳

### `GET /`

- 当静态资源存在时，挂载整个静态目录

说明：

- 这些路径只有在 runtime 镜像内静态资源已构建并同步时才可用
- 生产 hosted UI 的源码、镜像和发布节奏归属 `agentengine-hosted-ui` 独立仓库；`ksadk-python` 中的静态资源只作为 SDK 本地 UI 副本保留

## 7. Hermes 运行时详细接口

底层实现：`ksadk-python/deploy/hermes/runtime/app.py`

Hermes 不是直接把 `ksadk.server.app` 暴露出去，而是在容器内再包一层 wrapper：

- `/v1/*` 代理到内部 API server
- `/` 代理到内部 dashboard
- `/_ksadk/terminal/ws` 由 wrapper 自己实现
- workspace files 由 wrapper 直接挂载

## 7.1 路径总览

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `/` | GET 等 | Hermes dashboard 管理 UI |
| `/chat` | GET | AgentEngine hosted chat UI |
| `/health` | GET | wrapper 健康检查 |
| `/v1/{path}` | 全方法 | OpenAI-compatible API 透传 |
| `/_ksadk/workspace/v1/*` | GET/HEAD/POST/DELETE | workspace files |
| `/_ksadk/terminal/ws` | WebSocket | 终端 / connect / exec / pairing |

## 7.2 健康检查

### `GET /health`

响应示例：

```json
{
  "ok": true,
  "checks": {
    "api": {
      "name": "api",
      "ok": true,
      "status_code": 200,
      "url": "http://127.0.0.1:8642/health"
    },
    "dashboard": {
      "name": "dashboard",
      "ok": true,
      "status_code": 200,
      "url": "http://127.0.0.1:9119/"
    }
  }
}
```

## 7.3 `/v1/*`

Hermes 外层 wrapper 对外暴露整个 `/v1/{path}`，本质是透传到内部 `API_SERVER_PORT=8642`。

文档上应理解为：

- 至少提供 `/v1/chat/completions`
- 其余 `/v1/*` 只要内部 API server 存在，也会通过 wrapper 暴露

SSE 要点：

- wrapper 明确要求对 `/v1/*` 保持真流式转发
- 不应把上游流读取完后再一次性回包

## 7.4 Workspace Files

Hermes 直接复用通用的 `/_ksadk/workspace/v1/*` contract。

可用路径与通用 runtime 完全一致：

- `GET /_ksadk/workspace/v1/healthz`
- `GET /_ksadk/workspace/v1/entries`
- `HEAD /_ksadk/workspace/v1/files/{path}`
- `GET /_ksadk/workspace/v1/files/{path}`
- `POST /_ksadk/workspace/v1/files/{path}`
- `DELETE /_ksadk/workspace/v1/files/{path}`

## 7.5 终端 WebSocket

### `WS /_ksadk/terminal/ws`

连接要求：

- 必须带 `Sec-WebSocket-Protocol: ks-terminal.v1`
- 公网访问时应带 `Authorization: Bearer <api_key>`

建立连接后，客户端首帧必须是 JSON 文本：

```json
{
  "type": "start",
  "mode": "tui",
  "argv": [],
  "cwd": ".",
  "rows": 24,
  "cols": 80
}
```

`mode` 支持：

- `tui`
- `exec`
- `pairing`
- `connect`

其中：

- `tui` 会执行 `hermes chat`
- `exec` 走只读命令白名单
- `pairing` 走 `hermes pairing`
- `connect` 走 `hermes gateway setup`

服务端可能返回的文本消息：

```json
{"type":"ready"}
```

```json
{"type":"exit","code":0}
```

```json
{"type":"error","message":"..."}
```

控制帧示例：

```json
{"type":"resize","rows":40,"cols":120}
```

```json
{"type":"signal","signal":"SIGINT"}
```

```json
{"type":"stdin_eof"}
```

另外：

- PTY 输出主要通过 WebSocket binary frame 回传
- 如果首帧不是 `type=start`，服务端会报错

## 8. OpenClaw 运行时可确认接口

当前主线代码里，对 OpenClaw 可以准确写入文档的只有“平台补充 contract”，不要把上游 OpenClaw 原生全部接口误写成 ksadk/AgentEngine contract。

## 8.1 运行模式

OpenClaw gateway 主要有三种鉴权模式：

- `trusted-proxy`
- `token`
- `none`

默认建议模式：

- `trusted-proxy`

说明：

- 公网经 AgentEngine 网关访问时，主路径仍是 trusted-proxy 设计
- 自管或本地直连示例里，也支持 `token` 模式

## 8.2 健康检查

OpenClaw 运行镜像健康探针使用：

- `GET /healthz`

但这属于 OpenClaw gateway 原生健康接口，不是 ksadk 额外实现。

## 8.3 Workspace Files 平台补充接口

OpenClaw 会额外起一个本地 `workspace_files_app` sidecar，然后由 gateway 代理：

- `/_ksadk/workspace/v1/*`

可确认的外部 contract 与通用 runtime 一致：

- `GET /_ksadk/workspace/v1/healthz`
- `GET /_ksadk/workspace/v1/entries`
- `HEAD /_ksadk/workspace/v1/files/{path}`
- `GET /_ksadk/workspace/v1/files/{path}`
- `POST /_ksadk/workspace/v1/files/{path}`
- `DELETE /_ksadk/workspace/v1/files/{path}`

说明：

- sidecar 自身监听 `127.0.0.1:${WORKSPACE_FILES_PORT}`
- 公网访问时看到的是经 OpenClaw gateway 代理后的同一路径

## 9. 哪些接口能通过公网数据面直接访问

这个点很容易误判，这里单独说明。

### 9.1 一定可作为公网 contract 使用的接口

- `/v1/responses`
- `/v1/chat/completions`
- `/chat`
- `/_ksadk/workspace/v1/*`
- Hermes 的 `/_ksadk/terminal/ws`
- `GET /agentengine/api/v1/AttachmentContent`
- `GET /agentengine/api/v1/GetWorkspaceFileContent`
- `GET /agentengine/api/v1/ExportWorkspaceZip`
- Hosted UI action 白名单：
  - `GetAgentUiBootstrap`
  - `CreateSession`
  - `GetSession`
  - `ListSessions`
  - `DeleteSession`
  - `ListSessionEvents`
  - `SubscribeRunEvents`
  - `RunAgent`
  - `ListSessionCheckpoints`
  - `PreviewCheckpointResume`
  - `ListToolReceipts`
  - `ResumeRun`
  - `CancelRun`
  - `GetResponseFeedback`
  - `UpsertResponseFeedback`
  - `DeleteResponseFeedback`
  - `UploadFile`
  - `ListWorkspaceFiles`
  - `AddWorkspaceFile`
  - `DeleteWorkspaceFile`
  - `ListAgentModels`

### 9.2 不应假设公网可调用的接口

不要假设下列内容一定是公网 contract：

- 任意 `/agentengine/api/v1/*` 路径
- runtime 本地存在但未进入 Hosted UI action 白名单的 UI 辅助路径，例如 Workspace HTML 预览路径
- `/debug/*`、`/builder/*`、`/traces`、`eval_sets`、`eval_results` 等开发 / 调试 / 内部辅助入口
- 任意 Pod 内部监听端口
- OpenClaw 上游项目的全部原生 API
- Hermes dashboard 内部 `/api/*` 的所有未文档化子路径

## 10. 调用示例

## 10.1 通用 Agent：调用 `/v1/chat/completions`

```bash
curl -X POST "https://<PublicEndpoint>/v1/chat/completions" \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{
    "messages": [
      {
        "role": "user",
        "content": "你好"
      }
    ],
    "stream": false
  }'
```

## 10.2 Hosted UI：调用 `RunAgent`

```bash
curl -X POST "https://<PublicEndpoint>/agentengine/api/v1/RunAgent" \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "AgentId": "ar-demo",
    "SessionId": "sess-123",
    "ApiFormat": "responses",
    "Stream": true,
    "Messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "input_text",
            "text": "继续"
          }
        ]
      }
    ]
  }'
```

## 10.3 Workspace：列目录

```bash
curl "https://<PublicEndpoint>/_ksadk/workspace/v1/entries?path=.&recursive=false" \
  -H "Authorization: Bearer <api_key>"
```

## 10.4 Hermes：连接终端

```bash
wscat \
  -H "Authorization: Bearer <api_key>" \
  -s "ks-terminal.v1" \
  -c "wss://<PublicEndpoint>/_ksadk/terminal/ws"
```

首帧：

```json
{"type":"start","mode":"tui","rows":24,"cols":80}
```

## 11. 结论

当前 `master` 下可以稳定对外承诺的核心运行时 contract 是：

### 通用 Agent

- `/v1/responses`
- `/v1/chat/completions`
- 公网 `/chat` 入口由 `agentengine-hosted-ui` 承载；runtime 本地 `/chat` 只用于 SDK 本地 UI 或内置静态资源场景
- `/_ksadk/workspace/v1/*`
- Hosted UI action 白名单

### Hermes

- `/`
- 公网 `/chat` 入口由 `agentengine-hosted-ui` 承载
- `/v1/*`
- `/_ksadk/terminal/ws`
- `/_ksadk/workspace/v1/*`
- `/health`

### OpenClaw

- OpenClaw gateway 原生入口
- 平台额外挂出的 `/_ksadk/workspace/v1/*`
- 可配置的 `trusted-proxy | token | none` 鉴权模式

如果后续要继续扩展文档，建议按两个方向增量补充：

1. 基于真实镜像再验证 OpenClaw 原生 gateway 的稳定可见路由
2. 为 Hosted UI action 补充逐接口完整示例响应
