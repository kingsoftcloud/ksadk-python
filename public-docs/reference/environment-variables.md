# 环境变量参考

这页列出本地运行和发布流水线中最常用的 KsADK 环境变量。提交到仓库的文件只放
占位值，真实值放本地 `.env` 或 CI secrets。

## 模型 Provider

| 变量 | 用途 |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI 兼容模型 provider 的 API Key |
| `OPENAI_BASE_URL` | provider base URL，通常以 `/v1` 结尾 |
| `OPENAI_API_BASE` | `OPENAI_BASE_URL` 的兼容别名 |
| `OPENAI_MODEL_NAME` | 本地 runner 和 UI 使用的默认模型 |
| `MODEL_NAME` | 一些项目使用的兼容别名 |

示例：

```bash
OPENAI_API_KEY=sk-test
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL_NAME=my-model
```

## 项目与本地 UI

| 变量 | 用途 |
| --- | --- |
| `KSADK_PROJECT_DIR` | `agentengine web` 解析出的项目目录 |
| `AGENTENGINE_UI_DIR` | 本地 UI 状态目录，默认在 `.agentengine/ui` 下 |
| `KSYUN_REGION` | 云端 action 和部分 SDK client 使用的区域 |

## 会话存储

| 变量 | 用途 |
| --- | --- |
| `KSADK_SESSION_BACKEND` | session backend，例如 `local` 或 `postgres` |
| `AGENTENGINE_SESSION_BACKEND` | session backend 兼容别名 |
| `KSADK_SESSION_DSN` | PostgreSQL 或共享后端 DSN |
| `KSADK_SESSION_PATH` | 本地 session 数据库路径 |
| `KSADK_SESSION_NAMESPACE` | 共享 session 后端命名空间 |
| `KSADK_STM_BACKEND` | 短期记忆/session backend 兼容变量 |
| `KSADK_STM_PATH` | 本地 SQLite UI/session 状态路径 |
| `KSADK_STM_DB_PATH` | 本地 SQLite 路径兼容别名 |
| `KSADK_STM_URL` | 数据库型 session 状态 DSN 兼容变量 |
| `KSADK_STM_DB_URL` | 数据库型 session 状态 DSN 兼容变量 |
| `KSADK_ADK_SESSION_BACKEND` | ADK 原生 session backend 选择器 |
| `KSADK_ADK_SESSION_PATH` | ADK 原生 SQLite session 路径 |
| `KSADK_ADK_SESSION_URL` | ADK 原生数据库 session URL |
| `KSADK_TENANT_ID` | session 命名空间租户 id |
| `KSADK_WORKSPACE_ID` | session 命名空间 workspace id |
| `AGENTENGINE_TENANT_ID` | 租户 id 兼容变量 |
| `AGENTENGINE_WORKSPACE_ID` | workspace id 兼容变量 |

本地 UI 通常设置：

```bash
KSADK_STM_BACKEND=sqlite
KSADK_STM_PATH=.agentengine/ui/sessions.sqlite
```

## Ambient 运行时上下文

| 变量 | 用途 |
| --- | --- |
| `KSADK_KB_AMBIENT_ENABLED` | 启用运行时注入知识库上下文，默认启用 |
| `KSADK_KB_AMBIENT_POLICY` | `on_demand`、`always` 或 `disabled` |
| `KSADK_LTM_AMBIENT_ENABLED` | 启用运行时注入记忆上下文，默认启用 |
| `KSADK_LTM_AMBIENT_POLICY` | `on_demand`、`always` 或 `disabled` |
| `KSADK_LTM_AUTO_SAVE` | 支持时把完整对话轮次保存到长期记忆 |
| `KSADK_LTM_AGENT_ID` | 记忆条目关联的 agent id |

## Workspace 文件

| 变量 | 用途 |
| --- | --- |
| `KSADK_WORKSPACE_FILES_ENABLED` | 启用 workspace 文件路由 |
| `KSADK_WORKSPACE_ROOT_LABEL` | workspace 根目录显示名称 |
| `KSADK_WORKSPACE_MAX_UPLOAD_BYTES` | 单文件上传大小上限 |

## 长期记忆

