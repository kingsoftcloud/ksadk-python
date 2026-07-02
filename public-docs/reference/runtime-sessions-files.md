# 会话与文件

KsADK 的本地运行时把对话历史、上传文件和 workspace 产物统一放在 session
边界内。业务 Agent 不应直接依赖某个宿主机绝对路径，而应通过 session id、
workspace 路由和运行时 payload 获取上下文。

## Session 标识

| 字段 | 含义 |
| --- | --- |
| `session_id` | 本地 UI、OpenAI 兼容 API 和运行时内部共享的会话标识 |
| `user_id` | 用户维度标识，用于隔离记忆、历史和审计 |
| `account_id` | 云账号维度标识，hosted 场景下随请求透传，用于跨账号边界隔离 |
| `conversation` | Responses API 中的会话连续性对象 |
| `previous_response_id` | 客户端续聊时可使用的上一轮响应 id |

`account_id` 在 hosted 链路里由 gateway 注入，业务 Agent 不应自行伪造；
本地运行时按 owner 隔离，不会跨 `account_id` 共享 session、附件或 workspace。

不要在业务代码里每轮生成新的 session id；这会导致 UI 历史、附件引用和运行时状态断裂。

## 本地存储

`agentengine web .` 默认使用项目目录下的 `.agentengine/ui/sessions.sqlite`。
相关变量见 [环境变量](environment-variables.md)。

```bash
KSADK_STM_BACKEND=sqlite
KSADK_STM_PATH=.agentengine/ui/sessions.sqlite
```

共享环境可改用 PostgreSQL：

```bash
KSADK_SESSION_BACKEND=postgres
KSADK_SESSION_DSN=postgresql://user:pass@example.invalid:5432/ksadk
```

公开文档只使用占位 DSN，不提交真实连接串。

## 文件上传

本地 UI 上传文件后，运行时会把文件引用归一化到当前 turn 的输入中。业务 Agent
应读取标准化消息、附件 metadata 或框架 runner payload，而不是猜测浏览器上传目录。

常见输入类型：

- 文本消息。
- 图片输入，例如 `input_image`。
- 文件输入，例如 `input_file`。
- 历史 turn 中仍有效的附件引用。

### 上传 URI scheme

!!! new "0.6.7 新增"
    本地与 hosted 上传统一为 attachment URI scheme，由同一读取入口解析。

上传文件后，运行时把文件引用归一化为 attachment URI：

| Scheme | 来源 | 含义 |
| --- | --- | --- |
| `ksadk-upload://{file_id}` | 本地 `agentengine web` 上传 | 由本地 `AttachmentStorageService` 写入，绑定当前 server |
| `ae-upload://{file_id}` | hosted 链路上传 | 托管平台返回的 attachment 引用，需要服务端解析 |

两者都通过同一入口读取：

```bash
curl -sS "https://<public-endpoint>/agentengine/api/v1/AttachmentContent?FileUri=ksadk-upload://<file_id>" \
  -H "Authorization: Bearer <api_key>"
```

读取 `ae-upload://` 时，服务端会按 `{file_id}` 定位托管上传内容并返回字节流，
同时把内容回写到本地 cache（`local_path`），后续读取命中本地缓存。
非 `ae-upload://` 前缀的路径按 workspace 相对路径处理。

`AttachmentContent` 返回的 attachment 在运行时内部表示为 `AttachmentBytes`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `data` | bytes | 附件原始字节流 |
| `display_name` | str | 展示用文件名，已做 sanitize |
| `mime_type` | str | MIME 类型，缺失时按文件名推断 |
| `local_path` | Path 或 `None` | 本地 cache 绝对路径；hosted attachment 首次读取后回写，命中后为非空 |

公开示例不要把 `ksadk-upload://` 或 `ae-upload://` URI 当作持久外部 URL 写进源码或文档。

## Workspace

Workspace 是 Agent 生成产物的推荐位置，例如 HTML、Markdown、JSON、CSV 或代码文件。
本地 UI 和 hosted UI 可以围绕同一逻辑 workspace 展示、预览和下载。

```python
from ksadk.sessions.local_service import resolve_local_session_dir

workspace = resolve_local_session_dir() / "workspace"
workspace.mkdir(parents=True, exist_ok=True)
(workspace / "report.md").write_text("# Report\n", encoding="utf-8")
```

路径必须留在 workspace 根目录内，避免写入任意宿主机文件系统。

## Checkpoint 与 Resume

!!! new "0.6.7 新增"
    KsADK 引入框架级 checkpoint/resume 能力，覆盖 list、preview、resume、cancel 四个动作。

运行时通过四个 hosted UI action 管理 run 级 checkpoint 与恢复，路径见
[远程运行时 API](remote-runtime-api.md)：

