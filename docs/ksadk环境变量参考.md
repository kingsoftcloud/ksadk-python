# KSADK 环境变量参考

本文档面向部署、运行、运维和 SDK 集成排障。它不是业务代码 `.env` 模板；业务方自己的变量，例如 `APP_ENV`、`DB_URL`、`CUSTOM_API_KEY`，只要不是 KsADK / 平台运行时读取的变量，都属于业务自定义变量，不在本文逐项维护。

本文档基于当前 `feat/skill-runtime` 工作树和 `master` 分支源码扫描整理，覆盖 `ksadk/`、`deploy/`、`tests/` 中已经注册或常见可配置的运行时变量。测试专用变量、PID/marker/cache 等进程内部临时变量、镜像构建脚本内部常量不会逐项列入表格；如果要排查这些高级项，以对应脚本源码和模板 README 为准。

## 1. 阅读规则

| 字段 | 含义 |
| --- | --- |
| 变量 | 环境变量名。 |
| 作用层级 | 主要读取方：CLI、本地运行时、云端 Runtime、Runner、Sandbox、Skill Runtime、平台服务等。 |
| 是否必传 | `是` 表示该场景启用时必须设置；`条件必传` 表示只有选择某个 backend/能力时才必传；`否` 表示有默认值或可不启用。 |
| 默认值 | 未设置时的 SDK 行为。`未设置` 表示没有默认值。`代码常量` 表示源码内部表名/依赖列表，不建议用户改。 |
| 别名/兼容 | 可替代变量、旧变量或 fallback 链路。优先使用表中第一列变量。 |
| 敏感 | 是否包含 token、secret、DSN、API key。敏感变量只能通过本地 shell、CI Secret、K8S Secret 或平台 Secret 注入。 |
| 配置方/来源 | 一般由谁提供或注入。 |
| 是否业务自定义 | `否` 表示 KsADK/平台读取；`是` 表示业务方可自由定义，本文只说明边界。 |
| 说明 | 用途、取值、注意事项。 |

## 2. 常见场景必传清单

### 2.1 本地运行普通 Agent

| 变量 | 是否必传 | 别名/兼容 | 敏感 | 配置方/来源 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `OPENAI_API_KEY` | 条件必传 | 部分 OpenAI 兼容实现也支持 `MODEL_API_KEY` | 是 | 开发者 / 模型网关 Secret | 使用 OpenAI 兼容模型时需要。 |
| `OPENAI_BASE_URL` | 条件必传 | `OPENAI_API_BASE` | 否 | 开发者 / 模型网关 | OpenAI 兼容接口 base url。 |
| `OPENAI_MODEL_NAME` | 条件必传 | `MODEL_NAME` | 否 | 开发者 | 默认模型名。 |
| `KSYUN_REGION` | 否 | 无 | 否 | 开发者 / 平台 | 本地 CLI 默认 `cn-beijing-6`。跨环境建议显式设置。 |

### 2.2 CLI 构建、发布、部署到金山云

| 变量 | 是否必传 | 别名/兼容 | 敏感 | 配置方/来源 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `KSYUN_ACCESS_KEY` | 是 | `KS3_ACCESS_KEY` | 是 | 开发者 / CI Secret | 金山云 API / KS3 / KOP 签名 AK。 |
| `KSYUN_SECRET_KEY` | 是 | `KS3_SECRET_KEY` | 是 | 开发者 / CI Secret | 金山云 API / KS3 / KOP 签名 SK。 |
| `KSYUN_ACCOUNT_ID` | 条件必传 | 无 | 否 | 开发者 / 平台账号 | 创建/查询/删除资源、权限预检查、个人版 KCR 用户名兜底等场景需要。 |
| `KSYUN_REGION` | 否 | 无 | 否 | 开发者 / 平台 | 默认 `cn-beijing-6`。 |
| `AGENTENGINE_SERVER_URL` | 否 | 无 | 否 | 平台 / 开发者 | 覆盖 AgentEngine Server 地址。内部账号/内网环境建议 `http://aicp.inner.api.ksyun.com`；公网账号通常不设置或使用 `https://aicp.api.ksyun.com`。 |
| `AGENTENGINE_API_VERSION` | 否 | 无 | 否 | 平台 / 开发者 | 覆盖 KOP API version。 |
| `AGENTENGINE_SIGN_SERVICE` | 否 | 无 | 否 | 平台 / 开发者 | 覆盖 KOP signing service。 |
| `KSADK_AICP_ENDPOINT_MODE` | 否 | 无 | 否 | 平台 / 开发者 | AICP endpoint 选择策略，支持 `auto/detect/internal/inner/public`。内网环境可显式设为 `inner`，跳过自动探测。 |

### 2.3 ADK Runner 注入远端 MCP tools

| 变量 | 是否必传 | 别名/兼容 | 敏感 | 配置方/来源 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `KSADK_ENABLE_MCP_TOOLS` | 否 | 无 | 否 | 开发者 / 平台 | 默认 `1`，设为 `0/false/no/off` 禁用自动注入。 |
| `KSADK_MCP_SERVERS` | 条件必传 | 无 | 是 | 开发者 / 平台 Secret | JSON 数组，配置 MCP server url、api_key、tool_filter、tool_name_prefix。可能包含 token。 |

### 2.4 Skill Runtime 本地模式

| 变量 | 是否必传 | 别名/兼容 | 敏感 | 配置方/来源 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `KSADK_SKILLS_MODE` | 否 | 无 | 否 | 开发者 / Runner 环境 | `auto/local/sandbox`。本地调试可显式设为 `local`。 |
| `KSADK_LOCAL_SKILLS_DIR` | 条件必传 | `KSADK_SKILL_CACHE_DIR` 可作为 fallback | 否 | 开发者 | 本地已解压 Skill 包目录；目录下每个 skill 应包含 `SKILL.md`。 |
| `KSADK_SKILL_RUNTIME_BACKEND` | 否 | 无 | 否 | 开发者 | 本地进程模式设为 `local_process`。 |
| `KSADK_SKILL_RUNTIME_AGENT_PATH` | 条件必传 | 默认使用 SDK 内置 agent | 否 | 开发者 | `local_process` backend 的 agent 入口。 |

### 2.5 Skill Runtime 远程 Sandbox / E2B 模式

| 变量 | 是否必传 | 别名/兼容 | 敏感 | 配置方/来源 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `KSADK_SANDBOX_TEMPLATE_ID` | 是 | `KSADK_SKILL_RUNTIME_TEMPLATE_ID` | 否 | 沙箱控制台 / 沙箱团队 | 新部署优先使用。AIO template 是 Skill Runtime 默认推荐。 |
| `E2B_API_URL` | 是 | 无 | 否 | 沙箱团队 / Secret 配置 | E2B 兼容 manager endpoint。 |
| `E2B_API_KEY` | 是 | 无 | 是 | 沙箱团队 / Secret 配置 | E2B SDK 原生 API key，不能写入代码、文档示例明文、测试 fixture 或日志。 |
| `KSADK_SANDBOX_BACKEND` | 否 | 无 | 否 | 平台 / 开发者 | 默认 `e2b`。后续可扩展其他 backend。 |
| `KSADK_SANDBOX_TYPE` | 否 | 无 | 否 | 平台 / 开发者 | `aio/code/browser/private`，默认 `aio`。 |
| `KSADK_SANDBOX_TIMEOUT` | 否 | `KSADK_SKILL_RUNTIME_TIMEOUT` | 否 | 平台 / 开发者 | Sandbox 会话超时秒数，默认 `900`。 |
| `KSADK_SANDBOX_ALLOW_INTERNET_ACCESS` | 否 | `KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS` | 否 | 平台 / 开发者 | 是否允许 sandbox 出网，默认 `true`。 |
| `KSADK_SKILLS_MODE` | 否 | 无 | 否 | Runner 环境 | `auto` 下检测到 sandbox backend/template 会注入 `execute_skills`；也可显式设为 `sandbox`。 |
| `KSADK_SKILL_RUNTIME_BACKEND` | 否 | 无 | 否 | Runner 环境 | 显式设为 `e2b` 会走远程 backend；显式 `disabled` 会禁止 Skill Runtime 注入。未设置且存在 `KSADK_SANDBOX_TEMPLATE_ID` 时自动使用 `e2b`。 |

### 2.6 Skill Center / Skill Service

| 变量 | 是否必传 | 别名/兼容 | 敏感 | 配置方/来源 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `KSADK_SKILL_SERVICE_URL` | 条件必传 | 无 | 否 | 平台 / Skill Service | 配置后 Runtime agent 才会从 Skill Center 拉取 skill。直连 REST 可用 `/agentengine/skill/api/v1`，AICP KOP 可用 `http://aicp.inner.api.ksyun.com`。 |
| `KSADK_SKILL_SERVICE_ENDPOINT` | 否 | 无 | 否 | 平台 / Skill Service | 未设置 `KSADK_SKILL_SERVICE_URL` 时的 AICP endpoint 覆盖，只写 host/path，不含 scheme。 |
| `KSADK_SKILL_SERVICE_SCHEME` | 否 | 无 | 否 | 平台 / Skill Service | 未设置 `KSADK_SKILL_SERVICE_URL` 时的 AICP URL scheme 覆盖；内网 endpoint 默认会使用 `http`。 |
| `KSADK_SKILL_SPACE_IDS` | 条件必传 | `SKILL_SPACE_ID` | 否 | Agent 创建/更新时注入 / Runner 环境 | 逗号分隔 space id；单 space 兼容变量为 `SKILL_SPACE_ID`。 |
| `SKILL_SPACE_ID` | 条件必传 | `KSADK_SKILL_SPACE_IDS` | 否 | 兼容旧/单 space 注入 | 单个 Skill Space id。新部署优先 `KSADK_SKILL_SPACE_IDS`。 |
| `KSADK_SKILL_SERVICE_ACCOUNT_ID` | 条件必传 | `KSYUN_ACCOUNT_ID` | 否 | 平台 / 租户上下文 | Skill Service 租户隔离 header。KOP 或直连 REST 租户视图通常需要。 |
| `KSADK_SKILL_SERVICE_ACCESS_KEY` | 条件必传 | `KSYUN_ACCESS_KEY`、`KS3_ACCESS_KEY` | 是 | 平台 Secret | AICP KOP 签名 AK。直连 REST 或 bearer token 模式不需要。 |
| `KSADK_SKILL_SERVICE_SECRET_KEY` | 条件必传 | `KSYUN_SECRET_KEY`、`KS3_SECRET_KEY` | 是 | 平台 Secret | AICP KOP 签名 SK。直连 REST 或 bearer token 模式不需要。 |
| `KSADK_SKILL_SERVICE_TOKEN` | 条件必传 | 无 | 是 | 平台 Secret | Bearer token 模式使用；KOP 签名模式通常不使用。 |
| `KSADK_SKILL_SERVICE_REGION` | 否 | `KSYUN_REGION` | 否 | 平台 / 开发者 | 默认 `cn-beijing-6`。 |
| `KSADK_SKILL_SERVICE_API_VERSION` | 否 | 无 | 否 | 平台 / 开发者 | 默认 `2024-06-12`；不要复用 Sandbox KOP 的 `2026-04-01`。 |
| `KSADK_SKILL_SERVICE_SIGN_SERVICE` | 否 | 无 | 否 | 平台 / 开发者 | 默认 `aicp`。 |
| `KSADK_SKILL_MANIFEST_LIMIT` | 否 | 无 | 否 | 平台 / 开发者 | 外层 Agent instruction 最多注入的远端 skill manifest 数量，默认 `30`。 |
| `KSADK_SKILL_MANIFEST_TIMEOUT` | 否 | 无 | 否 | 平台 / 开发者 | 拉取远端 skill manifest 的超时秒数，默认 `5`。 |
| `KSADK_SELECTED_SKILL_NAMES` | 否 | 无 | 否 | Runner / Runtime agent | `execute_skills` 选中的 skill 名称列表，Runtime agent 优先按它下载；通常由 SDK 自动注入。 |
| `KSADK_SKILL_CACHE_DIR` | 否 | 无 | 否 | Runtime agent | Skill archive 下载和解压缓存目录。 |
| `KSADK_SKILL_WORKDIR` | 否 | 无 | 否 | Runtime agent | workflow 工作目录。 |
| `KSADK_SKILL_ARTIFACT_PROJECT` | 否 | 无 | 否 | Runtime agent | 最小 artifact workflow 默认项目名，默认 `ksadk-artifact`。 |

### 2.7 知识库、记忆库、会话存储

