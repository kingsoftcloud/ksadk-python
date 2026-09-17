# Skill 评测结果独立传递

状态：2026-09-11 工作区实现，尚未部署。适用于 Base Agent 与 KsADK 的结果边界；本次未修改 AgentEngine Server。

## 契约与职责

Base Agent 最终 Graph 状态显式返回 `skill_eval_result`（schema_version 为 `base_agent.skill_eval_result.v2`），`answer` 和消息 content 只承载业务正文。每轮重新生成结果，不把结果追加为文本流，也不从模型消息解析结果。

KsADK 只提取显式、可 JSON 序列化的 v2 字段。LangGraph invoke、dict stream、checkpoint resume 和 canonical stream 在完成时传递；canonical 完成事件通过 source.metadata.metrics 保存结果，沿既有 Runtime 完成元数据路径持久化。Responses `/v1/responses` 与 `RunAgent` 的 Responses 格式在非流式 JSON、流式完成事件中返回同名独立字段，正文 output_text、output message 和文本 delta 不混入结果。无显式有效结果的 Agent 不新增结果字段。有效的 run.trace_id 同时投影到顶层 trace_id，保持旧评测调用方的关联能力；全零或非法 Trace ID 不投影。

结果字段不是认证声明。执行、产物与版本证据的可信边界不因传输字段改变而升级；不得把整个内部 Graph 状态或凭证透传到公共 API。

## Trace 与兼容

最终节点输出中的 skill_eval_result 与 answer/messages/timing 并列。运行 Span 使用 OpenInference 的 metadata JSON 属性记录结果，并设置 langfuse.trace.metadata.skill_eval_result；output.value 与 langfuse.trace.output 保持纯正文。模型节点的 usage_metadata 不改变，不向中间模型节点补写运行结束后的结果。

旧 Base Agent 的正文后缀仍由 AgentEngine Server 既有 RunSkillEvaluation 解析逻辑处理。新版本无需再次拼接。Agent 与 KsADK 应一起升级：仅移除 Agent 后缀而使用旧 KsADK 会失去 API 结果字段。Chat Completions 不在本次新增结构化结果契约内，其可见正文同样保持纯文本。

## 验证边界

本地覆盖独立字段和纯正文、流式/非流式 Responses、RunAgent、直接 Graph invoke、canonical 完成结果、节点输出及 Span 属性构造。测试使用本地模型替身，不调用真实模型、Skill Service 或沙箱；生产 Langfuse 属性映射与 AgentEngine Server 全链路需部署后确认。

2026-09-11 验证：KsADK 相关回归 365 项通过；Base Agent 54 项通过。覆盖 Responses/RunAgent 两入口的流式与非流式响应、canonical v3 完成字段、Graph 节点输出与运行 Span metadata，正文均不附加评测块。变更实现的 Ruff F/I 检查与 git diff --check 通过。
