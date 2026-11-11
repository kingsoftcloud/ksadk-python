# Google ADK 多版本兼容矩阵

> 更新日期：2026-11-11。保留 2026-07-21 对 **1.34.3** 与 **2.5.0** 的历史探测，
> 0.8.7 候选另验证当前默认解析的 **2.14.0** 及其实际依赖组合。
> 唯一收口点:`ksadk/compat/adk_compat.py`。业务代码不直接 `import google.adk`。

## 版本窗口

依赖约束:**`google-adk>=1.34.0,<3.0.0`**

| 边界 | 取值 | 理由 |
|---|---|---|
| 最低验证锚点 | **1.34.3** | 依赖下限保持 1.34.0；不承诺整个 1.x 系列。1.0.0 缺少当前运行时所需的 App、恢复和事件能力。 |
| 上界 | **<3.0.0** | 保留既有兼容窗口；验证点包括 2.5.0 与 2.14.0，其他依赖组合仍须通过实际导入和测试。 |

## 0.8.7 当前默认安装组合

开发 `uv.lock` 继续固定 Google ADK 2.5.0。直接从候选 wheel 安装会按包约束重新解析依赖；
2026-11-11 的实际默认组合包含 Google ADK 2.14.0、Pydantic 2.14.3、
LangChain Core 1.7.7、OpenAI 3.49.0、SQLAlchemy 2.1.8 与 asyncpg 0.32.0。
因此安装成功、锁文件测试以及默认解析测试分别保存证据，不能互相替代。

候选验证覆盖实际默认组合的 CLI、ADKRunner、会话导入、常规测试，以及 PostgreSQL 9.6/16
的真实持久化分组。`Event.node_info` 和 `NodeInfo.path` 等事件字段由同一兼容层处理，
不通过删去原生事件、恢复或工具调用身份来适配版本。

## 历史符号探测：1.34.3 与 2.5.0

ksadk 运行时 import 的**全部符号**在 1.34.3 与 2.5.0 **均存在**。逐符号探测结果(0 缺失):

| 符号 | 来源模块 | 1.34.3 | 2.5.0 |
|---|---|---|---|
| `Agent` | `google.adk.agents` | ✅ | ✅ |
| `RunConfig` / `StreamingMode` | `google.adk.agents.run_config` | ✅ | ✅ |
| `App` / `ResumabilityConfig` | `google.adk.apps` | ✅ | ✅ |
| `Event` | `google.adk.events.event` | ✅ | ✅ |
| `BaseMemoryService` / `SearchMemoryResponse` | `google.adk.memory.base_memory_service` | ✅ | ✅ |
| `MemoryEntry` | `google.adk.memory.memory_entry` | ✅ | ✅ |
| `LlmResponse` | `google.adk.models` | ✅ | ✅ |
| `LiteLlm` | `google.adk.models.lite_llm` | ✅ | ✅(需 `litellm`) |
| `Runner` | `google.adk.runners` | ✅ | ✅ |
| `BaseSessionService` / `InMemorySessionService` / `Session` / `DatabaseSessionService` | `google.adk.sessions` | ✅ | ✅(DB 需 `sqlalchemy`) |
| `GetSessionConfig` / `ListSessionsResponse` | `google.adk.sessions.base_session_service` | ✅ | ✅ |
| `FunctionTool` / `load_memory` | `google.adk.tools` | ✅ | ✅ |
| `McpTool` | `google.adk.tools.mcp_tool.mcp_tool` | ✅ | ✅(需 `mcp`) |
| `McpToolset` | `google.adk.tools.mcp_tool.mcp_toolset` | ✅ | ✅(需 `mcp`) |
| `CheckableMcpHttpClientFactory` / `StreamableHTTPConnectionParams` | `google.adk.tools.mcp_tool.mcp_session_manager` | ✅ | ✅(需 `mcp`) |
| `types` | `google.genai.types` | ✅ | ✅ |

> 注:`LiteLlm` / MCP / `DatabaseSessionService` 在干净环境首次探测报"缺失",实为缺**可选依赖**(`litellm` / `mcp` / `sqlalchemy`),补齐后即存在——不是 API 移除。这些可选依赖由 ksadk 的 `adk` extra 及主依赖提供。

## 签名差异(均为 2.x 向后兼容可选新增)

| 项 | 1.34.3 | 2.5.0 | 对 ksadk 影响 |
|---|---|---|---|
| `Runner.run_async` | 无 `yield_user_message` | 新增可选 `yield_user_message=False` | 无。ksadk 不传该参,两版调用一致。 |
| `RunConfig` | 基础字段 | 新增可选 `http_options`/`telemetry`/`model_input_context` 等 | 无。均为可选,不传不影响。 |
| `App.root_agent` | 必填 | 变为可选(`=None`) | 无。ksadk 始终显式传 `root_agent`。 |
| `DatabaseSessionService` | `db_url` 必填 | `db_url` 可选 + 新增可选 `db_engine` | 无。ksadk 始终传 `db_url`。 |
| `McpToolset.header_provider` | 同步 callable | 支持 awaitable | 无。ksadk 未用该参数。 |