| 变量 | 是否必传 | 别名/兼容 | 敏感 | 配置方/来源 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `KSADK_KB_DATASET_ID` | 条件必传 | 无 | 否 | 平台 / 开发者 | 配置后启用知识库检索。 |
| `KSADK_KB_ACCESS_KEY` | 条件必传 | `KSYUN_ACCESS_KEY` | 是 | 平台 Secret | SDK 知识库 backend AK。 |
| `KSADK_KB_SECRET_KEY` | 条件必传 | `KSYUN_SECRET_KEY` | 是 | 平台 Secret | SDK 知识库 backend SK。 |
| `KSADK_KB_ENDPOINT` | 否 | 无 | 否 | 平台 / 开发者 | 默认 `aicp.api.ksyun.com`。 |
| `KSADK_KB_REGION` | 否 | 无 | 否 | 平台 / 开发者 | 默认 `cn-beijing-6`。 |
| `KSADK_KB_SCHEME` | 否 | 无 | 否 | 平台 / 开发者 | KB endpoint 协议。内网 endpoint 默认 `http`，其他默认 `https`。 |
| `KSADK_KB_AMBIENT_POLICY` | 否 | 无 | 否 | 平台 / 开发者 | runtime 自动注入知识库上下文策略：`on_demand/always/disabled`。 |
| `KSADK_LTM_BACKEND` | 否 | 无 | 否 | 开发者 | 长期记忆 backend，默认 `local`，可选 `http/sdk`。 |
| `KSADK_LTM_HTTP_URL` | 条件必传 | 无 | 是 | 平台 Secret | `KSADK_LTM_BACKEND=http` 时需要。 |
| `KSADK_LTM_HTTP_TOKEN` | 条件必传 | 无 | 是 | 平台 Secret | HTTP LTM 鉴权 token。 |
| `KSADK_LTM_ACCESS_KEY` | 条件必传 | `KSYUN_ACCESS_KEY` | 是 | 平台 Secret | SDK LTM AK。 |
| `KSADK_LTM_SECRET_KEY` | 条件必传 | `KSYUN_SECRET_KEY` | 是 | 平台 Secret | SDK LTM SK。 |
| `KSADK_LTM_AMBIENT_POLICY` | 否 | 无 | 否 | 平台 / 开发者 | runtime 自动注入长期记忆上下文策略：`on_demand/always/disabled`。 |
| `KSADK_MEMORY_BACKEND` | 否 | 无 | 否 | 开发者 | 轻量 KV/消息历史 MemoryManager backend，默认 `memory`。 |
| `KSADK_MEMORY_URL` | 条件必传 | 无 | 是 | 开发者 / Secret | `KSADK_MEMORY_BACKEND=redis` 等远端 backend 连接 URL。 |
| `KSADK_SESSION_BACKEND` | 否 | `AGENTENGINE_SESSION_BACKEND`、`KSADK_STM_BACKEND` | 否 | 平台 / 开发者 | 会话 backend，默认 `local`。ADK/STM 也会把它作为兜底。 |
| `KSADK_SESSION_DSN` | 条件必传 | `KSADK_STM_URL`、`KSADK_STM_DB_URL`、`KSADK_ADK_SESSION_URL` | 是 | 平台 Secret | `postgres` / `database` backend 时必传。ADK/STM 也会把它作为兜底。 |
| `KSADK_SESSION_PATH` | 否 | `KSADK_STM_PATH`、`KSADK_STM_DB_PATH` | 否 | 本地运行时 | 本地 SQLite 会话库路径。 |
| `KSADK_SESSION_NAMESPACE` | 否 | `KSADK_WORKSPACE_ID`、`AGENTENGINE_WORKSPACE_ID`、`KSADK_TENANT_ID`、`AGENTENGINE_TENANT_ID` | 否 | 平台 / 开发者 | 会话命名空间。 |

### 2.8 可观测性和 Langfuse

| 变量 | 是否必传 | 别名/兼容 | 敏感 | 配置方/来源 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `LANGFUSE_PUBLIC_KEY` | 条件必传 | 无 | 是 | 平台 Secret / 开发者 | 启用 Langfuse 时需要。 |
| `LANGFUSE_SECRET_KEY` | 条件必传 | 无 | 是 | 平台 Secret / 开发者 | 启用 Langfuse 时需要。 |
| `LANGFUSE_BASE_URL` | 否 | `LANGFUSE_HOST` | 否 | 平台 / 开发者 | Langfuse endpoint。 |
| `LANGFUSE_USE_CALLBACK` | 否 | 无 | 否 | 开发者 | 控制是否启用 callback 集成。 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 条件必传 | 无 | 否 | 平台 / 开发者 | OTel Collector endpoint；未设置 traces 专用 endpoint 时，KsADK 会派生 `/v1/traces`。 |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | 否 | 无 | 否 | 平台 / 开发者 | 通用 OTLP 协议；KsADK 自动 HTTP exporter 当前支持 `http/protobuf`。 |
| `OTEL_EXPORTER_OTLP_HEADERS` | 否 | 无 | 是 | 平台 / 开发者 | 通用 OTLP headers，逗号分隔，值按 URL encoding；可能包含 `Authorization`。 |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | 否 | 无 | 否 | 平台 / 开发者 | traces 专用 endpoint；设置后优先于通用 endpoint。 |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | 否 | 无 | 否 | 平台 / 开发者 | traces 专用 OTLP 协议；设置后优先于通用 protocol。 |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | 否 | 无 | 是 | 平台 / 开发者 | traces 专用 OTLP headers；设置后优先于通用 headers。 |
| `OTEL_SERVICE_NAME` | 否 | 无 | 否 | 平台 / 开发者 | OTel service name。 |
| `OTEL_RESOURCE_ATTRIBUTES` | 否 | 无 | 否 | 平台 / 开发者 | OTel resource attributes。 |

## 3. 通用模型与 LLM 变量

| 变量 | 作用层级 | 是否必传 | 默认值 | 别名/兼容 | 敏感 | 配置方/来源 | 是否业务自定义 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `OPENAI_API_KEY` | 本地运行时 / Runtime 镜像 / OpenClaw / Hermes | 条件必传 | 未设置 | `LLM_API_KEY`、`MODEL_API_KEY`、部分 OpenClaw 场景使用 `OPENCLAW_MODEL_API_KEY` | 是 | 开发者 / Secret | 否 | OpenAI 兼容接口 API key。 |
| `OPENAI_BASE_URL` | 本地运行时 / Runtime 镜像 / OpenClaw / Hermes | 条件必传 | 未设置 | `OPENAI_API_BASE`、`LLM_API_BASE`、`MODEL_API_BASE`、部分 OpenClaw 场景使用 `OPENCLAW_MODEL_BASE_URL` | 否 | 开发者 / 平台 | 否 | OpenAI 兼容接口 base url。 |
| `OPENAI_MODEL_NAME` | 本地运行时 / Runtime 镜像 | 条件必传 | 未设置 | `LLM_MODEL`、`MODEL_NAME`、Hermes fallback 读取 `OPENAI_FALLBACK_MODEL_NAME` | 否 | 开发者 / 平台 | 否 | 默认模型名。 |
| `AGENTENGINE_MODEL_POLICY_JSON` | 托管运行时 / Runtime 镜像 | 否 | 未设置 | 无 | 否 | 平台 / Runtime 注入 | 否 | 统一模型策略 JSON，声明 primary / multimodal / fallback 角色及默认 model options。0.6.6 的 policy v1 默认主模型为 `glm-5.2`，多模态模型为 `kimi-k2.7-code`，fallback 为 `deepseek-v4-pro`。 |
| `OPENAI_CONTEXT_LENGTH` | Hermes / 模型配置 | 否 | 未设置 | `MODEL_CONTEXT_LENGTH`、`HERMES_CONTEXT_LENGTH` | 否 | 开发者 / 平台 | 否 | 模型上下文长度提示。 |
| `OPENAI_FALLBACK_MODEL_NAME` | Hermes / 模型配置 | 否 | 未设置 | `HERMES_FALLBACK_MODEL` | 否 | 开发者 / 平台 | 否 | Hermes fallback 模型名 fallback。 |
| `LLM_API_KEY` | Serverless / 兼容模型配置 | 条件必传 | 未设置 | `OPENAI_API_KEY`、`MODEL_API_KEY` | 是 | 平台 Secret / 开发者 | 否 | Serverless 平台兼容模型 API key。 |
| `LLM_API_BASE` | Serverless / 兼容模型配置 | 条件必传 | 未设置 | `OPENAI_BASE_URL`、`MODEL_API_BASE` | 否 | 平台 / 开发者 | 否 | Serverless 平台兼容模型 endpoint。 |
| `LLM_MODEL` | Serverless / OpenClaw | 条件必传 | 未设置 | `OPENAI_MODEL_NAME`、`MODEL_NAME` | 否 | 平台 / 开发者 | 否 | Serverless/OpenClaw 兼容模型名。 |
| `MODEL_API_KEY` | OpenClaw / 兼容模型配置 | 条件必传 | 未设置 | `OPENAI_API_KEY` | 是 | 开发者 / Secret | 否 | 兼容 OpenClaw 模型配置。 |
| `MODEL_API_BASE` | OpenClaw / 兼容模型配置 | 条件必传 | 未设置 | `OPENAI_BASE_URL` | 否 | 开发者 / 平台 | 否 | 兼容 OpenClaw 模型 endpoint。 |
| `MODEL_BASE_URL` | CLI model / 兼容模型配置 | 否 | 未设置 | `OPENAI_BASE_URL`、`OPENAI_API_BASE`、`MODEL_API_BASE` | 否 | 开发者 / 平台 | 否 | 部分 CLI model 命令和历史配置读取的 base url。 |
| `MODEL_NAME` | 本地运行时 / OpenClaw | 条件必传 | 未设置 | `OPENAI_MODEL_NAME` | 否 | 开发者 / 平台 | 否 | 旧版模型名变量。 |
| `COZE_WORKLOAD_IDENTITY_API_KEY` | Coze 导出项目兼容 | 条件必传 | 未设置 | 未设置时可由 `OPENAI_API_KEY` 自动补齐 | 是 | 开发者 / Secret | 否 | 部分 Coze 导出项目依赖 `coze_coding_dev_sdk`，SDK 会尝试从 OpenAI 兼容配置补齐。 |
| `COZE_INTEGRATION_BASE_URL` | Coze 导出项目兼容 | 条件必传 | 未设置 | 未设置时可由 `OPENAI_BASE_URL` 自动补齐 | 否 | 开发者 / 平台 | 否 | Coze integration endpoint。 |
| `COZE_INTEGRATION_MODEL_BASE_URL` | Coze 导出项目兼容 | 条件必传 | 未设置 | 未设置时可由 `OPENAI_BASE_URL` 自动补齐 | 否 | 开发者 / 平台 | 否 | Coze model endpoint。 |
| `COZE_MODEL_NAME` | Coze 导出项目兼容 | 条件必传 | 未设置 | 通常跟随业务导出项目 | 否 | 开发者 / 平台 | 否 | Coze 导出项目模型名。 |

## 4. 金山云账号、KOP、KS3 与镜像仓库

| 变量 | 作用层级 | 是否必传 | 默认值 | 别名/兼容 | 敏感 | 配置方/来源 | 是否业务自定义 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `KSYUN_ACCESS_KEY` | CLI / KOP / KS3 / Skill Service fallback | 条件必传 | 未设置 | `KS3_ACCESS_KEY` | 是 | 开发者 / CI Secret / K8S Secret | 否 | 金山云 AK。启用云端资源操作、KS3、KOP 签名时需要。 |
| `KSYUN_SECRET_KEY` | CLI / KOP / KS3 / Skill Service fallback | 条件必传 | 未设置 | `KS3_SECRET_KEY` | 是 | 开发者 / CI Secret / K8S Secret | 否 | 金山云 SK。 |
| `KSYUN_ACCOUNT_ID` | CLI / KOP / 权限预检查 / Skill Service fallback | 条件必传 | 未设置 | 无 | 否 | 平台账号 / 开发者 | 否 | 账号 ID。资源管理、租户隔离、个人版 KCR 用户名兜底等场景需要。 |
| `KSYUN_REGION` | CLI / KOP / KS3 / Skill Service fallback | 否 | `cn-beijing-6` | 无 | 否 | 开发者 / 平台 | 否 | 区域。跨环境、预发、生产联调建议显式设置。 |
| `KS_ACCESS_KEY_ID` | 旧 KingsoftCloudConfig | 条件必传 | 未设置 | 建议迁移到 `KSYUN_ACCESS_KEY` | 是 | 兼容旧配置 | 否 | 早期 SDK settings 读取的 AK；不与 `KSYUN_ACCESS_KEY` 自动互通。 |
| `KS_SECRET_ACCESS_KEY` | 旧 KingsoftCloudConfig | 条件必传 | 未设置 | 建议迁移到 `KSYUN_SECRET_KEY` | 是 | 兼容旧配置 | 否 | 早期 SDK settings 读取的 SK；不与 `KSYUN_SECRET_KEY` 自动互通。 |
| `KS_REGION` | 旧 KingsoftCloudConfig | 否 | `cn-beijing-6` | 建议迁移到 `KSYUN_REGION` | 否 | 兼容旧配置 | 否 | 早期 SDK settings 读取的 region；不与 `KSYUN_REGION` 自动互通。 |
| `KS3_ACCESS_KEY` | KS3 / 兼容 fallback | 条件必传 | 未设置 | `KSYUN_ACCESS_KEY` | 是 | 开发者 / Secret | 否 | KS3 专用 AK 兼容变量。 |
| `KS3_SECRET_KEY` | KS3 / 兼容 fallback | 条件必传 | 未设置 | `KSYUN_SECRET_KEY` | 是 | 开发者 / Secret | 否 | KS3 专用 SK 兼容变量。 |
| `KS3_BUCKET` | 构建上传 / 版本发布 | 条件必传 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | 自定义 KS3 bucket。 |
| `KS3_ENDPOINT_MODE` | KS3 上传 | 否 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | KS3 endpoint 选择策略。 |
| `KS3_ENDPOINT_PROBE_TIMEOUT_SECONDS` | KS3 上传 | 否 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | KS3 endpoint 探测超时。 |
| `KS3_UPLOAD_TIMEOUT_SECONDS` | KS3 上传 | 否 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | KS3 上传超时。 |
| `KCR_REGISTRY` | 镜像构建 / MCP / Serverless | 条件必传 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | 镜像仓库地址，通常为 `<registry>/<namespace>`，例如 `agenthzzqy-vpc.ksyunkcr.com/testagent-pub` 或第三方 registry/namespace。 |
| `KCR_ENDPOINT` | 镜像构建 / MCP / Serverless | 否 | `hub.kce.ksyun.com` | 无 | 否 | 开发者 / 平台 | 否 | KCR endpoint。 |
| `KCR_USERNAME` | 镜像构建 / MCP / Serverless | 条件必传 | 未设置 | 个人版 KCR 可回退 `KSYUN_ACCOUNT_ID` | 否 | 开发者 / 平台 | 否 | 镜像仓库访问凭证用户名。企业版 KCR 和第三方镜像仓库必须显式设置；个人版 KCR 可留空并使用 `KSYUN_ACCOUNT_ID` 作为用户名兜底。 |
| `KCR_PASSWORD` | 镜像构建 / MCP / Serverless | 条件必传 | 未设置 | 无 | 是 | 开发者 / Secret | 否 | 镜像仓库访问凭证密码或 token。 |

