## Why

KsADK Harness 已具备 Agent Loop、Context/Memory、Skill/MCP、Sandbox、生命周期、多 Agent 与多 Runtime 适配的主要模块，但各能力的完成口径仍分散在实现、矩阵和测试中，部分真实环境与故障路径尚未形成统一的发布门禁。需要以明确的运行合同、降级语义和端到端证据完成核心能力收口，使默认 Harness 能稳定用于本地调试和生产托管，并诚实表达外部 Runtime 与后端的能力差异。

## What Changes

- 收口默认 Agent Loop 的流式 Tool Calling、重试、取消、中断、Checkpoint 恢复、Context Overflow 与 Provider 故障处理状态机。
- 收口长任务 Context/Memory 的重建、后台整理、语义召回、冲突处理、故障降级和真实模型质量门禁。
- 收口 Skill/MCP 渐进披露、鉴权轮换、Schema 漂移、健康检查、熔断、断连恢复和企业兼容矩阵。
- 建立统一 Sandbox Backend Conformance，并对 Subprocess、E2B、KOP/私有后端给出真实验证或明确的 `not_configured` 结论。
- 固化 Draft 即时调试与正式 Revision → Build → Approval → Deploy → Activate → Invoke → Rollback 两条隔离生命周期。
- 收口多 Agent 独立 Checkpoint、预算、并行调度、失败传播、取消、Artifact 聚合和父节点验证。
- 以 KsADK Harness + LangGraph 为默认深度接管实现，通过 RuntimeAdapter、Capability Declaration 和 Conformance 兼容 ADK、Codex 等外部 Runtime，不承诺无法观测或不受平台控制的能力。
- 明确排除 Studio 可观测查询与展示产品建设；仅保留上述运行、恢复、验证与兼容性所必需的 RuntimeEvent 和证据数据。
- 不改变现有公开版本号，不执行发布，不把 Registry、Skill 管理面或云端控制面搬入本仓库。

## Capabilities

### New Capabilities

- `agent-loop-reliability`: 默认 Agent Loop 的流式执行、工具循环、重试、取消、恢复、溢出处理和故障分类合同。
- `context-memory-governance`: 长任务 Context 与长期 Memory 的作用域、整理、召回、冲突、降级和质量验证合同。
- `capability-runtime-resilience`: Skill 与 MCP 的渐进披露、鉴权、Schema 漂移、健康状态、恢复和兼容矩阵合同。
- `sandbox-backend-conformance`: Sandbox 后端能力声明、租约、隔离、取消、恢复、Artifact 与真实后端验证合同。
- `agent-lifecycle`: Draft 调试和正式不可变版本构建、部署、激活、调用、回滚的隔离生命周期合同。
- `multi-agent-orchestration`: 子 Agent 的 Checkpoint、预算、调度、失败传播、取消、结构化输出和父节点验证合同。
- `runtime-adapter-conformance`: 默认托管引擎与外部 Runtime 的能力声明、事件、恢复和一致性验证合同。

### Modified Capabilities

无。当前 OpenSpec 主规格尚未建立，本变更将现有 Harness 行为和剩余收口要求首次固化为正式能力规格。

## Impact

- 主要影响 `ksadk/harness/`、`tests/harness/`、`tests/memory/` 及 Harness 正式文档。
- 可能调整内部 Engine、Reasoner、RuntimeAdapter、SandboxBackend、MCP/Skill Runtime 和生命周期装配接口，但应保持现有 SDK 边界及已验证行为兼容。
- 真实环境验证可能依赖模型 Endpoint、MCP 服务、E2B、KOP 或平台私有 Sandbox；缺少条件时必须形成可机器读取的阻断或 `not_configured` 结果，不得伪造通过。
- `agentengine-server`、Skill Service、Sandbox Service 仍分别拥有云端控制面、Skill 治理和远程 Sandbox 生命周期；跨服务缺口只在本仓记录契约和联调条件。
