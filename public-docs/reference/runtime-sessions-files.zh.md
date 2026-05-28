# 会话与文件

KsADK 的本地运行时把对话历史、上传文件和 workspace 产物统一放在 session
边界内。业务 Agent 不应直接依赖某个宿主机绝对路径，而应通过 session id、
workspace 路由和运行时 payload 获取上下文。

## Session 标识

| 字段 | 含义 |
| --- | --- |
| `session_id` | 本地 UI、OpenAI 兼容 API 和运行时内部共享的会话标识 |
| `user_id` | 用户维度标识，用于隔离记忆、历史和审计 |
| `conversation` | Responses API 中的会话连续性对象 |
| `previous_response_id` | 客户端续聊时可使用的上一轮响应 id |

不要在业务代码里每轮生成新的 session id；这会导致 UI 历史、附件引用和运行时状态断裂。

## 本地存储

`agentengine web .` 默认使用项目目录下的 `.agentengine/ui/sessions.sqlite`。
相关变量见 [环境变量](environment-variables.zh.md)。

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

## 设计原则

- 会话连续性交给 KsADK runtime，不放进全局变量。
- 业务状态放在框架 state，例如 LangGraph state。
- 大文件和二进制产物通过 workspace 或 UI 文件面板处理。
- 共享后端要配置 namespace，避免不同 Agent 或环境互相污染。
