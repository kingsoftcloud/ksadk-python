# KSADK 环境变量参考

本文档覆盖 `ksadk/` 源码中出现的 `KSADK_*` 名称。部分条目是源码内部常量或导出名，不是运行时可配置的环境变量，默认值列为 `代码常量`。

| 变量 | 模块 | 默认值 | 敏感 | 用途 |
| --- | --- | --- | --- | --- |
| `KSADK_ADK_SESSION_BACKEND` | sessions | 未设置 | 否 | ADK 专用 session backend 选择器。 |
| `KSADK_ADK_SESSION_PATH` | sessions | 未设置 | 否 | ADK 专用 SQLite session 数据库路径。 |
| `KSADK_ADK_SESSION_URL` | sessions | 未设置 | 是 | ADK 专用数据库 session URL。 |
| `KSADK_ALLOWED_SUFFIXES` | builders | 代码常量 | 否 | 代码打包允许的文件后缀集合。 |
| `KSADK_ATTACHMENT_RUNTIME_REQUIREMENTS` | builders | 代码常量 | 否 | 附件运行时内置依赖集合。 |
| `KSADK_CORE_RUNTIME_REQUIREMENTS` | builders | 代码常量 | 否 | 核心运行时内置依赖集合。 |
| `KSADK_ENABLE_MCP_TOOLS` | mcp_runtime | `1` | 否 | 控制 ADK Runner 是否注入 MCP tools。 |
| `KSADK_ENABLE_SANDBOX_TOOLS` | sandbox | `1` | 否 | 控制 ADK Runner 是否注入默认 sandbox tools。 |
| `KSADK_EVENTS_TABLE` | sessions | 代码常量 | 否 | 本地 SQLite session events 表名。 |
| `KSADK_FEISHU_APP_ID` | cli | 未设置 | 否 | OpenClaw 诊断中飞书辅助脚本使用的 app id。 |
| `KSADK_FEISHU_RESULT_PATH` | cli | 未设置 | 否 | OpenClaw 诊断中飞书辅助脚本写入结果的路径。 |
| `KSADK_KB` | knowledge_base | 未设置 | 否 | AICP knowledge-base 连接配置前缀。 |
| `KSADK_KB_ACCESS_KEY` | knowledge_base | `KSYUN_ACCESS_KEY` | 是 | Knowledge-base API access key。 |
| `KSADK_KB_DATASET_ID` | knowledge_base | 未设置 | 否 | Knowledge-base dataset id，存在时启用知识库。 |
| `KSADK_KB_ENDPOINT` | knowledge_base | `aicp.api.ksyun.com` | 否 | Knowledge-base API endpoint。 |
| `KSADK_KB_REGION` | knowledge_base | `cn-beijing-6` | 否 | Knowledge-base region。 |
| `KSADK_KB_RERANKING_ENABLE` | knowledge_base | `false` | 否 | 是否启用 knowledge-base reranking。 |
| `KSADK_KB_SCORE_THRESHOLD` | knowledge_base | `0.0` | 否 | Knowledge-base 检索分数阈值。 |
| `KSADK_KB_SEARCH_METHOD` | knowledge_base | `intelligence_search` | 否 | Knowledge-base 检索方法。 |
| `KSADK_KB_SECRET_KEY` | knowledge_base | `KSYUN_SECRET_KEY` | 是 | Knowledge-base API secret key。 |
| `KSADK_KB_TOP_K` | knowledge_base | `5` | 否 | Knowledge-base 返回结果数量。 |
| `KSADK_LTM` | memory | 未设置 | 否 | AICP long-term-memory 连接配置前缀。 |
| `KSADK_LTM_ACCESS_KEY` | memory | `KSYUN_ACCESS_KEY` | 是 | Long-term-memory API access key。 |
| `KSADK_LTM_AGENT_ID` | memory | 未设置 | 否 | Long-term-memory agent id。 |
| `KSADK_LTM_APP_NAME` | memory | 未设置 | 否 | Long-term-memory application name 覆盖值。 |
| `KSADK_LTM_BACKEND` | memory | `local` | 否 | Long-term-memory backend 选择器。 |
| `KSADK_LTM_ENDPOINT` | memory | 未设置 | 否 | Long-term-memory API endpoint。 |
| `KSADK_LTM_HTTP_TOKEN` | memory | 未设置 | 是 | HTTP long-term-memory 服务认证 token。 |
| `KSADK_LTM_HTTP_URL` | memory | 未设置 | 是 | HTTP long-term-memory 服务 URL。 |
| `KSADK_LTM_INDEX` | memory | 未设置 | 否 | Long-term-memory index 名称。 |
| `KSADK_LTM_NAMESPACE` | memory | 未设置 | 否 | Long-term-memory namespace。 |
| `KSADK_LTM_REGION` | memory | `cn-beijing-6` | 否 | Long-term-memory region。 |
| `KSADK_LTM_SCENE_ID` | memory | 未设置 | 否 | Long-term-memory scene id。 |
| `KSADK_LTM_SCHEME` | memory | `https` | 否 | Long-term-memory API scheme。 |
| `KSADK_LTM_SECRET_KEY` | memory | `KSYUN_SECRET_KEY` | 是 | Long-term-memory API secret key。 |
| `KSADK_LTM_TOP_K` | memory | `5` | 否 | Long-term-memory 检索结果数量。 |
| `KSADK_MCP_SERVERS` | mcp_runtime | 未设置 | 是 | MCP server JSON 数组配置，可能包含 token、URL 等连接信息。 |
| `KSADK_MEMORY_BACKEND` | memory | `memory` | 否 | 通用 memory backend 选择器。 |
| `KSADK_MEMORY_PREFIX` | memory | `ksadk:memory:` | 否 | 通用 memory key 前缀。 |
| `KSADK_MEMORY_TTL` | memory | 未设置 | 否 | 通用 memory 默认 TTL 秒数。 |
| `KSADK_MEMORY_URL` | memory | 未设置 | 是 | 通用 memory backend URL。 |
| `KSADK_PG_EVENTS_TABLE` | sessions | 代码常量 | 否 | PostgreSQL session events 表名。 |
| `KSADK_PG_SESSIONS_TABLE` | sessions | 代码常量 | 否 | PostgreSQL sessions 表名。 |
| `KSADK_PG_STATES_TABLE` | sessions | 代码常量 | 否 | PostgreSQL session states 表名。 |
| `KSADK_PROJECT_DIR` | sessions | 当前工作目录 | 否 | 本地 session/workspace 状态使用的 project root。 |
| `KSADK_RESPONSES_SESSION_HEADER` | runners | 未设置 | 否 | RemoteRunner 透传 Responses session 的 header 名称。 |
| `KSADK_RUNTIME_PORT` | cli | `8080` | 否 | 模板运行时导出的 HTTP 端口。 |
| `KSADK_RUNTIME_REQUIREMENTS` | builders | 代码常量 | 否 | 完整运行时内置依赖集合。 |
| `KSADK_SESSIONS_TABLE` | sessions | 代码常量 | 否 | 本地 SQLite sessions 表名。 |
| `KSADK_SESSION_BACKEND` | sessions | `local` | 否 | 会话持久化 backend 选择器。 |
| `KSADK_SESSION_DSN` | sessions | 未设置 | 是 | 会话持久化数据库 DSN。 |
| `KSADK_SESSION_NAMESPACE` | sessions | 未设置 | 否 | 会话 namespace。 |
| `KSADK_SESSION_PATH` | sessions | 未设置 | 否 | 会话本地 SQLite 数据库路径。 |
| `KSADK_STATES_TABLE` | sessions | 代码常量 | 否 | 本地 SQLite session states 表名。 |
| `KSADK_STM_BACKEND` | sessions | 未设置 | 否 | Short-term-memory session backend 选择器。 |
| `KSADK_STM_DB_PATH` | sessions | 未设置 | 否 | 旧版 short-term-memory SQLite 路径。 |
| `KSADK_STM_DB_URL` | sessions | 未设置 | 是 | 旧版 short-term-memory 数据库 URL。 |
| `KSADK_STM_PATH` | sessions | 未设置 | 否 | Short-term-memory SQLite 路径。 |
| `KSADK_STM_URL` | sessions | 未设置 | 是 | Short-term-memory 数据库 URL。 |
| `KSADK_TENANT_ID` | sessions | 未设置 | 否 | session namespace 作用域中的 tenant id。 |
| `KSADK_UPDATED_AT` | configs | 写入部署环境时生成 | 否 | serverless 部署更新触发时间戳。 |
| `KSADK_VERSION` | configs | 代码常量 | 否 | SDK version 导出名。 |
| `KSADK_WORKSPACE_ID` | sessions | 未设置 | 否 | session namespace 作用域中的 workspace id。 |