## 5. 通用 Sandbox Runtime

| 变量 | 作用层级 | 是否必传 | 默认值 | 别名/兼容 | 敏感 | 配置方/来源 | 是否业务自定义 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `KSADK_SANDBOX_BACKEND` | Sandbox backend factory | 否 | `e2b` | 无 | 否 | 平台 / 开发者 | 否 | 通用 sandbox backend。首版支持 `e2b`。 |
| `KSADK_SANDBOX_TYPE` | Sandbox spec | 否 | `aio` | 无 | 否 | 沙箱控制台 / 平台 | 否 | `aio/code/browser/private`。Skill Runtime 默认推荐 `aio`。 |
| `KSADK_SANDBOX_TEMPLATE_ID` | Sandbox spec / Skill Runtime E2B backend | 条件必传 | 未设置 | `KSADK_SKILL_RUNTIME_TEMPLATE_ID` | 否 | 沙箱控制台 / 沙箱团队 | 否 | 远程 sandbox 执行时必传。新部署优先使用。 |
| `KSADK_SANDBOX_TIMEOUT` | Sandbox spec | 否 | `900` | `KSADK_SKILL_RUNTIME_TIMEOUT` | 否 | 平台 / 开发者 | 否 | Sandbox 会话超时秒数。 |
| `KSADK_SANDBOX_ALLOW_INTERNET_ACCESS` | Sandbox spec | 否 | `true` | `KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS` | 否 | 平台 / 开发者 | 否 | 是否允许 sandbox 出网。 |
| `KSADK_SANDBOX_STARTUP_RETRY_ATTEMPTS` | E2B Sandbox backend | 否 | `6` | 无 | 否 | 平台 / 开发者 | 否 | 沙箱创建后 readiness 探测最大重试次数，用于兜底短暂 `NotFoundException` / `FileNotFoundException`。 |
| `KSADK_SANDBOX_STARTUP_RETRY_DELAY` | E2B Sandbox backend | 否 | `0.2` | 无 | 否 | 平台 / 开发者 | 否 | 沙箱 readiness 首次重试间隔秒数，后续指数退避，单次 sleep 上限 1 秒。 |
| `E2B_API_URL` | E2B SDK | 条件必传 | 未设置 | 无 | 否 | 沙箱团队 / Secret 配置 | 否 | E2B 兼容 manager endpoint。使用 E2B backend 时必传。 |
| `E2B_API_KEY` | E2B SDK | 条件必传 | 未设置 | 无 | 是 | 沙箱团队 / Secret 配置 | 否 | E2B API key。严禁写入代码、文档明文、测试 fixture、日志。 |

## 6. Skill Runtime 与 Skill Center

| 变量 | 作用层级 | 是否必传 | 默认值 | 别名/兼容 | 敏感 | 配置方/来源 | 是否业务自定义 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `KSADK_SKILLS_MODE` | ADK Runner | 否 | `auto` | 无 | 否 | 开发者 / 平台 | 否 | `auto/local/sandbox`。`auto` 会根据 sandbox template 或本地 skill 目录自动选择。 |
| `KSADK_LOCAL_SKILLS_DIR` | ADK Runner / Runtime agent | 条件必传 | 未设置 | `KSADK_SKILL_CACHE_DIR` 可作为 Runner 本地扫描 fallback | 否 | 开发者 | 否 | 本地 skill 目录。 |
| `KSADK_SKILL_RUNTIME_BACKEND` | Skill Runtime factory | 否 | `disabled`；未设置且存在 `KSADK_SANDBOX_TEMPLATE_ID` 时自动走 `e2b` | 无 | 否 | 开发者 / 平台 | 否 | `disabled/local_process/e2b`。显式 `disabled` 会阻止自动注入。 |
| `KSADK_SKILL_RUNTIME_TEMPLATE_ID` | Skill Runtime E2B backend | 否 | 未设置 | `KSADK_SANDBOX_TEMPLATE_ID` | 否 | 旧部署 / 兼容 | 否 | 兼容变量。新部署不要优先使用。 |
| `KSADK_SKILL_RUNTIME_TIMEOUT` | Skill Runtime command | 否 | `900` | `KSADK_SANDBOX_TIMEOUT` 在 E2B 会话层优先 | 否 | 开发者 / 平台 | 否 | workflow 命令超时秒数。 |
| `KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS` | Skill Runtime E2B backend | 否 | `true` | `KSADK_SANDBOX_ALLOW_INTERNET_ACCESS` 优先 | 否 | 旧部署 / 兼容 | 否 | 兼容变量。 |
| `KSADK_SKILL_RUNTIME_AGENT_PATH` | local_process backend | 条件必传 | SDK 内置 `ksadk/skills/runtime/agent.py` | 无 | 否 | 开发者 | 否 | 本地进程 backend 的 agent 路径。 |
| `KSADK_SKILL_SERVICE_URL` | Runtime agent / Skill Service client | 条件必传 | 未设置 | 无 | 否 | Skill Service / 平台 | 否 | 配置后从 Skill Center 拉取技能。支持直连 REST 和 AICP KOP endpoint。 |
| `KSADK_SKILL_SERVICE_ENDPOINT` | Runtime agent / AICP resolver | 否 | 按 `KSADK_AICP_ENDPOINT_MODE` 自动选择 | 无 | 否 | Skill Service / 平台 | 否 | 未设置 `KSADK_SKILL_SERVICE_URL` 时覆盖 Skill Service AICP endpoint。 |
| `KSADK_SKILL_SERVICE_SCHEME` | Runtime agent / AICP resolver | 否 | 内网 endpoint 为 `http`，公网默认 `https` | 无 | 否 | Skill Service / 平台 | 否 | 未设置 `KSADK_SKILL_SERVICE_URL` 时覆盖 Skill Service AICP URL scheme。 |
| `KSADK_SKILL_SPACE_IDS` | Runner / Runtime agent | 条件必传 | 未设置 | `SKILL_SPACE_ID` | 否 | Agent 创建/更新 / 平台注入 | 否 | 逗号分隔 Skill Space id。 |
| `KSADK_PUBLIC_SKILL_ALLOWLIST` | Runtime agent | 否 | 未设置 | 无 | 否 | 平台 / Skill Service | 否 | 逗号分隔 public skill 名称白名单；未设置时加载 public space 下全部 active skills。 |
| `KSADK_PUBLIC_SKILL_SPACE_IDS` | Runner / Runtime agent | 否 | 未设置 | 无 | 否 | 平台 / Skill Service | 否 | 逗号分隔官方公共 Skill Space id，会追加在用户 space 之后。 |
| `SKILL_SPACE_ID` | Runtime agent / 兼容 | 条件必传 | 未设置 | `KSADK_SKILL_SPACE_IDS` | 否 | 旧部署 / 单 space 注入 | 否 | 单 space 兼容变量。 |
| `KSADK_SKILL_SERVICE_ACCOUNT_ID` | Skill Service client | 条件必传 | 未设置 | `KSYUN_ACCOUNT_ID` | 否 | 平台租户上下文 | 否 | 租户隔离 account id。 |
| `KSADK_SKILL_SERVICE_ACCESS_KEY` | Skill Service KOP signing | 条件必传 | 未设置 | `KSYUN_ACCESS_KEY`、`KS3_ACCESS_KEY` | 是 | Secret | 否 | AICP KOP endpoint 签名 AK。 |
| `KSADK_SKILL_SERVICE_SECRET_KEY` | Skill Service KOP signing | 条件必传 | 未设置 | `KSYUN_SECRET_KEY`、`KS3_SECRET_KEY` | 是 | Secret | 否 | AICP KOP endpoint 签名 SK。 |
| `KSADK_SKILL_SERVICE_TOKEN` | Skill Service bearer auth | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | Bearer token 模式。 |
| `KSADK_SKILL_SERVICE_REGION` | Skill Service KOP signing | 否 | `cn-beijing-6` | `KSYUN_REGION` | 否 | 平台 / 开发者 | 否 | KOP 签名 region。 |
| `KSADK_SKILL_SERVICE_API_VERSION` | Skill Service KOP action | 否 | `2024-06-12` | 无 | 否 | Skill Service / 平台 | 否 | Skill Center KOP API 版本。 |
| `KSADK_SKILL_SERVICE_SIGN_SERVICE` | Skill Service KOP signing | 否 | `aicp` | 无 | 否 | Skill Service / 平台 | 否 | KOP signing service。 |
| `KSADK_SKILL_MANIFEST_LIMIT` | ADK Runner | 否 | `30` | 无 | 否 | 平台 / 开发者 | 否 | 外层 Agent instruction 最多注入的远端 skill manifest 数量。 |
| `KSADK_SKILL_MANIFEST_TIMEOUT` | Skill Service client | 否 | `5` | 无 | 否 | 平台 / 开发者 | 否 | 拉取远端 skill manifest 的超时秒数。 |
| `KSADK_SELECTED_SKILL_NAMES` | Runtime agent | 否 | 未设置 | 无 | 否 | Runner / Runtime agent | 否 | `execute_skills` 选中的 skill 名称列表，Runtime agent 优先按它下载。 |
| `KSADK_SKILL_ALLOW_HASH_MISMATCH` | Runtime agent / PackageStore | 否 | `false` | 无 | 否 | 调试 / 兼容旧包 | 否 | 允许 ContentHash 校验失败后以 unverified cache 加载旧 skill 包；生产不建议开启。 |
| `KSADK_SKILL_CACHE_DIR` | Runtime agent / PackageStore | 否 | 系统临时目录下 `ksadk-skill-cache` | 无 | 否 | Runtime agent | 否 | Skill archive 下载与解压缓存。 |
| `KSADK_SKILL_WORKDIR` | Runtime agent | 否 | 系统临时目录下 `ksadk-skill-workflow` | 无 | 否 | Runtime agent | 否 | workflow 工作目录。 |
| `KSADK_SKILL_OUTPUT_DIR` | Runtime agent workflow | 否 | `KSADK_SKILL_WORKDIR/artifacts` | 无 | 否 | Runtime agent | 否 | 传给本地 skill workflow 脚本的产物输出目录。 |
| `KSADK_SKILL_ROOT_DIR` | Runtime agent workflow | 否 | 当前执行 skill 根目录 | 无 | 否 | Runtime agent | 否 | 传给本地 skill workflow 脚本的 skill 根目录。 |
| `KSADK_SKILL_ARTIFACT_PROJECT` | Runtime agent | 否 | `ksadk-artifact` | 无 | 否 | Runtime agent | 否 | 最小 artifact workflow 项目目录名。 |
| `KSADK_WORKFLOW_PROMPT` | Runtime agent workflow | 否 | 当前 workflow prompt | 无 | 否 | Runtime agent | 否 | 传给本地 skill workflow 脚本的用户请求文本。 |

## 7. MCP Runtime

| 变量 | 作用层级 | 是否必传 | 默认值 | 别名/兼容 | 敏感 | 配置方/来源 | 是否业务自定义 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `KSADK_ENABLE_MCP_TOOLS` | ADK Runner | 否 | `1` | 无 | 否 | 开发者 / 平台 | 否 | 控制远端 MCP tools 自动注入。 |
| `KSADK_MCP_SERVERS` | MCP runtime | 条件必传 | 未设置 | 无 | 是 | 开发者 / 平台 Secret | 否 | JSON 数组。可能包含 MCP server api_key。 |

## 8. 会话、短期记忆和长期记忆

