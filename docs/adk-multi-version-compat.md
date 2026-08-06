# Google ADK 多版本兼容矩阵(goal-00)

> 生成日期:2026-07-21。实测解包对比 `google-adk` **1.34.3** 与 **2.5.0**(当前最新 2.x)。
> 唯一收口点:`ksadk/compat/adk_compat.py`。业务代码不直接 `import google.adk`。

## 版本窗口

依赖约束:**`google-adk>=1.34.0,<3.0.0`**

| 边界 | 取值 | 理由 |
|---|---|---|
| 最低锚点 | **1.34.x** | 不承诺"1.x 全系"。实测 1.0.0 缺 `google.adk.apps`、缺多项 resumability/EventActions 能力,支持 1.0 代价过大;1.34 是当前事实最低可用线。 |
| 上界 | **<3.0.0**(含 2.5.x) | 2.x 为当前主线;实测 ksadk 用到的符号在 2.x 全部保留。 |

## 探测结论:无核心类移除,仅 2.x 向后兼容新增

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

**能力对等结论:ksadk 现有调用不依赖任何 2.x-only 参数,1.34 与 2.x 对 ksadk 能力完全对等,无需降级 shim。**

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

据此 ksadk `pyproject.toml` 的 otel 钉由 `==1.37.0` **放宽为 `>=1.39.0,<=1.41.1`**(goal-00,
经拍板"想办法支持 2.x")。效果:

- `uv lock` 默认解析到 **google-adk 2.5.0**(otel 1.41.1、starlette 1.3.1、google-genai 2.12.1),
  即 2.x 成为默认安装;实测全量测试绿。
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

`ci.yml` 增加 google-adk matrix:**1.34.x** 与 **2.5.x** 各跑一遍全量测试,防止任何一端回归。
