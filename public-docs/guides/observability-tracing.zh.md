# 可观测与链路追踪

KsADK 支持本地 spans、Langfuse 和 OTLP exporter。可观测配置是可选能力，不应成为
quickstart 的前置条件。

## 常见变量

- `LANGFUSE_PUBLIC_KEY`
- `LANGFUSE_SECRET_KEY`
- `LANGFUSE_BASE_URL`
- `LANGFUSE_HOST`
- `LANGFUSE_USE_CALLBACK`

## 建议

- 本地开发先跑通模型调用，再启用 tracing。
- 公开文档只写占位 key 和 URL。
- trace metadata 不应包含 token、客户数据或完整附件内容。
- 失败时打印诊断，不影响普通本地运行。
