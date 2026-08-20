# Cloud Monitor Span 兼容设计

## 目标

在不改变原生 Langfuse OTLP 上报的前提下，修复 Cloud Monitor 的资源标识和字段兼容：

- Cloud Monitor 的 `resource.service.name` 使用 `resource.agentengine.agent_id`。
- 将 resource 中的 `agentengine.account_id`、`agentengine.agent_id`、
  `agentengine.framework`、`agentengine.langfuse_project_id`、`agentengine.region`
  复制到每个 Span 的 `gen_ai.agentengine.*` 属性；Span 已有值时不覆盖。
- 在 Span attributes 中保留或补充 Langfuse 标准字段：`session.id`、
  `langfuse.observation.type`、`langfuse.observation.metadata.ls_provider`、
  `langfuse.observation.model.name`、
  `langfuse.observation.metadata.ls_model_name` 和
  `langfuse.observation.usage_details`。usage details 使用 JSON 字符串。
- 保留现有 `langfuse.*` Span 属性，并恢复旧版 Cloud Monitor 根 Span 的
  `gen_ai.usage.*` token 汇总。
- 保持两路 OTLP 的 `trace_id`、`span_id` 和父子关系一致。

## 方案

继续使用一个 `TracerProvider` 生成同一批 `ReadableSpan`。Generic/Langfuse exporter
直接接收原始 Span；仅 Cloud Monitor 的 `_LoggingSpanExporter` 在委托导出前调用
`_prepare_cloud_monitor_spans`。

转换函数克隆 Cloud Monitor 所需的 Span 和 Resource，不修改输入对象：

1. 从 Resource 读取 `agentengine.agent_id`，非空时仅在克隆 Resource 中覆盖
   `service.name`。
2. 将选定的 `agentengine.*` Resource 属性通过 `setdefault` 复制到克隆 Span 的
   `gen_ai.agentengine.*` 属性。
3. 保留 Span 的全部现有属性，包括 `langfuse.trace.*`、
   `langfuse.observation.*`、`langfuse.session.id` 和 `langfuse.user.id`。
4. 仅在缺失时从现有 OpenInference/GenAI 属性补充 Langfuse observation 类型、
   provider、model 和 usage details，并将同一 trace 的 `session.id` 复制到子 Span。
5. 使用删除前实现的叶子 Span token 聚合规则，仅为缺少 usage 的根 Span补充
   `gen_ai.usage.*`。
6. 克隆时复用原始 context、parent、events、links、status、时间和 instrumentation
   scope，因此不会生成新 trace 或改变拓扑。

不修改 `OTEL_SERVICE_NAME`、全局 `TracerProvider` Resource、Agent Runtime 环境变量，
也不创建第二个 `TracerProvider`。

## 失败语义

- Resource 缺少 `agentengine.agent_id` 时不改 `service.name`。
- Resource 属性为空时不复制。
- Span 已有同名 `gen_ai.agentengine.*` 或 Langfuse 属性时保留 Span 原值。
- 转换或克隆异常沿用 exporter 的现有失败日志和导出失败语义，不回退为修改原对象。

## 验证

- 单测验证 Cloud Monitor 专属转换、非覆盖语义、Resource 保留和旧版 token 汇总。
- 双路本地 OTLP HTTP E2E 验证 Langfuse/Generic 原始字段不变、Cloud Monitor 字段新增，
  且两路 `trace_id`、`span_id` 完全一致。