| 变量 | 作用层级 | 是否必传 | 默认值 | 别名/兼容 | 敏感 | 配置方/来源 | 是否业务自定义 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `KSADK_SESSION_BACKEND` | Sessions | 否 | `local` | `AGENTENGINE_SESSION_BACKEND`、`KSADK_STM_BACKEND` | 否 | 开发者 / 平台 | 否 | 会话存储 backend。ADK/STM 也会把它作为兜底。 |
| `KSADK_SESSION_DSN` | Sessions | 条件必传 | 未设置 | `KSADK_STM_URL`、`KSADK_STM_DB_URL`、`KSADK_ADK_SESSION_URL` | 是 | Secret | 否 | PostgreSQL DSN。`postgres` / `database` backend 时必传。ADK/STM 也会把它作为兜底。 |
| `KSADK_SESSION_PATH` | Sessions | 否 | 项目目录下本地 sqlite 路径 | `KSADK_STM_PATH`、`KSADK_STM_DB_PATH` | 否 | 开发者 / 本地运行时 | 否 | 本地 SQLite 会话路径。 |
| `KSADK_SESSION_NAMESPACE` | Sessions | 否 | 未设置 | `KSADK_WORKSPACE_ID`、`AGENTENGINE_WORKSPACE_ID`、`KSADK_TENANT_ID`、`AGENTENGINE_TENANT_ID` | 否 | 平台 | 否 | 会话 namespace。 |
| `KSADK_TENANT_ID` | Sessions | 否 | 未设置 | `AGENTENGINE_TENANT_ID` | 否 | 平台 | 否 | 租户 id。 |
| `KSADK_WORKSPACE_ID` | Sessions | 否 | 未设置 | `AGENTENGINE_WORKSPACE_ID` | 否 | 平台 | 否 | workspace id。 |
| `KSADK_STM_BACKEND` | 旧 STM / Sessions fallback | 否 | 未设置 | `KSADK_SESSION_BACKEND` | 否 | 兼容旧部署 | 否 | 旧变量。新部署优先 `KSADK_SESSION_BACKEND`，但 ADK/STM 仍可读。 |
| `KSADK_STM_PATH` | 旧 STM / Sessions fallback | 否 | 未设置 | `KSADK_SESSION_PATH` | 否 | 兼容旧部署 | 否 | 旧变量。 |
| `KSADK_STM_DB_PATH` | 旧 STM / Sessions fallback | 否 | 未设置 | `KSADK_SESSION_PATH` | 否 | 兼容旧部署 | 否 | 旧变量。 |
| `KSADK_STM_URL` | 旧 STM / Sessions fallback | 条件必传 | 未设置 | `KSADK_SESSION_DSN` | 是 | 兼容旧部署 | 否 | 旧变量。ADK/STM 仍可读。 |
| `KSADK_STM_DB_URL` | 旧 STM / Sessions fallback | 条件必传 | 未设置 | `KSADK_SESSION_DSN` | 是 | 兼容旧部署 | 否 | 旧变量。ADK/STM 仍可读。 |
| `KSADK_ADK_SESSION_BACKEND` | ADK Memory | 否 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | ADK 原生 session backend。 |
| `KSADK_ADK_SESSION_PATH` | ADK Memory | 否 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | ADK 原生 session sqlite 路径。 |
| `KSADK_ADK_SESSION_URL` | ADK Memory | 条件必传 | 未设置 | `KSADK_SESSION_DSN` | 是 | Secret | 否 | ADK 原生 session 数据库 URL。统一 session DSN 也可兜底。 |
| `KSADK_MEMORY_BACKEND` | MemoryManager | 否 | `memory` | 无 | 否 | 开发者 / 平台 | 否 | 轻量 KV/消息历史 backend。当前内置 `memory`，注册 Redis backend 后可用 `redis`。 |
| `KSADK_MEMORY_URL` | MemoryManager | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | 远端 MemoryManager backend 连接 URL，例如 Redis URL。 |
| `KSADK_MEMORY_PREFIX` | MemoryManager | 否 | `ksadk:memory:` | 无 | 否 | 开发者 / 平台 | 否 | MemoryManager key prefix。 |
| `KSADK_MEMORY_TTL` | MemoryManager | 否 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | MemoryManager 默认 TTL 秒数。 |
| `KSADK_LTM_BACKEND` | Long-term memory | 否 | `local` | 无 | 否 | 开发者 / 平台 | 否 | LTM backend。 |
| `KSADK_LTM_HTTP_URL` | HTTP LTM | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | HTTP LTM URL。 |
| `KSADK_LTM_HTTP_TOKEN` | HTTP LTM | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | HTTP LTM token。 |
| `KSADK_LTM_ACCESS_KEY` | SDK LTM | 条件必传 | 未设置 | `KSYUN_ACCESS_KEY` | 是 | Secret | 否 | SDK LTM AK。 |
| `KSADK_LTM_SECRET_KEY` | SDK LTM | 条件必传 | 未设置 | `KSYUN_SECRET_KEY` | 是 | Secret | 否 | SDK LTM SK。 |
| `KSADK_LTM_REGION` | SDK LTM | 否 | `cn-beijing-6` | 无 | 否 | 平台 / 开发者 | 否 | SDK LTM region。 |
| `KSADK_LTM_ENDPOINT` | SDK LTM | 否 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | SDK LTM endpoint。 |
| `KSADK_LTM_SCHEME` | SDK LTM | 否 | `https` | 无 | 否 | 平台 / 开发者 | 否 | SDK LTM scheme。 |
| `KSADK_LTM_INDEX` | LTM | 否 | 未设置 | 无 | 否 | 开发者 | 否 | LTM index。 |
| `KSADK_LTM_NAMESPACE` | LTM | 否 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | LTM 记忆库 ID；环境变量名保持不变，请填写新版 SDK 的 `MemoryCollectionId`。 |
| `KSADK_LTM_AGENT_ID` | LTM | 否 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | LTM agent id。 |
| `KSADK_LTM_SCENE_ID` | LTM | 否 | `_sys_general` | 无 | 否 | 平台 / 开发者 | 否 | LTM scene id；新版记忆库保存必传，未设置时使用通用场景 `_sys_general`。 |
| `KSADK_LTM_APP_NAME` | LTM | 否 | 未设置 | 无 | 否 | 开发者 | 否 | LTM application name 覆盖。 |
| `KSADK_LTM_TOP_K` | LTM | 否 | `5` | 无 | 否 | 开发者 | 否 | LTM 返回条数。 |
| `KSADK_LTM_AUTO_SAVE` | Conversations runtime | 否 | SDK LTM 已绑定时为 `true` | 无 | 否 | 平台 / 开发者 | 否 | 是否在每轮完成后 best-effort 镜像 user/assistant 文本到记忆库。只接受布尔语义：`true/false`、`1/0`、`on/off`。 |
| `KSADK_LTM_AMBIENT_ENABLED` | Conversations runtime | 否 | `true` | 无 | 否 | 平台 / 开发者 | 否 | 是否允许 runtime 自动加载长期记忆上下文。 |
| `KSADK_LTM_AMBIENT_POLICY` | Conversations runtime | 否 | `on_demand` | 无 | 否 | 平台 / 开发者 | 否 | 长期记忆 ambient context 策略：`on_demand/always/disabled`。 |
| `MEM0_API_KEY` | OpenClaw memory backend | 条件必传 | 未设置 | 无 | 是 | 平台 Secret | 否 | 选择 `mem0` memory backend manifest 时需要。 |
| `MEM0_USER_ID` | OpenClaw memory backend | 条件必传 | 未设置 | 无 | 否 | 平台 / 用户上下文 | 否 | 选择 `mem0` memory backend manifest 时需要。 |
| `MEM0_BASE_URL` | OpenClaw memory backend | 条件必传 | 未设置 | 无 | 否 | 平台 | 否 | 选择 `mem0` memory backend manifest 时需要。 |

## 9. 知识库

| 变量 | 作用层级 | 是否必传 | 默认值 | 别名/兼容 | 敏感 | 配置方/来源 | 是否业务自定义 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `KSADK_KB` | Knowledge base | 否 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | AICP knowledge-base 连接配置前缀。 |
| `KSADK_KB_DATASET_ID` | Knowledge base | 条件必传 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | 存在时启用知识库。 |
| `KSADK_KB_ACCESS_KEY` | Knowledge base | 条件必传 | 未设置 | `KSYUN_ACCESS_KEY` | 是 | Secret | 否 | KB AK。 |
| `KSADK_KB_SECRET_KEY` | Knowledge base | 条件必传 | 未设置 | `KSYUN_SECRET_KEY` | 是 | Secret | 否 | KB SK。 |
| `KSADK_KB_ENDPOINT` | Knowledge base | 否 | `aicp.api.ksyun.com` | 无 | 否 | 平台 / 开发者 | 否 | KB endpoint。 |
| `KSADK_KB_REGION` | Knowledge base | 否 | `cn-beijing-6` | 无 | 否 | 平台 / 开发者 | 否 | KB region。 |
| `KSADK_KB_SCHEME` | Knowledge base | 否 | 内网 endpoint 默认 `http`，其他默认 `https` | 无 | 否 | 平台 / 开发者 | 否 | KB endpoint scheme。 |
| `KSADK_KB_SEARCH_METHOD` | Knowledge base | 否 | `intelligence_search` | 无 | 否 | 开发者 | 否 | 检索方法。 |
| `KSADK_KB_TOP_K` | Knowledge base | 否 | `5` | 无 | 否 | 开发者 | 否 | 返回条数。 |
| `KSADK_KB_SCORE_THRESHOLD` | Knowledge base | 否 | `0.0` | 无 | 否 | 开发者 | 否 | 分数阈值。 |
| `KSADK_KB_RERANKING_ENABLE` | Knowledge base | 否 | `false` | 无 | 否 | 开发者 | 否 | 是否启用 reranking。 |
| `KSADK_KB_AMBIENT_ENABLED` | Conversations runtime | 否 | `true` | 无 | 否 | 平台 / 开发者 | 否 | 是否允许 runtime 自动加载知识库上下文。 |
| `KSADK_KB_AMBIENT_POLICY` | Conversations runtime | 否 | `on_demand` | 无 | 否 | 平台 / 开发者 | 否 | 知识库 ambient context 策略：`on_demand/always/disabled`。 |
| `KSYUN_SECRET_ID` | Knowledge base fallback | 否 | 未设置 | `KSYUN_ACCESS_KEY` | 是 | 兼容旧配置 | 否 | 代码中作为 KB AK 的旧 fallback，建议使用 `KSADK_KB_ACCESS_KEY` 或 `KSYUN_ACCESS_KEY`。 |

## 10. CLI、构建、部署和 UI 行为