| 变量 | 用途 |
| --- | --- |
| `KSADK_LTM_BACKEND` | 记忆 backend：`local`、`http` 或 `sdk` |
| `KSADK_LTM_TOP_K` | 默认检索记忆条数 |
| `KSADK_LTM_INDEX` | 本地或通用记忆索引名 |
| `KSADK_LTM_APP_NAME` | 记忆服务应用名 |
| `KSADK_LTM_HTTP_URL` | HTTP 记忆 backend 地址 |
| `KSADK_LTM_HTTP_TOKEN` | HTTP 记忆 backend token |
| `KSADK_LTM_ACCESS_KEY` | SDK 记忆 AK，可回退到云账号 AK 环境变量 |
| `KSADK_LTM_SECRET_KEY` | SDK 记忆 SK，可回退到云账号 SK 环境变量 |
| `KSADK_LTM_REGION` | SDK 记忆区域 |
| `KSADK_LTM_ENDPOINT` | SDK 记忆 endpoint |
| `KSADK_LTM_SCHEME` | SDK 记忆协议，通常为 `https` |
| `KSADK_LTM_NAMESPACE` | SDK 记忆 namespace |
| `KSADK_LTM_AGENT_ID` | 记忆条目关联的 agent id |
| `KSADK_LTM_SCENE_ID` | 场景 id，默认 `_sys_general` |
| `KSADK_LTM_AUTO_SAVE` | 支持时启用自动记忆保存 |
| `KSADK_MEMORY_BACKEND` | 旧版通用 memory backend 选择器 |
| `KSADK_MEMORY_URL` | 旧版通用 memory backend URL |
| `KSADK_MEMORY_PREFIX` | 旧版通用 memory key 前缀 |
| `KSADK_MEMORY_TTL` | 旧版通用 memory TTL，单位秒 |

## 知识库

| 变量 | 用途 |
| --- | --- |
| `KSADK_KB_DATASET_ID` | 启用知识库数据集集成 |
| `KSADK_KB_TOP_K` | 检索片段数量 |
| `KSADK_KB_ACCESS_KEY` | 可选知识库 SDK AK |
| `KSADK_KB_SECRET_KEY` | 可选知识库 SDK SK |
| `KSADK_KB_REGION` | 知识库服务区域 |
| `KSADK_KB_ENDPOINT` | 知识库服务 endpoint |
| `KSADK_KB_SEARCH_METHOD` | 检索方式，默认 `intelligence_search` |
| `KSADK_KB_SCORE_THRESHOLD` | 可选分数阈值 |
| `KSADK_KB_RERANKING_ENABLE` | 支持时启用 reranking |

## Skill Runtime

| 变量 | 用途 |
| --- | --- |
| `KSADK_SKILL_SPACE_IDS` | 逗号分隔的 Skill Space id |
| `SKILL_SPACE_ID` | 单个 Skill Space id 的兼容别名 |
| `KSADK_PUBLIC_SKILL_SPACE_IDS` | 追加的公共 Skill Space id |
| `KSADK_PUBLIC_SKILL_ALLOWLIST` | 允许暴露的公共 Skill 名称 |
| `KSADK_LOCAL_SKILLS_DIR` | 包含 `SKILL.md` 包的本地目录 |
| `KSADK_SELECTED_SKILL_NAMES` | 外层 Agent 选择的 Skill 名称 |
| `KSADK_SKILLS_MODE` | Skill 加载模式：`auto`、`local` 或 `sandbox` |
| `KSADK_SKILL_SERVICE_URL` | Skill Service endpoint |
| `KSADK_SKILL_SERVICE_TOKEN` | token 鉴权时的 Skill Service token |
| `KSADK_SKILL_SERVICE_ACCESS_KEY` | 签名请求使用的 Skill Service AK |
| `KSADK_SKILL_SERVICE_SECRET_KEY` | 签名请求使用的 Skill Service SK |
| `KSADK_SKILL_SERVICE_ACCOUNT_ID` | 签名请求账号 id |
| `KSADK_SKILL_SERVICE_REGION` | Skill Service 区域 |
| `KSADK_SKILL_SERVICE_API_VERSION` | Skill Service API 版本 |
| `KSADK_SKILL_SERVICE_SIGN_SERVICE` | 签名 service 名称 |
| `KSADK_SKILL_CACHE_DIR` | 已下载 skill 包本地缓存目录 |
| `KSADK_SKILL_ALLOW_HASH_MISMATCH` | 允许预览 hash 不匹配的 skill 包 |
| `KSADK_SKILL_MANIFEST_TIMEOUT` | 远程 Skill manifest 列表超时时间，单位秒 |
| `KSADK_SKILL_MANIFEST_LIMIT` | 注入到 Agent 指令中的 manifest 数量上限 |
| `KSADK_SKILL_RUNTIME_BACKEND` | 隔离执行 backend，例如 `local_process` 或 `e2b` |
| `KSADK_SKILL_RUNTIME_TEMPLATE_ID` | runtime template id |
| `KSADK_SANDBOX_BACKEND` | sandbox backend 选择器兼容变量 |
| `KSADK_SANDBOX_TEMPLATE_ID` | sandbox template id，也会启用 E2B 风格 backend |
| `KSADK_SKILL_RUNTIME_TIMEOUT` | 隔离执行超时时间，单位秒 |
| `KSADK_SANDBOX_TIMEOUT` | sandbox timeout 兼容变量 |
| `KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS` | sandbox 执行是否允许访问互联网 |
| `KSADK_SKILL_RUNTIME_AGENT_PATH` | local process runtime 使用的 agent 路径 |
| `KSADK_SKILL_WORKDIR` | 暴露给 skill 执行的工作目录 |
| `KSADK_SKILL_ARTIFACT_PROJECT` | 产物项目名称 |

