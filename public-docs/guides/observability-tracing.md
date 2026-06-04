# 可观测与链路追踪

KsADK 提供本地 spans、Langfuse 兼容路径和标准 OTLP HTTP traces exporter。
tracing 是可选诊断能力，不应成为 quickstart 的前置条件。

从 `0.6.2` 开始，推荐把业务代码写成 **OTel-first**：应用只创建
OpenTelemetry spans、span events 和 attributes，后端通过
`OTEL_EXPORTER_OTLP_*` 环境变量路由到 Langfuse、OTel Collector 或其他兼容后端。

## 导出路径

| 路径 | 默认行为 | 用途 |
| --- | --- | --- |
| In-memory spans | 本地 runner 路径默认启用 | 本地 debug API 和 Web UI trace 视图 |
| Generic OTLP HTTP | 设置 `OTEL_EXPORTER_OTLP_*` 后启用 | 发送到 Langfuse 或任意 OTLP HTTP Collector |
| Langfuse OTLP HTTP | 没有 generic OTLP 且设置 Langfuse key 时自动启用 | 兼容旧 Langfuse 环境变量 |
| OTLP gRPC | 通过 `enable_otlp=True` 显式启用 | 兼容旧代码路径 |
| OpenInference instrumentation | best effort | 为 ADK、LangChain 等框架补充框架级 spans |

本地 in-memory exporter 是最安全的公开默认值；远端 exporter 应通过环境变量启用。
如果外部 exporter 未配置或初始化失败，本地运行仍应继续。

## 通用 OTLP HTTP

只配置通用 endpoint 时，KsADK 会自动派生 `/v1/traces`：

```bash
export OTEL_SERVICE_NAME="customer-agent-runtime"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
export OTEL_EXPORTER_OTLP_ENDPOINT="https://otel-collector.example.com/otel"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20demo-token"

agentengine run .
```

也可以显式配置 traces 专用 endpoint、protocol 和 headers。`TRACES_*` 优先于通用配置：

```bash
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL="http/protobuf"
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://otel-collector.example.com/otel/v1/traces"
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Bearer%20trace-token"
```

headers 使用 OTLP 约定的逗号分隔格式，value 建议 URL encode。KsADK 会把
`Bearer%20trace-token` 解码为 `Bearer trace-token`。

## Langfuse 兼容

如果团队仍使用 Langfuse 环境变量，原配置继续可用：

```bash
export LANGFUSE_PUBLIC_KEY="pk-example"
export LANGFUSE_SECRET_KEY="secret-example"
export LANGFUSE_BASE_URL="https://langfuse.example.com"

agentengine run .
```

当 generic OTLP 和 Langfuse 环境变量同时存在时，`setup_tracing(enable_langfuse=None)`
优先使用 generic OTLP，不会再额外启用 Langfuse 直连 exporter。这样可以避免同一次运行在
Langfuse 中出现重复 traces。确实需要强制 Langfuse 兼容路径时，可显式调用
`setup_tracing(enable_langfuse=True)`。

如果把 Langfuse 当作 OTLP backend，也可以只配置标准 OTLP 变量：

```bash
export OTEL_SERVICE_NAME="customer-agent-runtime"
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL="http/protobuf"
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://langfuse.example.com/api/public/otel/v1/traces"
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Basic%20<base64-public-secret>,x-langfuse-ingestion-version=4"
```

公开文档只能使用占位 key 和用户自有 endpoint，不要发布真实 trace 截图、tenant id 或私有 URL。

## Callback-only 模式

LangChain 或 LangGraph 项目有时更适合使用 Langfuse callback handler，而不是 direct OTLP。
显式启用 callback-only：

```bash
export LANGFUSE_PUBLIC_KEY="pk-example"
export LANGFUSE_SECRET_KEY="secret-example"
export LANGFUSE_BASE_URL="https://langfuse.example.com"
export LANGFUSE_USE_CALLBACK="true"

agentengine run .
```

同一次运行通常只选择 callback 或 direct OTLP 之一，除非已经确认重复 traces 可接受。

## Span event 与子 span

span event 是某个 span 内的时间点事件，适合记录：

- `checkpoint.saved`
- `agent.run.started`
- `analysis.milestone`
- 错误提示和恢复 hint

子 span 是 trace tree 中的独立步骤，适合记录：

- tool 调用
- checkpoint 持久化
- 外部 I/O
- 报告生成
- score 计算

在 Langfuse 等后端中，子 span 通常更容易在 trace tree 或 observation 列表里看到，也更适合做
duration、status、tool name 和错误聚合。span event 通常挂在父 span 明细中，不一定会成为独立树节点。

## Metadata 与 score

长周期、多用户、多实例场景建议在关键 span 上带稳定、非敏感 attributes：

- `ksadk.agent_id`
- `ksadk.session_id`
- `ksadk.user_id`
- `ksadk.invocation_id`
- `ksadk.runtime.service`
- `ksadk.runtime.instance_id`

评估分数建议先用后端无关的 attributes 表达：

```python
span.set_attribute("score.name", "answer_quality")
span.set_attribute("score.value", 0.88)
span.set_attribute("score.source", "auto_evaluator")
span.set_attribute("score.comment", "Report covers evidence and next steps.")
```

如果后端是 Langfuse，平台服务或 OTel Collector 可以把 `score.*` attributes 转成 Langfuse
native score。KsADK 业务代码不需要直接 import Langfuse SDK，这样后续替换后端时改动更小。

不要把 raw prompt、凭证、私有 URL、客户名称或上传文件正文写入 span attributes。

## 故障排查

| 现象 | 可能原因 | 检查 |
| --- | --- | --- |
| 本地 trace 视图为空 | 没有运行产生 spans，或 tracing 被禁用 | 先跑一次请求，再确认本地服务启用了 tracing |
| generic OTLP 没有数据 | endpoint、TLS、auth 或 Collector policy 不匹配 | 检查 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` 和 headers |
| Langfuse 没有收到 spans | key 缺失、base URL 错误、callback-only 或 generic OTLP 抢占 | 检查 Langfuse 变量、`LANGFUSE_USE_CALLBACK` 和 `OTEL_EXPORTER_OTLP_*` |
| Langfuse 出现重复 traces | callback 和 direct OTLP 同时启用 | 同一项目选择一种路径 |
| score 没有显示为 native score | 后端没有把 `score.*` attributes 做映射 | 在 Collector 或平台服务侧增加转换逻辑 |
| 框架 spans 较少 | optional instrumentation 未安装或未生效 | 安装 tracing extra，并检查框架 instrumentation |

## 公开文档规则

- 使用占位 key 和用户自有 endpoint。
- 不提交包含 tracing 凭证的 `.env`。
- 不发布私有 trace id、tenant id、客户名称或真实截图。
- 私有 Collector 地址和租户路由写在内部 runbook，不放进公开 SDK 仓库。