| 变量 | 作用层级 | 是否必传 | 默认值 | 别名/兼容 | 敏感 | 配置方/来源 | 是否业务自定义 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `AGENTENGINE_SERVER_URL` | CLI / API client | 否 | 自动探测：优先 `http://aicp.inner.api.ksyun.com`，不可达时回落 `https://aicp.api.ksyun.com` | 无 | 否 | 平台 / 开发者 | 否 | 覆盖 AgentEngine Server 地址。内部账号/内网环境建议显式设为 `http://aicp.inner.api.ksyun.com`；公网账号通常不设置或使用 `https://aicp.api.ksyun.com`。如果公网 AICP 返回 `InnerAccountCanOnlyAccessThroughIntranet`，客户端会自动切内网重试一次。 |
| `AGENTENGINE_API_VERSION` | CLI / API client | 否 | 内置版本 | 无 | 否 | 平台 / 开发者 | 否 | 覆盖 AgentEngine API version。 |
| `AGENTENGINE_PRE_CONTROL_REGION` | CLI / API client | 否 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | 预发控制面 region 覆盖。 |
| `AGENTENGINE_PRE_CUSTOM_SOURCE` | CLI / API client | 否 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | 预发 custom source 覆盖。 |
| `KSADK_AICP_ENDPOINT_MODE` | AICP resolver | 否 | `auto` | 无 | 否 | 平台 / 开发者 | 否 | AICP endpoint 选择策略，支持 `auto/detect/internal/inner/public`。内网环境可显式设为 `inner`，跳过自动探测。 |
| `AGENTENGINE_MODEL_ALLOWLIST` | CLI model / OpenClaw | 否 | 未设置 | `OPENCLAW_MODEL_ALLOWLIST` | 否 | 平台 / 开发者 | 否 | 模型列表过滤。OpenClaw 场景优先使用 `OPENCLAW_MODEL_ALLOWLIST`。 |
| `AGENTENGINE_UI_DIR` | 本地 Web UI / Sessions | 否 | 未设置 | 无 | 否 | 本地开发者 | 否 | 本地 UI 静态目录覆盖，主要用于 Web/文件上传本地调试。 |
| `KSADK_WEB_VERSION` | Hosted Web UI static sync | 否 | `latest` | 可显式设置 `0.2.7` / `v0.2.7` | 否 | 构建环境 / 开发者 | 否 | `make sync-ksadk-web-static` 使用的 `@kingsoftcloud/ksadk-web` npm dist-tag 或版本，默认消费最新 release。 |
| `KSADK_WEB_PACKAGE` | Hosted Web UI static sync | 否 | `@kingsoftcloud/ksadk-web` | 无 | 否 | 构建环境 / 开发者 | 否 | 本地 UI static 同步使用的 npm 包名。 |
| `KSADK_WEB_TARBALL_NAME` | Hosted Web UI static sync | 否 | 根据 `KSADK_WEB_VERSION` 派生 | 无 | 否 | 构建环境 | 否 | 仅在设置 `KSADK_WEB_RELEASE_URL` 时作为下载保存文件名；npm pack 模式会使用 npm 返回的真实 tarball 文件名。 |
| `KSADK_WEB_RELEASE_URL` | Hosted Web UI static sync | 否 | 未设置 | 无 | 否 | 构建环境 / 开发者 | 否 | 可选兼容兜底。设置后跳过 npm pack，改从该 tarball URL 下载。 |
| `KSADK_WEB_CACHE_DIR` | Hosted Web UI static sync | 否 | `.cache/ksadk-web` | 无 | 否 | 构建环境 / 开发者 | 否 | KsADK Web 包解压缓存目录。 |
| `KSADK_GLOBAL_CONFIG_ENV_KEYS` | CLI | 否 | 未设置 | 无 | 否 | CLI 内部 | 否 | CLI 启动时记录哪些环境变量由 `~/.agentengine/settings.json` 补入，用于区分用户显式环境变量和全局配置默认值。 |
| `AGENTENGINE_LOCAL_RUNTIME_VENV_REEXEC` | 本地 runtime CLI | 否 | 自动判断 | 无 | 否 | 本地开发者 / 测试 | 否 | 控制本地 runtime 是否在虚拟环境中 re-exec。普通用户通常无需设置。 |
| `AGENTENGINE_WEB_VENV_REEXEC` | 本地 Web CLI | 否 | 自动判断 | 无 | 否 | 本地开发者 / 测试 | 否 | 控制本地 Web 命令是否在虚拟环境中 re-exec。普通用户通常无需设置。 |
| `AGENTENGINE_DEBUG` | CLI | 否 | 未设置 | 无 | 否 | 开发者 | 否 | 开启更详细错误输出。 |
| `AGENTENGINE_GLOBAL_DRY_RUN` | CLI / API client | 否 | 未设置 | 无 | 否 | 开发者 / 测试 | 否 | 全局 dry-run 开关。 |
| `AGENTENGINE_OUTPUT_MODE` | CLI | 否 | `pretty` | 无 | 否 | 开发者 / CI | 否 | 输出模式，影响 JSON/pretty 渲染。 |
| `AGENTENGINE_NO_COLOR` | CLI | 否 | 未设置 | `NO_COLOR` | 否 | 开发者 / CI | 否 | 禁用彩色输出。 |
| `SESSION_TITLE_MODEL` | Conversations runtime | 否 | 未设置 | 默认模型配置 | 否 | 开发者 / 平台 | 否 | 会话标题生成模型覆盖。 |
| `COMPACTION_DISABLE_SEMANTIC` | Conversations runtime | 否 | `false` | 无 | 否 | 开发者 / 平台 | 否 | 禁用语义压缩摘要。 |
| `COMPACTION_SUMMARY_TIMEOUT_MS` | Conversations runtime | 否 | `45000` | 无 | 否 | 开发者 / 平台 | 否 | 语义压缩摘要超时毫秒数。 |
| `COMPACTION_SUMMARY_MAX_GROUPS` | Conversations runtime | 否 | `12` | 无 | 否 | 开发者 / 平台 | 否 | 单次语义压缩最大分组数。 |
| `COMPACTION_SUMMARY_MODEL` | Conversations runtime | 否 | 默认模型配置 | 无 | 否 | 开发者 / 平台 | 否 | 语义压缩摘要模型覆盖。 |
| `PORT` | Runtime image / Web | 否 | `8080` | `KSADK_RUNTIME_PORT` 在部分模板中转写 | 否 | 平台 / Runtime 镜像 | 否 | 容器监听端口。业务服务也可能读取同名变量；此时属于业务自定义。 |
| `HOST` | MCP / Web runtime | 否 | `0.0.0.0` | 无 | 否 | Runtime 镜像 | 否 | MCP/Web 服务监听地址。 |
| `LOG_LEVEL` | Runtime image | 否 | `INFO` | 无 | 否 | 开发者 / 平台 | 否 | 模板运行时日志级别。 |
| `CODE_PATH` | Runtime image | 否 | `/app/code` | 无 | 否 | Runtime 镜像 | 否 | 代码包解压/挂载目录。 |
| `PIP_INDEX_URL` | 构建 / Runtime image | 否 | pip 默认 | `UV_INDEX_URL` | 否 | 开发者 / 平台 | 否 | Python 依赖安装源。 |
| `UV_INDEX_URL` | 构建 / Runtime image | 否 | uv 默认 | `PIP_INDEX_URL` | 否 | 开发者 / 平台 | 否 | uv 依赖安装源。 |
| `KSADK_BUILD_PIP_INSTALL_TIMEOUT_SECONDS` | Code Builder | 否 | `2700` | 无 | 否 | 构建环境 / 开发者 | 否 | 源码构建时 `pip install` 总超时秒数。 |
| `KSADK_BUILD_ENABLE_ATTACHMENT_OCR` | Code Builder / Container Builder | 否 | `false` | 无 | 否 | 构建环境 / 开发者 | 否 | 是否把平台本地 OCR 依赖打进代码包。不开启不影响多模态模型直接消费 `input_image`。 |
| `KSADK_BUILD_ENABLE_MCP` | Code Builder / Container Builder | 否 | `false` | 无 | 否 | 构建环境 / 开发者 | 否 | 强制把 `mcp` / `langchain-mcp-adapters` 打进包。通常会根据项目 import 或非空 `KSADK_MCP_SERVERS` 自动启用；`[]` 不会启用。 |
| `KSADK_BUILD_ENABLE_POSTGRES_SESSION` | Code Builder / Container Builder | 否 | `false` | 无 | 否 | 构建环境 / 开发者 | 否 | 强制把 `asyncpg` 打进包。通常会根据 `KSADK_SESSION_BACKEND=postgres`、`KSADK_SESSION_DSN` 或 PostgreSQL DSN 自动启用。 |
| `KSADK_RUNTIME_PORT` | Runtime image / CLI | 否 | `8080` | 无 | 否 | 平台 | 否 | 模板运行时 HTTP 端口。 |
| `KSADK_PROJECT_DIR` | Sessions / Web | 否 | 当前工作目录 | 无 | 否 | 本地运行时 | 否 | 本地 session/workspace 状态 project root。 |
| `KSADK_RESPONSES_SESSION_HEADER` | RemoteRunner | 否 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | 远端 Responses session 透传 header 名称。 |
| `KSADK_TERMINAL_EXEC_SUBCOMMAND_ALLOWLIST` | Terminal exec | 否 | 默认常见只读命令 | 无 | 否 | 平台 / 开发者 | 否 | 追加允许远程 terminal exec 透传的命令前缀，多个前缀用逗号、分号或换行分隔；例如 `config,openclaw config`。设置为 `*` 时允许全部远程 exec 命令。 |
| `KSADK_TOOL_APPROVAL_MODE` | Built-in tools / Conversations runtime | 否 | `off` | 无 | 否 | 平台 / 开发者 | 否 | 内置工具审批模式；`strict` 时中高风险工具需要审批。 |
| `KSADK_FEISHU_APP_ID` | OpenClaw diagnostics | 否 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | 飞书辅助 app id。 |
| `KSADK_FEISHU_RESULT_PATH` | OpenClaw diagnostics | 否 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | 飞书辅助结果路径。 |
| `KSADK_WORKSPACE_FILES_ENABLED` | Hermes/OpenClaw workspace files | 否 | 镜像内通常默认 `1` | `OPENCLAW_WORKSPACE_FILES_ENABLED` | 否 | Runtime 镜像 / 平台 | 否 | 工作区文件服务开关。 |
| `KSADK_WORKSPACE_ROOT` | Hermes/OpenClaw workspace files | 否 | 镜像工作目录 | `OPENCLAW_WORKSPACE_DIR`、`HERMES_WORKDIR` | 否 | Runtime 镜像 / 平台 | 否 | 工作区根目录。 |

## 11. 可观测性

| 变量 | 作用层级 | 是否必传 | 默认值 | 别名/兼容 | 敏感 | 配置方/来源 | 是否业务自定义 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `LANGFUSE_PUBLIC_KEY` | Tracing / Runtime | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | Langfuse public key。 |
| `LANGFUSE_SECRET_KEY` | Tracing / Runtime | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | Langfuse secret key。 |
| `LANGFUSE_BASE_URL` | Tracing / Runtime | 否 | 未设置 | `LANGFUSE_HOST` | 否 | 平台 / 开发者 | 否 | Langfuse endpoint。 |
| `LANGFUSE_HOST` | Tracing / Runtime | 否 | 未设置 | `LANGFUSE_BASE_URL` | 否 | 兼容旧配置 | 否 | Langfuse endpoint 旧变量。 |
| `LANGFUSE_PROJECT_ID` | Tracing | 否 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | Langfuse project id。 |
| `LANGFUSE_USE_CALLBACK` | Tracing | 否 | 未设置 | 无 | 否 | 开发者 | 否 | 是否启用 Langfuse callback。 |
| `LANGCHAIN_TRACING_V2` | LangChain tracing | 否 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | LangChain v2 tracing 开关。 |
| `LANGCHAIN_VERBOSE` | Runtime image | 否 | `true` | 无 | 否 | 开发者 / 平台 | 否 | 模板运行时 LangChain verbose 开关。 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTel | 条件必传 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | OTel Collector endpoint；未设置 traces 专用 endpoint 时，KsADK 会派生 `/v1/traces`。 |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | OTel | 否 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | 通用 OTLP 协议；KsADK 自动 HTTP exporter 当前支持 `http/protobuf`。 |
| `OTEL_EXPORTER_OTLP_HEADERS` | OTel | 否 | 未设置 | 无 | 是 | 平台 / 开发者 | 否 | 通用 OTLP headers，逗号分隔，值按 URL encoding；可能包含 `Authorization`。 |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | OTel | 否 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | traces 专用 endpoint；设置后优先于通用 endpoint。 |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | OTel | 否 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | traces 专用 OTLP 协议；设置后优先于通用 protocol。 |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | OTel | 否 | 未设置 | 无 | 是 | 平台 / 开发者 | 否 | traces 专用 OTLP headers；设置后优先于通用 headers。 |
| `OTEL_SERVICE_NAME` | OTel | 否 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | service name。 |
| `OTEL_RESOURCE_ATTRIBUTES` | OTel | 否 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | resource attributes。 |

## 12. Hermes 和 OpenClaw 常见运行时变量

Hermes / OpenClaw 有大量镜像启动和安全策略变量，本文只列常见运行时可配置项。`*_PID`、`*_MARKER`、`*_CACHE_DIR`、`*_SPEC`、`*_PLUGIN_ID`、`*_PATCH_ROOTS`、`*_READY_STATUSES` 等主要是脚本内部状态或模板常量，未逐项列出。完整模板变量以 `deploy/hermes/`、`deploy/openclaw/`、`deploy/openclaw-user-template/` 内 README 和 bootstrap 脚本为准。

