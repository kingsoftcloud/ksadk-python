# AgentKit Studio OTLP Trace Explorer 重写实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重写 AgentKit Studio 的 Trace 采集、持久化、查询与详情界面，使本地和未来云端 Agent 共用 OTLP 数据合同，并只展示运行时真实上报的 Token 与耗时。

**Architecture:** RuntimeAdapter 继续输出统一 RuntimeEvent，但 TraceAdapter 将这些事件投影成标准 OTLP JSON，并持久化至 `.agentkit/traces`。Studio Server 对外提供 TraceSummary、无损 TraceView 和 Raw OTLP；前端可观测页成为独立 Trace Explorer，不再复用 Chat 路由或 Chat Inspector。

**Tech Stack:** Python 3.10+、FastAPI、Pydantic v2、OpenTelemetry/OTLP JSON、原生 HTML/CSS/JavaScript、pytest、Playwright/internal browser。

## Global Constraints

- 新 Trace ID 是 32 位小写十六进制，Span ID 是 16 位小写十六进制。
- OTLP JSON 的 canonical 层级为 `resourceSpans -> scopeSpans -> spans`。
- W3C `traceparent` 可由真实 Trace/Span ID 构造；Run ID 与 Trace ID 不混用。
- Token 与耗时没有上游实报值时显示“未上报”，禁止字符估算或把缺失值显示为 0。
- Prompt、Completion、命令输出和凭证不得默认进入 Trace attributes。
- 自定义字段使用 `agentkit.*`；已有标准含义优先使用 `gen_ai.*`、`service.*` 和 `deployment.*`。
- Trace Explorer 独立于 Chat；打开 Trace 不得改变 Agent、Session 或 Chat 路由。
- 维持现有浅色、克制的 Studio 视觉语言；签名元素是可缩放的 Span 时间瀑布，不引入装饰性渐变。

---

### Task 1: Codex 真实 Usage 与 Duration 贯通

**Files:**
- Modify: `ksadk/codex/client.py`
- Modify: `ksadk/codex/runtime.py`
- Modify: `ksadk/runners/codex_runner.py`
- Modify: `ksadk/runtime/runner_adapter.py`
- Modify: `ksadk/studio/runtime_orchestrator.py`
- Modify: `ksadk/studio/contracts.py`
- Test: `tests/runners/test_codex_runtime.py`
- Test: `tests/runners/test_codex_runner.py`
- Test: `tests/studio/test_runtime_orchestrator.py`

**Interfaces:**
- Consumes: Codex SDK notifications `thread/tokenUsage/updated`、`turn/started`、`turn/completed`。
- Produces: `RuntimeEvent(EventType.USAGE_REPORTED)`，以及保留原生 timing 的 `RUN_PROGRESS`；`RunRecord.usage.reported/source` 和 `RunRecord.duration_source`。

- [ ] **Step 1: 写失败测试，证明 Codex 通知未被过滤**

  构造真实形状的 notification fixture，断言 client 输出上述三种 method，CodexRuntime 将 `tokenUsage.last` 转成精确 usage，并将 `turn.durationMs` 保留在事件中。

- [ ] **Step 2: 运行定向测试并确认因通知被过滤而失败**

  Run: `uv run pytest tests/runners/test_codex_runtime.py tests/runners/test_codex_runner.py tests/studio/test_runtime_orchestrator.py -q`

- [ ] **Step 3: 最小实现通知映射和指标优先级**

  `Usage` 新增 `reported`、`source`、`cached_input_tokens`、`reasoning_output_tokens`；Studio 最终记录优先使用 Runtime 上报 duration，否则才使用本地 wall clock，并明确 `durationSource=runtime|studio`。

- [ ] **Step 4: 运行定向测试并确认通过**

  Run: `uv run pytest tests/runners/test_codex_runtime.py tests/runners/test_codex_runner.py tests/studio/test_runtime_orchestrator.py -q`

### Task 2: OTLP Trace Store 与查询合同

**Files:**
- Create: `ksadk/studio/otel_trace.py`
- Modify: `ksadk/studio/event_store.py`
- Modify: `ksadk/studio/api.py`
- Modify: `ksadk/model_proxy/server.py`
- Test: `tests/studio/test_otel_trace.py`
- Test: `tests/studio/test_codex_api.py`
- Test: `tests/model_proxy/test_server.py`

**Interfaces:**
- Consumes: `RunRecord`、`RunEvent[]` 以及 proxy 的真实 completion usage/duration。
- Produces: `OtlpTraceStore.sync(record, events)`、`get_trace_view(trace_id)`、`get_otlp(trace_id)`、`list_trace_summaries(...)`；API `GET /api/v1/traces`、`GET /api/v1/traces/{traceId}`、`GET /api/v1/traces/{traceId}/otlp`。

- [ ] **Step 1: 写失败测试定义 OTLP JSON 与安全边界**

  固定 fixture 的时间、Usage、模型和 Tool 事件，手工断言 `resourceSpans/scopeSpans/spans`、父子关系、OTel ID 长度、精确 usage、精确 duration；同时断言 prompt、completion、命令正文与 secret 不出现在 Raw OTLP。