该轮探测中，SDK 所用调用签名在两个锚点均可用；新版本新增的字段仍通过兼容层和回归测试核对，
不能仅凭主版本相同推断全部调用行为一致。

## 行为差异(非 API 签名,已吸收统一)

| 项 | 1.34.x | 2.x | 处理 |
|---|---|---|---|
| Agent 名称非法的报错措辞 | 构造后不立即校验,ksadk 后续给出友好提示;错误消息含 `...valid identifier` | 构造时即用 pydantic 校验并抛 `ValidationError`,消息为 `...valid Python identifier`(多了 "Python") | `ADKRunner._invalid_agent_name_load_error` 的匹配器由 `"valid identifier"` 放宽为 `"valid"`+`"identifier"` 双关键字,两版都转化为同一条 actionable hint(如 `agent_0707agent_adk`)。**行为已统一,无降级。** |

## 依赖解析:opentelemetry 窗口取两版交集,默认解析到 adk 2.x

`google-adk` 各版本对 otel 的要求:

- adk **1.34.x**:`opentelemetry-api/sdk >=1.36,<=1.41.1`、`starlette>=0.49.1,<1`。
- adk **2.5.x**:`opentelemetry-api/sdk >=1.39,<=1.42.1`、`starlette>=1.3.1,<2`。

otel 区间存在交集 **[1.39, 1.41.1]**(两版都满足);starlette 区间互斥(0.x vs 1.x,
但两版 adk 不需要同装,故不冲突)。

据此 ksadk `pyproject.toml` 的 otel 约束由 `==1.37.0` 放宽为 `>=1.39.0,<=1.41.1`。效果：

- 开发锁文件固定 **google-adk 2.5.0**（otel 1.41.1、starlette 1.3.1、google-genai 2.12.1）；
  该锁文件不约束普通 wheel 安装，当前默认解析见上文。
- adk 1.34.x 仍可装(CI matrix 用 `uv pip install google-adk==1.34.3` 覆盖验证,otel 1.41.1
  落在 1.34.x 允许的 `<=1.41.1` 内,starlette 由 uv 降到 0.x)。
- `fastapi<1.0.0` 与 starlette 1.3.1 兼容(fastapi 0.136.x 要求 `starlette>=0.46.0` 无上界),无冲突。

> 说明:otel 由 1.37.0 升到 1.41.1,ksadk tracing 全量测试在 otel 1.41.1 下绿,未发现回归。
> 若平台其他组件对 otel 有更窄要求,需在各自约束里单独声明。

## 使用 2.x-only 能力的纪律

未来若要用某个 2.x 才有的可选参数/符号,**必须**在兼容层内用
`adk_compat.adk_version_at_least("2.x.0")` 判断后再下发,并对 1.34 做显式降级;
不允许业务代码自行 `import google.adk` 探测版本。

## 运行时 import 站点(已全部收口到 `ksadk/compat/adk_compat.py`)

| 文件 | 用法 |
|---|---|
| `ksadk/mcp_runtime/__init__.py` | MCP toolset / 连接参数(模块级) |
| `ksadk/memory/adk/resilient_session_service.py` | Session/Event(模块级) |
| `ksadk/memory/adk/short_term_memory.py` | Session 后端(模块级 + 函数内) |
| `ksadk/memory/adk/long_term_memory.py` | Memory/Event/genai types(模块级) |
| `ksadk/memory/adk_tool.py` | `FunctionTool`(函数内,可降级为原函数) |
| `ksadk/knowledge_base/adk_tool.py` | `FunctionTool`(函数内,可降级为原函数) |
| `ksadk/runners/adk_runner.py` | Runner/App/LiteLlm/RunConfig 等(函数内,10 处) |

## 不受版本 pin 影响的字符串引用(非运行时 import,故未收口)

这些是**对用户源码的字符串匹配 / 代码生成模板 / 子进程调用 / 文案**,与 ksadk 进程内
google.adk 版本无关,刻意保留:

- `ksadk/detection/detector.py`:检测用户代码里是否出现 `google.adk` 字符串(框架识别)。
- `ksadk/cli/cmd_create.py`:`TEMPLATES` 内生成的**用户项目**代码(`from google.adk...`),
  属用户代码;另有 `'google.adk': 'google-adk'` 的包名映射。
- `ksadk/cli/cmd_run.py`:`python -m google.adk.cli` 子进程调用 ADK 原生 CLI。
- 各处日志/错误文案与 docstring。

## CI 保障

`ci.yml` 保留 **1.34.3**、**2.5.0** 两个锚点，并增加当前默认解析的 **2.14.0**。
常规测试与真实进程测试使用相同的三版本矩阵，真实进程测试在独立 job 中执行。
默认解析分组在前端和制品构建后升级实际依赖，再固定并断言该分组的 ADK 版本；
后续命令使用 `--no-sync`，避免锁文件悄悄替换测试版本。浏览器按升级后的 Playwright 安装。
PostgreSQL Teams 测试使用固定的测试依赖 `pgserver==0.1.4`，不增加 SDK 的运行时依赖。