| 变量 | 作用层级 | 是否必传 | 默认值 | 别名/兼容 | 敏感 | 配置方/来源 | 是否业务自定义 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `HERMES_MODEL_PROVIDER` | Hermes | 否 | `custom` | 无 | 否 | 开发者 / 平台 | 否 | Hermes 模型 provider。 |
| `HERMES_CONTEXT_LENGTH` | Hermes | 否 | `OPENAI_CONTEXT_LENGTH` / `MODEL_CONTEXT_LENGTH` | 无 | 否 | 开发者 / 平台 | 否 | 上下文长度。 |
| `HERMES_COMPRESSION_MODEL` | Hermes | 否 | `OPENAI_MODEL_NAME` | 无 | 否 | 开发者 / 平台 | 否 | 压缩模型。 |
| `HERMES_COMPRESSION_BASE_URL` | Hermes | 否 | `OPENAI_BASE_URL` | 无 | 否 | 开发者 / 平台 | 否 | 压缩模型 endpoint。 |
| `HERMES_COMPRESSION_PROVIDER` | Hermes | 否 | `HERMES_MODEL_PROVIDER` | 无 | 否 | 开发者 / 平台 | 否 | 压缩模型 provider。 |
| `HERMES_COMPRESSION_CONTEXT_LENGTH` | Hermes | 否 | `HERMES_CONTEXT_LENGTH` | 无 | 否 | 开发者 / 平台 | 否 | 压缩模型上下文长度。 |
| `HERMES_COMPRESSION_TIMEOUT` | Hermes | 否 | `120` | 无 | 否 | 开发者 / 平台 | 否 | 压缩请求超时秒数。 |
| `HERMES_FALLBACK_MODEL` | Hermes | 否 | `OPENAI_FALLBACK_MODEL_NAME` | 无 | 否 | 开发者 / 平台 | 否 | fallback 模型。 |
| `HERMES_FALLBACK_BASE_URL` | Hermes | 否 | `OPENAI_BASE_URL` | 无 | 否 | 开发者 / 平台 | 否 | fallback endpoint。 |
| `HERMES_FALLBACK_PROVIDER` | Hermes | 否 | `custom` | 无 | 否 | 开发者 / 平台 | 否 | fallback 模型 provider。 |
| `HERMES_HOSTED_RUNTIME` | Hermes | 否 | `1` | 无 | 否 | Runtime 镜像 / 平台 | 否 | 标识 Hermes 以 hosted runtime 模式运行。 |
| `HERMES_HOME` | Hermes | 否 | `${HERMES_STATE_DIR}` | 无 | 否 | Runtime 镜像 | 否 | Hermes 状态根目录。 |
| `HERMES_STATE_DIR` | Hermes | 否 | `${HOME}/.hermes` | 无 | 否 | Runtime 镜像 | 否 | Hermes 状态目录。 |
| `HERMES_WORKDIR` | Hermes | 否 | 镜像默认值 | 无 | 否 | Runtime 镜像 | 否 | Hermes 工作目录。 |
| `HERMES_RUN_DIR` | Hermes | 否 | `${HERMES_HOME}/run` | 无 | 否 | Runtime 镜像 | 否 | Hermes 运行时 PID/socket 目录。 |
| `HERMES_SESSION_DIR` | Hermes | 否 | `${HERMES_HOME}/sessions` | 无 | 否 | Runtime 镜像 | 否 | Hermes 会话目录。 |
| `MCPORTER_HOME` | Hermes | 否 | `${HERMES_HOME}/mcporter` | 无 | 否 | Runtime 镜像 | 否 | MCPorter 状态目录。 |
| `XDG_CONFIG_HOME` | Hermes | 否 | `${HERMES_HOME}/xdg/config` | 无 | 否 | Runtime 镜像 | 否 | XDG config 目录覆盖。 |
| `XDG_CACHE_HOME` | Hermes | 否 | `${HERMES_HOME}/xdg/cache` | 无 | 否 | Runtime 镜像 | 否 | XDG cache 目录覆盖。 |
| `XDG_STATE_HOME` | Hermes | 否 | `${HERMES_HOME}/xdg/state` | 无 | 否 | Runtime 镜像 | 否 | XDG state 目录覆盖。 |
| `AGENT_BROWSER_HOME` | Hermes browser | 否 | `/usr/local/lib/node_modules/agent-browser` | 无 | 否 | Runtime 镜像 | 否 | browser agent 安装目录。 |
| `AGENT_BROWSER_EXECUTABLE_PATH` | Hermes/OpenClaw browser | 否 | `/usr/bin/chromium` 或自动探测 | `OPENCLAW_BROWSER_EXECUTABLE_PATH` | 否 | Runtime 镜像 / 开发者 | 否 | 浏览器可执行文件路径覆盖。 |
| `AGENT_BROWSER_STATE_DIR` | Hermes browser | 否 | `${HERMES_HOME}/browser` | 无 | 否 | Runtime 镜像 | 否 | browser agent 状态目录。 |
| `AGENT_BROWSER_RUN_DIR` | Hermes browser | 否 | `${AGENT_BROWSER_STATE_DIR}/run` | 无 | 否 | Runtime 镜像 | 否 | browser agent 运行目录。 |
| `AGENT_BROWSER_SESSION_DIR` | Hermes browser | 否 | `${AGENT_BROWSER_STATE_DIR}/sessions` | 无 | 否 | Runtime 镜像 | 否 | browser agent 会话目录。 |
| `AGENT_BROWSER_SOCKET_DIR` | Hermes browser | 否 | `${AGENT_BROWSER_RUN_DIR}` | 无 | 否 | Runtime 镜像 | 否 | browser agent socket 目录。 |
| `AGENT_BROWSER_ARTIFACTS_DIR` | Hermes browser | 否 | `${AGENT_BROWSER_STATE_DIR}/artifacts` | 无 | 否 | Runtime 镜像 | 否 | browser agent 产物目录。 |
| `AGENT_BROWSER_LOG_DIR` | Hermes browser | 否 | `${AGENT_BROWSER_STATE_DIR}/logs` | 无 | 否 | Runtime 镜像 | 否 | browser agent 日志目录。 |
| `API_SERVER_ENABLED` | Hermes API server | 否 | `true` | 无 | 否 | Runtime 镜像 / 开发者 | 否 | Hermes 内置 API server 开关。 |
| `API_SERVER_HOST` | Hermes API server | 否 | `127.0.0.1` | 无 | 否 | Runtime 镜像 / 开发者 | 否 | Hermes 内置 API server host。 |
| `API_SERVER_PORT` | Hermes API server | 否 | `8642` | 无 | 否 | Runtime 镜像 / 开发者 | 否 | Hermes 内置 API server port。 |
| `API_SERVER_KEY` | Hermes API server | 条件必传 | 未设置 | `HERMES_API_SERVER_KEY` | 是 | Secret | 否 | Hermes API server 鉴权 key。 |
| `TAVILY_API_KEY` | Hermes / OpenClaw web search | 条件必传 | 未设置 | `OPENCLAW_TAVILY_API_KEY` | 是 | Secret | 否 | Tavily 搜索 key。 |
| `FIRECRAWL_API_KEY` | Hermes web/search skill | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | bundled web/search skill 使用 Firecrawl 时需要。 |
| `EXA_API_KEY` | Hermes web/search skill | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | bundled web/search skill 使用 Exa 时需要。 |
| `PARALLEL_API_KEY` | Hermes web/search skill | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | bundled web/search skill 使用 Parallel 时需要。 |
| `BROWSERBASE_API_KEY` | Hermes browser skill | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | browser skill 使用 Browserbase 时需要。 |
| `BROWSER_USE_API_KEY` | Hermes browser skill | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | browser-use 云服务模式需要。 |
| `CAMOFOX_URL` | Hermes browser skill | 条件必传 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | browser skill 使用 Camofox 服务时的 endpoint。 |
| `KDOCS_OPEN_BROWSER` | Hermes kdocs skill | 否 | `0` | 无 | 否 | 开发者 | 否 | kdocs token 获取脚本是否自动打开浏览器。 |
| `HERMES_DASHBOARD_HOST` | Hermes | 否 | `127.0.0.1` | 无 | 否 | Runtime 镜像 / 开发者 | 否 | Dashboard 监听 host。 |
| `HERMES_DASHBOARD_PORT` | Hermes | 否 | `9119` | 无 | 否 | Runtime 镜像 / 开发者 | 否 | Dashboard 端口。 |
| `HERMES_UI_LOCALE` | Hermes | 否 | `zh` | `LANG`、`LC_ALL` | 否 | Runtime 镜像 / 开发者 | 否 | UI 语言。 |
| `HERMES_API_SERVER_KEY` | Hermes CLI | 条件必传 | 未设置 | `API_SERVER_KEY` | 是 | Secret | 否 | Hermes API server 鉴权 key。 |
| `HERMES_IMAGE` | Hermes CLI | 否 | CLI 内置镜像 | `HERMES_DOCKER_IMAGE` | 否 | 开发者 / CI | 否 | Hermes 镜像覆盖。 |
| `HERMES_RESOURCE` | Hermes CLI | 否 | 未设置 | 无 | 否 | 开发者 / 平台 | 否 | Hermes 资源规格覆盖。 |
| `OPENCLAW_GATEWAY_AUTH_MODE` | OpenClaw | 条件必传 | 模板默认值 | 无 | 否 | 平台 / 开发者 | 否 | Gateway 鉴权模式。 |
| `OPENCLAW_GATEWAY_TOKEN` | OpenClaw | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | token 模式鉴权 token。 |
| `OPENCLAW_GATEWAY_PASSWORD` | OpenClaw | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | password 模式鉴权密码。 |
| `OPENCLAW_GATEWAY_PORT` | OpenClaw | 否 | 模板默认值 | 无 | 否 | Runtime 镜像 / 平台 | 否 | Gateway 端口。 |
| `OPENCLAW_GATEWAY_BIND` | OpenClaw | 否 | `lan` | 无 | 否 | Runtime 镜像 / 平台 | 否 | gateway 绑定模式。 |
| `OPENCLAW_GATEWAY_TRUSTED_PROXY_USER_HEADER` | OpenClaw | 条件必传 | 模板默认值 | `OPENCLAW_TRUSTED_PROXY_USER_HEADER` | 否 | 平台 | 否 | trusted-proxy 用户 header。 |
| `OPENCLAW_TRUSTED_PROXIES` | OpenClaw | 否 | 模板默认值 | 无 | 否 | 平台 | 否 | trusted-proxy 允许代理列表。 |
| `OPENCLAW_INTERNAL_TRUSTED_PROXY_USER` | OpenClaw | 否 | `openclaw-backend` | 无 | 否 | Runtime 镜像 / 平台 | 否 | 内部 loopback 请求用户。 |
| `OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER` | OpenClaw | 否 | `OPENCLAW_TRUSTED_PROXY_USER_HEADER` | 无 | 否 | Runtime 镜像 / 平台 | 否 | 内部 loopback 用户 header。 |
| `OPENCLAW_ALLOWED_ORIGINS` | OpenClaw | 否 | 模板默认值 | 无 | 否 | 平台 / 开发者 | 否 | CORS allowed origins，支持列表/JSON。 |
| `OPENCLAW_ALLOW_INSECURE_AUTH` | OpenClaw | 否 | `false` | 无 | 否 | 开发者 / 测试 | 否 | 允许不安全鉴权配置，生产不要开启。 |
| `OPENCLAW_DISABLE_DEVICE_AUTH` | OpenClaw | 否 | `false` | 无 | 否 | 开发者 / 测试 | 否 | 禁用设备鉴权。 |
| `OPENCLAW_MODEL_API_KEY` | OpenClaw | 条件必传 | 未设置 | `OPENAI_API_KEY` / `MODEL_API_KEY` | 是 | Secret | 否 | OpenClaw 模型 API key。 |
| `OPENCLAW_MODEL_BASE_URL` | OpenClaw | 条件必传 | 未设置 | `OPENAI_BASE_URL` / `MODEL_API_BASE` | 否 | 平台 / 开发者 | 否 | OpenClaw 模型 endpoint。 |
| `OPENCLAW_DEFAULT_MODEL` | OpenClaw | 条件必传 | 未设置 | `OPENAI_MODEL_NAME` / `MODEL_NAME` | 否 | 平台 / 开发者 | 否 | OpenClaw 默认模型。 |
| `OPENCLAW_FALLBACK_MODEL` | OpenClaw | 否 | `AGENTENGINE_MODEL_POLICY_JSON` 的 fallback 或未设置 | 无 | 否 | 平台 / 开发者 | 否 | OpenClaw fallback 模型。显式设置时优先于平台策略。 |
| `OPENCLAW_IMAGE_MODEL` | OpenClaw | 否 | `AGENTENGINE_MODEL_POLICY_JSON` 的 multimodal 或未设置 | 无 | 否 | 平台 / 开发者 | 否 | OpenClaw 多模态 / 图像场景模型。 |
| `OPENCLAW_MODEL_PROVIDER_ID` | OpenClaw | 否 | `ksyun` | 无 | 否 | 平台 / 开发者 | 否 | OpenClaw 模型 provider id。 |
| `OPENCLAW_MODEL_API` | OpenClaw | 否 | `openai-completions` | 无 | 否 | 平台 / 开发者 | 否 | OpenClaw 模型 API 类型。 |
| `OPENCLAW_MODEL_CATALOG_JSON` | OpenClaw | 否 | 自动生成 | 无 | 否 | 平台 / 开发者 | 否 | 覆盖模型 catalog。 |
| `OPENCLAW_MODEL_ALLOWLIST` | OpenClaw | 否 | 未设置 | `AGENTENGINE_MODEL_ALLOWLIST` | 否 | 平台 / 开发者 | 否 | OpenClaw 模型白名单。 |
| `OPENCLAW_MODEL_API_KEY_SECRET_SOURCE` | OpenClaw | 否 | 模板默认值 | 无 | 否 | 平台 | 否 | 模型 API key secret 来源，例如 env/file。 |
| `OPENCLAW_MODEL_API_KEY_SECRET_PROVIDER` | OpenClaw | 否 | 模板默认值 | 无 | 否 | 平台 | 否 | 模型 API key secret provider 标识。 |
| `OPENCLAW_MODEL_API_KEY_SECRET_ID` | OpenClaw / safe exec | 条件必传 | 未设置 | 无 | 是 | Secret 配置 | 否 | file/secret-provider 模式下的模型或 web-search key 引用 ID。 |
| `OPENCLAW_MODEL_API_KEY_SECRET_FILE_PATH` | OpenClaw | 条件必传 | 模板默认值 | 无 | 是 | Secret 挂载 | 否 | file secret 模式下的 key 文件路径。 |
| `OPENCLAW_BROWSER_ENABLED` | OpenClaw | 否 | 安全策略决定 | 无 | 否 | 平台 / 开发者 | 否 | 是否启用浏览器能力。 |
| `OPENCLAW_BROWSER_NO_SANDBOX` | OpenClaw | 否 | `true` | 无 | 否 | Runtime 镜像 / 平台 | 否 | Chromium no-sandbox 开关。 |
| `OPENCLAW_BROWSER_HEADLESS` | OpenClaw | 否 | `true` | 无 | 否 | Runtime 镜像 / 平台 | 否 | 浏览器 headless 开关。 |
| `OPENCLAW_BROWSER_EXECUTABLE_PATH` | OpenClaw | 否 | 自动探测 | `OPENCLAW_BROWSER_EXECUTABLE` | 否 | Runtime 镜像 / 平台 | 否 | 浏览器可执行文件路径。 |
| `OPENCLAW_BROWSER_SSRF_POLICY_JSON` | OpenClaw | 否 | 模板默认策略 | 无 | 否 | 平台 / 开发者 | 否 | 浏览器 SSRF 策略 JSON。 |
| `OPENCLAW_WEB_FETCH_ENABLED` | OpenClaw | 否 | 模板默认值 | 无 | 否 | 平台 / 开发者 | 否 | web fetch 能力开关。 |
| `OPENCLAW_WEB_SEARCH_PROVIDER` | OpenClaw | 否 | 模板默认值 | 无 | 否 | 平台 / 开发者 | 否 | web search provider。 |
| `OPENCLAW_WEB_SEARCH_BASE_URL` | OpenClaw | 条件必传 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | web search endpoint。 |
| `OPENCLAW_WEB_SEARCH_MODEL` | OpenClaw | 条件必传 | 未设置 | 无 | 否 | 平台 / 开发者 | 否 | web search 模型名。 |
| `OPENCLAW_WEB_SEARCH_API_KEY` | OpenClaw | 条件必传 | 未设置 | 无 | 是 | Secret | 否 | web search API key。 |
| `OPENCLAW_WEB_SEARCH_API_KEY_SECRET_SOURCE` | OpenClaw | 否 | 模板默认值 | 无 | 否 | 平台 | 否 | web search key secret 来源。 |
| `OPENCLAW_WEB_SEARCH_API_KEY_SECRET_PROVIDER` | OpenClaw | 否 | 模板默认值 | 无 | 否 | 平台 | 否 | web search key secret provider 标识。 |
| `OPENCLAW_WEB_SEARCH_API_KEY_SECRET_ID` | OpenClaw | 条件必传 | 未设置 | 无 | 是 | Secret 配置 | 否 | web search key 引用 ID。 |
| `OPENCLAW_TAVILY_API_KEY` | OpenClaw web search | 条件必传 | 未设置 | `TAVILY_API_KEY` | 是 | Secret | 否 | OpenClaw Tavily 搜索 key。 |
| `OPENCLAW_WEB_SAFE_SEARCH_MODE` | OpenClaw safe exec | 否 | `bing` | 无 | 否 | 平台 / 开发者 | 否 | safe web search 模式，支持默认 Bing RSS 或模型搜索。 |
| `OPENCLAW_WEB_SAFE_SEARCH_MODEL` | OpenClaw safe exec | 条件必传 | 未设置 | `OPENCLAW_WEB_SEARCH_MODEL` / 默认模型 | 否 | 平台 / 开发者 | 否 | safe web search 模型名覆盖。 |
| `OPENCLAW_WEB_SAFE_SEARCH_BASE_URL` | OpenClaw safe exec | 条件必传 | 未设置 | `OPENCLAW_WEB_SEARCH_BASE_URL` / 模型 base url | 否 | 平台 / 开发者 | 否 | safe web search 模型 endpoint。 |
| `OPENCLAW_WEB_SAFE_SEARCH_API` | OpenClaw safe exec | 否 | `OPENCLAW_MODEL_API` 或 `openai-completions` | 无 | 否 | 平台 / 开发者 | 否 | safe web search 模型 API 类型。 |
| `OPENCLAW_WEB_SAFE_SEARCH_API_KEY` | OpenClaw safe exec | 条件必传 | 模型 key fallback | 无 | 是 | Secret | 否 | safe web search 专用 API key。 |
| `OPENCLAW_WEB_SAFE_SEARCH_SECRET_SOURCE` | OpenClaw safe exec | 否 | `OPENCLAW_MODEL_API_KEY_SECRET_SOURCE` | 无 | 否 | 平台 | 否 | safe web search key secret 来源。 |
| `OPENCLAW_WEB_SAFE_SEARCH_SECRET_FILE_PATH` | OpenClaw safe exec | 条件必传 | 未设置 | 无 | 是 | Secret 挂载 | 否 | safe web search file secret 路径。 |
| `OPENCLAW_WEB_SAFE_SEARCH_SECRET_ID` | OpenClaw safe exec | 条件必传 | 未设置 | 无 | 是 | Secret 配置 | 否 | safe web search key 引用 ID。 |
| `OPENCLAW_WEB_SAFE_SEARCH_ENDPOINT` | OpenClaw safe exec | 否 | `https://cn.bing.com/search?format=rss&q={query}` | 无 | 否 | 平台 / 开发者 | 否 | safe web search HTTP endpoint。 |
| `OPENCLAW_WEB_SAFE_READER_ENDPOINT` | OpenClaw safe exec | 否 | `https://r.jina.ai/` | 无 | 否 | 平台 / 开发者 | 否 | safe web reader endpoint。 |
| `OPENCLAW_WEB_SAFE_UNRESTRICTED` | OpenClaw safe exec | 否 | `false` | `OPENCLAW_EXEC_UNSAFE_MODE` 派生 | 否 | 开发者 / 测试 | 否 | 放宽 safe web SSRF 限制，生产不要开启。 |
| `OPENCLAW_WORKSPACE_FILES_ENABLED` | OpenClaw | 否 | 模板默认值 | `KSADK_WORKSPACE_FILES_ENABLED` | 否 | Runtime 镜像 / 平台 | 否 | workspace files 服务开关。 |
| `OPENCLAW_WORKSPACE_DIR` | OpenClaw | 否 | 模板默认值 | `KSADK_WORKSPACE_ROOT` | 否 | Runtime 镜像 / 平台 | 否 | workspace 目录。 |
| `OPENCLAW_WORKSPACE_FILES_PORT` | OpenClaw workspace files | 否 | 模板默认值 | 无 | 否 | Runtime 镜像 / 平台 | 否 | workspace files 服务端口。 |
| `OPENCLAW_WORKSPACE_FILES_PROXY_URL` | OpenClaw workspace files | 否 | 未设置 | 无 | 否 | Runtime 镜像 / 平台 | 否 | workspace files 代理地址。 |
| `OPENCLAW_PRESET_SKILLS_DIR` | OpenClaw bootstrap | 否 | `/opt/openclaw/preset-skills` | 无 | 否 | Runtime 镜像 / 平台 | 否 | 预置 skills 目录。 |
| `OPENCLAW_DEFAULT_EXTENSIONS_DIR` | OpenClaw bootstrap | 否 | `/opt/openclaw/default-extensions` | 无 | 否 | Runtime 镜像 / 平台 | 否 | 默认 extensions 目录。 |
| `OPENCLAW_GATEWAY_INTERNAL_HOST` | OpenClaw runtime proxy | 否 | `127.0.0.1` | 无 | 否 | Runtime 镜像 / 平台 | 否 | runtime proxy 连接内部 gateway 的 host。 |
| `OPENCLAW_GATEWAY_INTERNAL_PORT` | OpenClaw runtime proxy | 否 | `18080` | 无 | 否 | Runtime 镜像 / 平台 | 否 | runtime proxy 连接内部 gateway 的端口。 |
| `OPENCLAW_GATEWAY_PROXY_BASE_URL` | OpenClaw runtime proxy | 否 | 自动生成 | 无 | 否 | Runtime 镜像 / 平台 | 否 | runtime proxy HTTP base url 覆盖。 |
| `OPENCLAW_GATEWAY_PROXY_WS_URL` | OpenClaw runtime proxy | 否 | 自动生成 | 无 | 否 | Runtime 镜像 / 平台 | 否 | runtime proxy WebSocket url 覆盖。 |
| `OPENCLAW_GATEWAY_HANDOFF_GRACE_SECONDS` | OpenClaw gateway supervisor | 否 | `5` | 无 | 否 | Runtime 镜像 / 平台 | 否 | gateway handoff 等待秒数。 |
| `OPENCLAW_GATEWAY_LOCAL_RESTART_MAX` | OpenClaw gateway supervisor | 否 | `3` | 无 | 否 | Runtime 镜像 / 平台 | 否 | gateway 本地重启最大次数。 |
| `OPENCLAW_GATEWAY_LOCAL_RESTART_WINDOW_SECONDS` | OpenClaw gateway supervisor | 否 | `120` | 无 | 否 | Runtime 镜像 / 平台 | 否 | gateway 本地重启计数窗口。 |
| `OPENCLAW_GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS` | OpenClaw gateway supervisor | 否 | `1` | 无 | 否 | Runtime 镜像 / 平台 | 否 | gateway 本地重启退避秒数。 |
| `OPENCLAW_EXEC_STRICT_MODE` | OpenClaw | 否 | `false` | `OPENCLAW_EXEC_SAFE_MODE` | 否 | 平台 / 开发者 | 否 | 收紧 exec/fs 策略。 |
| `OPENCLAW_EXEC_HOST` | OpenClaw | 否 | `gateway` | 无 | 否 | 平台 / 开发者 | 否 | exec tool host 策略。 |
| `OPENCLAW_EXEC_SECURITY` | OpenClaw | 否 | `full` 或 profile 默认 | 无 | 否 | 平台 / 开发者 | 否 | exec 安全级别：`full/allowlist/deny` 等。 |
| `OPENCLAW_EXEC_ASK` | OpenClaw | 否 | `off` | 无 | 否 | 平台 / 开发者 | 否 | exec 询问策略。 |
| `OPENCLAW_EXEC_ASK_FALLBACK` | OpenClaw | 否 | profile 默认 | 无 | 否 | 平台 / 开发者 | 否 | 询问失败时的 fallback 策略。 |
| `OPENCLAW_EXEC_AUTO_ALLOW_SKILLS` | OpenClaw | 否 | `false` | 无 | 否 | 平台 / 开发者 | 否 | 是否自动允许预置 skill 调用 exec。 |
| `OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED` | OpenClaw | 否 | profile 默认 | 无 | 否 | 平台 / 开发者 | 否 | 是否启用默认 exec allowlist。 |
| `OPENCLAW_EXEC_ALLOWLIST` | OpenClaw | 否 | 未设置 | `OPENCLAW_EXEC_DEFAULT_ALLOWLIST` | 否 | 平台 / 开发者 | 否 | exec allowlist 覆盖。 |
| `OPENCLAW_FS_WORKSPACE_ONLY` | OpenClaw | 否 | profile 默认 | 无 | 否 | 平台 / 开发者 | 否 | 文件系统访问限制到 workspace。 |
| `OPENCLAW_ELEVATED_ENABLED` | OpenClaw | 否 | `false` | 无 | 否 | 平台 / 开发者 | 否 | elevated tool 开关。 |
| `OPENCLAW_PRESET_SKILLS_ALLOWLIST` | OpenClaw | 否 | 模板默认值 | 无 | 否 | Runtime 镜像 / 平台 | 否 | 预置 skills allowlist。 |
| `OPENCLAW_PRESET_PLUGINS_ALLOWLIST` | OpenClaw | 否 | 模板默认值 | 无 | 否 | Runtime 镜像 / 平台 | 否 | 预置 plugins allowlist。 |
| `OPENCLAW_RUNTIME_PROXY_ENABLED` | OpenClaw | 否 | 模板默认值 | 无 | 否 | Runtime 镜像 / 平台 | 否 | runtime proxy 开关。 |
| `OPENCLAW_RESPONSES_API_ENABLED` | OpenClaw | 否 | 模板默认值 | 无 | 否 | 平台 / 开发者 | 否 | Responses API 兼容入口开关。 |
| `OPENCLAW_THINKING_DEFAULT` | OpenClaw | 否 | `off` | 无 | 否 | 平台 / 开发者 | 否 | 默认 thinking effort。 |
| `OPENCLAW_VERBOSE_DEFAULT` | OpenClaw | 否 | `off` | 无 | 否 | 平台 / 开发者 | 否 | 默认 verbose 行为。 |
| `OPENCLAW_TYPING_MODE` | OpenClaw | 否 | `instant` | 无 | 否 | 平台 / 开发者 | 否 | UI typing 展示模式。 |
| `OPENCLAW_UI_LOCALE` | OpenClaw | 否 | 模板默认值 | `LANG`、`LC_ALL` | 否 | Runtime 镜像 / 开发者 | 否 | OpenClaw UI 语言。 |
| `OPENCLAW_CHANNEL_BOOTSTRAP_JSON` | OpenClaw | 否 | 未设置 | 无 | 是 | 平台 Secret / 部署配置 | 否 | channel 启动配置，可能包含登录/连接 token。 |
| `OPENCLAW_CONFIG_PATCH_JSON` | OpenClaw | 否 | 未设置 | 无 | 是 | 平台 Secret / 部署配置 | 否 | openclaw 配置 patch，可能包含 secret。 |
| `OPENCLAW_BOOTSTRAP_ONLY` | OpenClaw | 否 | `false` | 无 | 否 | Runtime 镜像 / 开发者 | 否 | 只执行 bootstrap，不启动 gateway。 |
| `OPENCLAW_STATE_DIR` | OpenClaw | 否 | `/home/node/.openclaw` | 无 | 否 | Runtime 镜像 | 否 | OpenClaw 状态目录。 |
| `OPENCLAW_TEMPLATE_DIR` | OpenClaw user template | 否 | `/opt/openclaw-template` | 无 | 否 | Runtime 镜像 | 否 | user template 根目录。 |
| `OPENCLAW_TEMPLATE_ENV_STRICT` | OpenClaw user template | 否 | `1` | 无 | 否 | Runtime 镜像 / 开发者 | 否 | user bootstrap 是否严格校验环境变量。 |
| `OPENCLAW_IMAGE` | OpenClaw CLI | 否 | CLI 内置镜像 | `OPENCLAW_DOCKER_IMAGE` | 否 | 开发者 / CI | 否 | OpenClaw 镜像覆盖。 |
| `OPENCLAW_RESOURCE` | OpenClaw CLI | 否 | CLI 默认规格 | 无 | 否 | 开发者 / 平台 | 否 | OpenClaw 资源规格快捷配置。 |
| `OPENCLAW_CPU` | OpenClaw CLI | 否 | CLI 默认规格 | 无 | 否 | 开发者 / 平台 | 否 | OpenClaw CPU 规格覆盖。 |
| `OPENCLAW_MEMORY` | OpenClaw CLI | 否 | CLI 默认规格 | 无 | 否 | 开发者 / 平台 | 否 | OpenClaw memory 规格覆盖。 |
| `OPENCLAW_RUNTIME_NPM_REGISTRY` | OpenClaw bootstrap | 否 | 镜像默认值 | 无 | 否 | Runtime 镜像 / 平台 | 否 | OpenClaw npm registry 覆盖。 |
| `OPENCLAW_RUNTIME_PIP_INDEX_URL` | OpenClaw bootstrap | 否 | 镜像默认值 | 无 | 否 | Runtime 镜像 / 平台 | 否 | OpenClaw pip index 覆盖。 |
| `OPENCLAW_RUNTIME_PIP_TRUSTED_HOST` | OpenClaw bootstrap | 否 | `mirrors.aliyun.com` | `PIP_TRUSTED_HOST` | 否 | Runtime 镜像 / 平台 | 否 | OpenClaw pip trusted-host 覆盖。 |
| `OPENCLAW_RUNTIME_UV_INDEX_URL` | OpenClaw bootstrap | 否 | `OPENCLAW_RUNTIME_PIP_INDEX_URL` | 无 | 否 | Runtime 镜像 / 平台 | 否 | OpenClaw uv index 覆盖。 |
| `OPENCLAW_RUNTIME_PLAYWRIGHT_DOWNLOAD_HOST` | OpenClaw bootstrap | 否 | `https://npmmirror.com/mirrors/playwright` | `PLAYWRIGHT_DOWNLOAD_HOST` | 否 | Runtime 镜像 / 平台 | 否 | Playwright 浏览器下载源覆盖。 |
| `OPENCLAW_RUNTIME_PUPPETEER_DOWNLOAD_BASE_URL` | OpenClaw bootstrap | 否 | `https://npmmirror.com/mirrors/chrome-for-testing` | `PUPPETEER_DOWNLOAD_BASE_URL` | 否 | Runtime 镜像 / 平台 | 否 | Puppeteer chrome-for-testing 下载源覆盖。 |
| `OPENCLAW_RUNTIME_PUPPETEER_DOWNLOAD_HOST` | OpenClaw bootstrap | 否 | `https://npmmirror.com/mirrors` | `PUPPETEER_DOWNLOAD_HOST` | 否 | Runtime 镜像 / 平台 | 否 | Puppeteer 下载 host 覆盖。 |
| `OPENCLAW_RUNTIME_CLAWHUB_SITE` | OpenClaw bootstrap | 否 | `https://cn.clawhub-mirror.com` | `CLAWHUB_SITE` | 否 | Runtime 镜像 / 平台 | 否 | ClawHub 站点地址覆盖。 |
| `OPENCLAW_RUNTIME_CLAWHUB_REGISTRY` | OpenClaw bootstrap | 否 | `CLAWHUB_SITE` 推导值 | `CLAWHUB_REGISTRY` | 否 | Runtime 镜像 / 平台 | 否 | ClawHub 插件仓库地址覆盖。 |
| `OPENCLAW_NPM_REGISTRY` | OpenClaw user template examples | 否 | `https://registry.npmmirror.com` | `OPENCLAW_RUNTIME_NPM_REGISTRY` | 否 | 镜像构建 / 开发者 | 否 | user template 示例中安装插件依赖的 npm registry。 |
| `PIP_TRUSTED_HOST` | Runtime image | 否 | 镜像或 bootstrap 设置 | `OPENCLAW_RUNTIME_PIP_TRUSTED_HOST` | 否 | Runtime 镜像 / 平台 | 否 | pip trusted-host。 |
| `PLAYWRIGHT_DOWNLOAD_HOST` | Runtime image | 否 | 镜像或 bootstrap 设置 | `OPENCLAW_RUNTIME_PLAYWRIGHT_DOWNLOAD_HOST` | 否 | Runtime 镜像 / 平台 | 否 | Playwright 浏览器下载源。 |
| `PUPPETEER_DOWNLOAD_BASE_URL` | Runtime image | 否 | 镜像或 bootstrap 设置 | `OPENCLAW_RUNTIME_PUPPETEER_DOWNLOAD_BASE_URL` | 否 | Runtime 镜像 / 平台 | 否 | Puppeteer 下载 base url。 |
| `PUPPETEER_DOWNLOAD_HOST` | Runtime image | 否 | 镜像或 bootstrap 设置 | `OPENCLAW_RUNTIME_PUPPETEER_DOWNLOAD_HOST` | 否 | Runtime 镜像 / 平台 | 否 | Puppeteer 下载 host。 |
| `KDOCS_TOKEN` | OpenClaw / Hermes kdocs skill | 条件必传 | 未设置 | 推荐迁移到 mcporter 配置 | 是 | 用户授权 / Secret | 否 | kdocs skill 运行态 token。Hermes 新流程优先 mcporter。 |
| `KDOCS_SKILL_REPO` | Hermes/OpenClaw image build | 否 | `https://github.com/kdocs-app/kdocs-skill.git` | 无 | 否 | 镜像构建 / 开发者 | 否 | 构建镜像时覆盖 kdocs skill 源仓库。 |
| `PLUGIN_API_KEY` | OpenClaw user template 示例 | 条件必传 | 未设置 | 无 | 是 | 业务扩展 Secret | 是 | user template 示例插件使用的业务 token，不属于 KsADK 标准契约。 |
| `DEMO_CHANNEL_API_KEY` | OpenClaw user template 示例 | 条件必传 | 未设置 | 无 | 是 | 业务扩展 Secret | 是 | user template 示例 channel 使用的业务 token，不属于 KsADK 标准契约。 |