- [ ] **Step 2: 运行测试并确认缺少 OtlpTraceStore**

  Run: `uv run pytest tests/studio/test_otel_trace.py -q`

- [ ] **Step 3: 实现 OTLP 投影、原子持久化和 TraceView**

  Root span 使用 `invoke_agent {agent}`；模型调用使用 `chat {model}` CLIENT span；Tool 使用 `execute_tool {name}` INTERNAL span；非结构化运行事件作为脱敏 span event。OTLP 64 位时间以十进制字符串编码。

- [ ] **Step 4: 增加列表、详情和 Raw OTLP API**

  TraceView 返回扁平 span 列表和真实聚合指标，同时保留 Raw OTLP 独立接口。旧 `run + events` Trace 响应不再是详情页合同。

- [ ] **Step 5: 让模型 proxy 在真实结束点上报 completion usage/duration**

  流式与非流式路径均发出 `proxy.completed`，只包含 response id、model、duration、status 和 provider usage，不包含 prompt/completion。

- [ ] **Step 6: 运行 API 与 proxy 回归**

  Run: `uv run pytest tests/studio/test_otel_trace.py tests/studio/test_codex_api.py tests/model_proxy/test_server.py -q`

### Task 3: 独立 Trace Explorer 界面

**Files:**
- Modify: `ksadk/studio/static/index.html`
- Modify: `ksadk/studio/static/app.js`
- Modify: `ksadk/studio/static/app.css`
- Test: `tests/studio/test_codex_static.py`
- Test: `tests/studio/test_style_system.py`

**Interfaces:**
- Consumes: Trace summary/list、TraceView、Raw OTLP API。
- Produces: `?view=observability&traceId=<otel-trace-id>` 深链接；Trace 列表、KPI、Span 树/瀑布、详情 tabs。

- [ ] **Step 1: 写失败的静态可访问性与路由测试**

  断言 Observability 有独立 Explorer 容器、Agent/状态筛选、span tree、detail tabs 和 Raw OTLP；断言 `data-open-trace` 不调用 `openChat`，路由保留 `view=observability&traceId=`。

- [ ] **Step 2: 运行静态测试并确认旧表格/跳转实现失败**

  Run: `uv run pytest tests/studio/test_codex_static.py tests/studio/test_style_system.py -q`

- [ ] **Step 3: 重写 Observability DOM 与状态管理**

  页面分为顶部筛选/KPI、左侧 Trace 列表、中间 Span 树和时间瀑布、右侧详情；Chat Inspector 只保留摘要和“打开完整 Trace”。

- [ ] **Step 4: 实现 span 选择、缩放和详情 tabs**

  瀑布按 root duration 计算相对位置；0ms、缺失结束时间和错误 Span 均有明确状态；Raw OTLP 使用可滚动 `<pre>`，长 attribute 不撑破布局。

- [ ] **Step 5: 完成响应式与键盘操作**

  1024px 以下右侧详情变为下方区域；Trace/Span 行可用键盘激活；focus-visible 清晰；遵循 reduced motion。

- [ ] **Step 6: 运行静态回归**

  Run: `uv run pytest tests/studio/test_codex_static.py tests/studio/test_style_system.py -q`

### Task 4: 本地浏览器端到端验收

**Files:**
- Modify: `tests/studio/e2e/fake_studio_server.py`
- Create: `tests/studio/e2e/test_trace_explorer_browser.py`
- Modify: `docs/agentkit-local-studio-phase1-delivery-design.md`

**Interfaces:**
- Consumes: 完整创建 Agent、Build、Run、Trace API 和 Studio UI。
- Produces: 可重复的本地演示与浏览器验收证据。

- [ ] **Step 1: 让浏览器 fixture 上报固定精确 usage/duration**

  Fixture 固定为 input=128、output=32、cached=16、reasoning=8、duration=1340ms，并包含一个 210ms Tool span。

- [ ] **Step 2: 写浏览器测试**

  创建/构建/运行 Agent，进入可观测页，打开 Trace，断言 URL 未切到 Chat、KPI 显示精确值、瀑布存在、Tool span 可选、Raw OTLP 包含 canonical 层级。

- [ ] **Step 3: 运行完整相关测试**

  Run: `uv run pytest tests/studio/test_otel_trace.py tests/studio/test_codex_api.py tests/studio/test_codex_static.py tests/studio/test_style_system.py tests/studio/test_runtime_orchestrator.py tests/runners/test_codex_runtime.py tests/runners/test_codex_runner.py tests/model_proxy/test_server.py -q`

- [ ] **Step 4: 使用内部浏览器完成真实交互验收**

  验证筛选、Trace 深链接、Span 展开、Attributes/Events/Resource/Raw OTLP、长内容滚动、刷新后恢复以及返回 Chat 不丢当前 Agent/Session。

- [ ] **Step 5: 更新中文设计文档并记录兼容边界**

  明确 RuntimeEvent 是实时交互合同，OTLP 是 Trace 事实源，TraceView 是无损查询模型；云端 Trace Provider 只需实现同一 Query 接口。