## Tool Gateway

| 变量 | 用途 |
| --- | --- |
| `KSADK_TOOL_APPROVAL_MODE` | 工具审批模式；设置为 `strict` 时 medium / high / critical 风险工具先返回 `approval_required` |

## MCP

| 变量 | 用途 |
| --- | --- |
| `KSADK_ENABLE_MCP_TOOLS` | 启用或禁用 MCP tools |
| `KSADK_MCP_SERVERS` | MCP server 定义 JSON 数组 |
| `KSADK_BUILD_ENABLE_MCP` | code build 时包含 MCP runtime 依赖 |

示例：

```bash
KSADK_ENABLE_MCP_TOOLS=1
KSADK_MCP_SERVERS='[{"name":"docs","url":"http://127.0.0.1:9000/mcp"}]'
```

## 构建与打包

| 变量 | 用途 |
| --- | --- |
| `KSADK_BUILD_PIP_INSTALL_TIMEOUT_SECONDS` | build 流程中 pip install 超时时间 |
| `KSADK_BUILD_ENABLE_ATTACHMENT_OCR` | 包含 OCR 附件依赖 |
| `KSADK_BUILD_ENABLE_POSTGRES_SESSION` | 包含 PostgreSQL session 依赖 |

## 可观测

| 变量 | 用途 |
| --- | --- |
| `OTEL_SERVICE_NAME` | OpenTelemetry service name，也可作为 agent name fallback |
| `OTEL_RESOURCE_ATTRIBUTES` | OpenTelemetry resource attributes，逗号分隔的 `key=value` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 通用 OTLP HTTP endpoint；未设置 traces endpoint 时会派生 `/v1/traces` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | 通用 OTLP protocol；KsADK 自动 HTTP exporter 支持 `http/protobuf` |
| `OTEL_EXPORTER_OTLP_HEADERS` | 通用 OTLP HTTP headers，逗号分隔且 value URL encoded，可能包含鉴权信息 |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | traces 专用 OTLP HTTP endpoint，优先于通用 endpoint |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | traces 专用 OTLP protocol，优先于通用 protocol |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | traces 专用 OTLP HTTP headers，优先于通用 headers，可能包含鉴权信息 |
| `LANGFUSE_PUBLIC_KEY` | 兼容旧 Langfuse tracing 自动配置 |
| `LANGFUSE_SECRET_KEY` | Langfuse secret |
| `LANGFUSE_BASE_URL` | Langfuse base URL |
| `LANGFUSE_HOST` | Langfuse host 兼容别名 |
| `LANGFUSE_USE_CALLBACK` | 使用框架 callback 模式，而不是直接 OTLP 路径 |
| `SESSION_TITLE_MODEL` | 本地会话标题生成模型覆盖 |
| `COMPACTION_DISABLE_SEMANTIC` | 禁用语义摘要压缩 |
| `COMPACTION_SUMMARY_TIMEOUT_MS` | 摘要生成超时时间 |
| `COMPACTION_SUMMARY_MAX_GROUPS` | 摘要消息分组上限 |
| `COMPACTION_SUMMARY_MODEL` | 语义摘要模型覆盖 |

## 安全规则

- 不提交真实 key、token、DSN、私有 endpoint、客户数据集 id 或 kubeconfig。
- 开发环境用本地 `.env`，自动化用 CI secrets。
- 公开文档只使用 `sk-test`、`my-model`、`https://api.example.com/v1` 这类占位值。