## 13. 内部常量和表名

这些变量名由源码作为常量导出或用于内部表名/依赖集合，一般不需要用户配置。

| 变量 | 作用层级 | 是否必传 | 默认值 | 别名/兼容 | 敏感 | 配置方/来源 | 是否业务自定义 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `KSADK_ALLOWED_SUFFIXES` | builders | 否 | 代码常量 | 无 | 否 | SDK 内部 | 否 | 代码打包允许后缀集合。 |
| `KSADK_ATTACHMENT_RUNTIME_REQUIREMENTS` | builders | 否 | 代码常量 | 无 | 否 | SDK 内部 | 否 | 附件运行时内置依赖集合。 |
| `KSADK_ATTACHMENT_OCR_RUNTIME_REQUIREMENTS` | builders | 否 | 代码常量 | 无 | 否 | SDK 内部 | 否 | 附件 OCR 运行时内置依赖集合。 |
| `KSADK_BUILD_ENABLE_ATTACHMENT_OCR` | builders | 否 | `false` | 无 | 否 | 构建环境 / 开发者 | 否 | 是否把平台本地 OCR 依赖打进代码包。 |
| `KSADK_BUILD_ENABLE_MCP` | builders | 否 | `false` | 无 | 否 | 构建环境 / 开发者 | 否 | 强制加入 MCP adapter 构建依赖。 |
| `KSADK_BUILD_PIP_INSTALL_TIMEOUT_SECONDS` | builders | 否 | `2700` | 无 | 否 | 构建环境 / 开发者 | 否 | 源码构建时 pip install 的超时秒数。 |
| `KSADK_BUILD_ENABLE_POSTGRES_SESSION` | builders | 否 | `false` | 无 | 否 | 构建环境 / 开发者 | 否 | 强制加入 PostgreSQL session 构建依赖。 |
| `KSADK_CORE_RUNTIME_REQUIREMENTS` | builders | 否 | 代码常量 | 无 | 否 | SDK 内部 | 否 | 核心运行时内置依赖集合。 |
| `KSADK_MCP_RUNTIME_REQUIREMENTS` | builders | 否 | 代码常量 | 无 | 否 | SDK 内部 | 否 | MCP adapter 可选运行时内置依赖集合。 |
| `KSADK_POSTGRES_SESSION_REQUIREMENTS` | builders | 否 | 代码常量 | 无 | 否 | SDK 内部 | 否 | PostgreSQL session 可选运行时内置依赖集合。 |
| `KSADK_RUNTIME_REQUIREMENTS` | builders | 否 | 代码常量 | 无 | 否 | SDK 内部 | 否 | 完整运行时内置依赖集合。 |
| `KSADK_SKILL_SERVICE` | skills | 否 | 代码调用前缀 | 无 | 否 | SDK 内部 | 否 | Skill Service AICP 连接配置前缀，用于解析 `KSADK_SKILL_SERVICE_ENDPOINT` / `KSADK_SKILL_SERVICE_SCHEME` / `KSADK_SKILL_SERVICE_REGION`；一般不需要用户单独设置。 |
| `KSADK_EVENTS_TABLE` | sessions | 否 | `ksadk_events` | 无 | 否 | SDK 内部 | 否 | 本地 SQLite events 表名。 |
| `KSADK_SESSIONS_TABLE` | sessions | 否 | `ksadk_sessions` | 无 | 否 | SDK 内部 | 否 | 本地 SQLite sessions 表名。 |
| `KSADK_STATES_TABLE` | sessions | 否 | `ksadk_states` | 无 | 否 | SDK 内部 | 否 | 本地 SQLite states 表名。 |
| `KSADK_PG_EVENTS_TABLE` | sessions | 否 | `ksadk_events` | 无 | 否 | SDK 内部 | 否 | PostgreSQL events 表名。 |
| `KSADK_PG_SESSIONS_TABLE` | sessions | 否 | `ksadk_sessions` | 无 | 否 | SDK 内部 | 否 | PostgreSQL sessions 表名。 |
| `KSADK_PG_STATES_TABLE` | sessions | 否 | `ksadk_states` | 无 | 否 | SDK 内部 | 否 | PostgreSQL states 表名。 |
| `KSADK_UPDATED_AT` | configs | 否 | 写入部署环境时生成 | 无 | 否 | SDK 内部 | 否 | serverless 部署更新触发时间戳。 |
| `KSADK_VERSION` | configs | 否 | 代码常量 | 无 | 否 | SDK 内部 | 否 | SDK version 导出名。 |