| Action | 路径 | 用途 |
| --- | --- | --- |
| `ListSessionCheckpoints` | `POST /agentengine/api/v1/ListSessionCheckpoints` | 列出某 session 下的 checkpoint，供 resume 选择 |
| `GetCheckpointResumePreview` | `POST /agentengine/api/v1/GetCheckpointResumePreview` | 预览从某 checkpoint resume 的内容与影响面 |
| `ResumeRun` | `POST /agentengine/api/v1/ResumeRun` | 从 checkpoint 恢复 run 执行 |
| `CancelRun` | `POST /agentengine/api/v1/CancelRun` | 取消正在进行的 run |

### ResumeMode

`GetAgentUiBootstrap` 返回的 `RuntimeCapabilities.ResumeRun.ResumeMode` 描述框架级
resume 能力，取值与框架适配相关：

| ResumeMode | 含义 |
| --- | --- |
| `time_travel` | 可回档到历史 checkpoint（LangGraph runner） |
| `forward_only` | 只能沿 invocation 连续性向前续跑，不能回档（ADK runner） |
| `none` | 该框架无框架级 resume 能力 |

### RunLifecycle 能力门控

前端 resume 入口不能只看 `ResumeMode`，必须同时满足 `GetAgentUiBootstrap` 返回的
`RunLifecycle` 两个标志：

- `RunLifecycle.Checkpoints = true`：session 存在可用 checkpoint。
- `RunLifecycle.CheckpointResume = true`：运行时支持从 checkpoint resume。

任一为 `false` 时，应视为不支持 resume，前端不应展示 `ResumeRun` 入口。
`CheckpointResumePreview` 标注是否提供 preview 能力。

### SubscribeRunEvents 续订

`SubscribeRunEvents` 是 hosted UI 的重连入口，基于持久化事件而非原 TCP 流：

```bash
curl -sS -N "https://<public-endpoint>/agentengine/api/v1/SubscribeRunEvents?SessionId=<sid>&InvocationId=<iid>&AfterSeqId=42" \
  -H "Authorization: Bearer <api_key>" \
  -H "Accept: text/event-stream"
```

| 字段 | 含义 |
| --- | --- |
| `SessionId` | 目标 session id |
| `InvocationId` | 目标 invocation id |
| `AfterSeqId` | 续订起点 seq_id，返回 `seq_id > AfterSeqId` 的事件；默认 `0` 表示从头订阅 |

!!! warning "5 分钟服务端保护超时"
    `SubscribeRunEvents` 服务端保护超时为 **5 分钟**（`5 * 60` 秒）。超时后服务端主动关闭
    SSE 流，客户端必须用最后消费的 `seq_id` 作为新的 `AfterSeqId` 重新发起续订，直到
    观察到终态 `run_status`（`completed`、`failed`、`cancelled` 或 `interrupted`）。

## 会话列表与事件分页

Hosted UI 提供会话与事件列表 action，支持分页。公开 API 客户端通常不直接对接，
优先使用 OpenAI 兼容的 `/v1/*`。

### ListSessions

请求字段：

| 字段 | 默认 | 约束 | 含义 |
| --- | --- | --- | --- |
| `AgentId` | — | 必填 | 目标 Agent id |
| `UserId` | `user` | 可选 | 用户维度隔离 |
| `Page` | `1` | `>=1` | 页码，从 1 开始 |
| `PageSize` | `20` | `1..200` | 每页条数，上限 200 |

响应 `Data` 字段：

| 字段 | 含义 |
| --- | --- |
| `Sessions` | 当前页 session 列表 |
| `Total` | 命中条件 session 总数 |
| `Page` | 当前页码 |
| `PageSize` | 当前每页条数 |

```bash
curl -sS -X POST https://<public-endpoint>/agentengine/api/v1/ListSessions \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{"AgentId":"my-agent","UserId":"user","Page":1,"PageSize":20}'
```

### ListSessionEvents

请求字段：

| 字段 | 默认 | 约束 | 含义 |
| --- | --- | --- | --- |
| `SessionId` | — | 必填 | 目标 session id |
| `Offset` | `0` | `>=0` | 事件偏移量 |
| `Limit` | 不限制 | `>=1` | 返回条数上限；缺省时返回全部事件 |

响应 `Data` 字段：

| 字段 | 含义 |
| --- | --- |
| `Events` | 当前页事件列表 |
| `Total` | session 下事件总数 |
| `Offset` | 当前偏移量 |
| `Limit` | 实际生效的 limit |

```bash
curl -sS -X POST https://<public-endpoint>/agentengine/api/v1/ListSessionEvents \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{"SessionId":"<sid>","Offset":0,"Limit":50}'
```

## 设计原则

- 会话连续性交给 KsADK runtime，不放进全局变量。
- 业务状态放在框架 state，例如 LangGraph state。
- 大文件和二进制产物通过 workspace 或 UI 文件面板处理。
- 共享后端要配置 namespace，避免不同 Agent 或环境互相污染。