## 14. 兼容、历史和不推荐变量

| 变量 | 状态 | 替代变量 | 说明 |
| --- | --- | --- | --- |
| `KSADK_ENABLE_SANDBOX_TOOLS` | master 旧 sandbox tools 开关，当前 Skill Runtime 重构后不再推荐 | `KSADK_SKILLS_MODE` + `KSADK_SKILL_RUNTIME_BACKEND` | master 分支仍存在。新实现不再默认注入 `execute_python/execute_bash/execute_javascript`。 |
| `KSADK_SANDBOX_TOOL_ID` | 早期 Skills 草案变量，不作为当前契约 | `KSADK_SANDBOX_TEMPLATE_ID` | 只保留在历史设计草案中。 |
| `KSADK_SANDBOX_HOST` | 早期/草案变量，不作为当前实现契约 | `E2B_API_URL` 或未来 provider endpoint | 当前通用 sandbox E2B backend 不读取。 |
| `KSADK_SANDBOX_REGION` | 早期/草案变量，不作为当前实现契约 | `KSADK_SANDBOX_TYPE` / provider 自身 region | 当前通用 sandbox E2B backend 不读取。 |
| `KSADK_SKILLS_DIR` | 早期/草案变量，不作为当前实现契约 | `KSADK_LOCAL_SKILLS_DIR` 或 `KSADK_SKILL_CACHE_DIR` | 当前 Runner/agent 不读取。 |
| `KSADK_SKILL_RUNTIME_ENDPOINT` | 早期/草案变量，不作为当前实现契约 | `E2B_API_URL` | E2B SDK 使用原生变量。 |
| `KSADK_SKILL_RUNTIME_API_KEY` | 早期/草案变量，不作为当前实现契约 | `E2B_API_KEY` | E2B SDK 使用原生变量。 |
| `KSADK_SKILL_RUNTIME_REGION` | 早期/草案变量，不作为当前实现契约 | 无 | 当前 E2B backend 不读取。 |
| `KSADK_SKILL_RUNTIME_TEMPLATE_ID` | 兼容变量 | `KSADK_SANDBOX_TEMPLATE_ID` | 仍可用，但新部署优先通用 sandbox 变量。 |
| `KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS` | 兼容变量 | `KSADK_SANDBOX_ALLOW_INTERNET_ACCESS` | 通用 sandbox 变量优先。 |
| `KSADK_STM_*` | 旧短期记忆变量 | `KSADK_SESSION_*` | 仍作为 fallback。 |
| `AGENTENGINE_SESSION_BACKEND` / `AGENTENGINE_TENANT_ID` / `AGENTENGINE_WORKSPACE_ID` | 平台兼容变量 | `KSADK_SESSION_BACKEND` / `KSADK_TENANT_ID` / `KSADK_WORKSPACE_ID` | 仍作为 fallback。 |
| `OPENAI_API_BASE` | OpenAI 旧变量 | `OPENAI_BASE_URL` | 仍作为兼容。 |
| `MODEL_NAME` | 旧模型名变量 | `OPENAI_MODEL_NAME` | 仍作为兼容。 |
| `MODEL_API_KEY` / `MODEL_API_BASE` | OpenClaw/模型兼容变量 | `OPENAI_API_KEY` / `OPENAI_BASE_URL` 或 OpenClaw 专用变量 | 按运行时模板选择。 |
| `LLM_API_KEY` / `LLM_API_BASE` / `LLM_MODEL` | Serverless/OpenClaw 兼容变量 | `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL_NAME` | 仍作为 fallback。 |
| `KINGSOFT_DOCS_TOKEN` | Hermes kdocs 旧变量，不推荐 | mcporter 内的 kdocs token | 只允许一次性迁移到 mcporter，不再建议写入环境变量或 `.env`。 |

## 15. 业务自定义变量边界

| 类型 | 是否业务自定义 | 是否写入本文 | 说明 |
| --- | --- | --- | --- |
| 业务代码读取的变量，例如 `APP_ENV`、`DATABASE_URL`、`REDIS_URL`、`MY_SERVICE_TOKEN` | 是 | 否 | 由业务方自己定义，KsADK 不做含义约束。 |
| Agent 依赖的第三方工具变量，例如某业务 API token | 是 | 否 | 可以通过部署环境注入，但不属于 KsADK 标准契约。 |
| SDK/镜像内置扩展读取的第三方 token，例如 `TAVILY_API_KEY`、`FIRECRAWL_API_KEY`、`MEM0_API_KEY` | 否 | 部分写入 | 只有被 KsADK runtime、Hermes/OpenClaw 模板或内置 skill 明确读取的变量才列入本文。 |
| 平台或 SDK 读取的变量，例如 `KSADK_*`、`KSYUN_*`、`E2B_*`、`OPENAI_*`、`LANGFUSE_*` | 否 | 是 | 本文维护常见和核心变量。 |
| 镜像模板内部变量，例如大量 `OPENCLAW_*` / `HERMES_*` 高级开关 | 否 | 部分写入 | 本文只列常见运行时可配置项，完整列表以对应模板 README/bootstrap 为准。 |

## 16. 配置建议

- 新部署优先使用通用变量：`KSADK_SANDBOX_TEMPLATE_ID`、`KSADK_SANDBOX_TIMEOUT`、`KSADK_SANDBOX_ALLOW_INTERNET_ACCESS`。
- Skill Runtime 兼容变量 `KSADK_SKILL_RUNTIME_TEMPLATE_ID` 仅用于迁移期。
- E2B backend 必须使用 SDK 原生 `E2B_API_URL` / `E2B_API_KEY`。
- Secret 不要写入代码、仓库文档、测试 fixture、日志、snapshot；使用 Secret 注入。
- 平台注入 Skill Space 时优先用 `KSADK_SKILL_SPACE_IDS`，单 space 兼容才使用 `SKILL_SPACE_ID`。
- `KSYUN_ACCESS_KEY` / `KSYUN_SECRET_KEY` 是多个服务的 fallback。生产 sandbox 中建议使用更窄权限的 `KSADK_SKILL_SERVICE_ACCESS_KEY` / `KSADK_SKILL_SERVICE_SECRET_KEY`。
