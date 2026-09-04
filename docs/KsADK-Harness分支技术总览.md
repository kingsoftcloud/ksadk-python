# KsADK Harness 分支技术总览

> 分支：`feat-ksadk-harness` · 文档状态：评审与合入说明 · 更新日期：2026-09-04

## 1. 概览

本分支将 KsADK 从“能够接入多种 Agent Runner”推进为一套面向企业 Agent 的统一
运行支架。它不重新发明模型或通用编排框架，而是在 LangGraph 等执行能力之上，
统一 Agent 的版本、运行合同、Context/Memory、工具安全、恢复、事件证据和生命周期。

默认路径由 **KsADK Harness Provider + Managed LangGraph Engine** 深度托管；Codex、
ADK 和外部 LangGraph 等 Runtime 继续通过 RuntimeAdapter 兼容接入。平台不要求所有
Runtime 暴露相同内部实现，而是通过 Capability Declaration 如实声明原生支持、兼容
支持、不支持和不可观测能力。

本分支的目标可以概括为：

> 换 Runtime，不换 Studio、版本、发布链路和事件合同；默认 Harness 提供完整治理，
> 外部 Runtime 按可验证能力兼容接入。

## 2. OpenSpec 依据

本文基于以下已完成的 OpenSpec change 汇总，不以历史计划代替当前实现：

| OpenSpec change | 解决的问题 | 状态 |
|---|---|---|
| `complete-ksadk-harness-core` | Agent Loop、Context/Memory、Skill/MCP、Sandbox、生命周期、多 Agent 与 RuntimeAdapter 的核心能力合同 | Complete |
| `durable-harness-checkpoint-recovery` | Studio/DSH Harness 的持久 Checkpoint、RunHandle 与跨进程恢复 | Complete |
| `fix-harness-usage-and-plugin-install-dx` | Studio 用量与推理过程上报、DSH 插件安装和默认授权体验 | Complete |

OpenSpec 确立的共同原则是：以“**合同 + Conformance + 证据**”收口；失败和降级必须
可见；缺少真实环境时报告 `not_configured`，不能用 Mock 或相似行为冒充生产通过。

## 3. 整体架构

KsADK Harness 是五层架构，不是固定的五步流水线。

### 3.1 体验与控制入口

Studio、Python SDK/CLI、OpenAI-compatible API、A2A/Hosted UI 和云端控制面共享平台
入口。入口负责形成请求和身份边界，不绑定某一种 Agent Loop。

### 3.2 构建与插件装配

Draft、Revision 或 Agent Project 经 Harness Compiler 编译为不可变 HarnessSpec，再由
DSH Provider Registry 选择并装配 Runtime Provider。DSH 负责插件发现和组装，不承载
Agent Loop。

### 3.3 稳定平台合同

所有 Runtime 通过三项合同接入：

- `RuntimeAdapter Contract`：统一启动、流式、取消、恢复和终态；
- `Capability Declaration`：声明每项能力的支持程度与责任方；
- `Canonical RuntimeEvent v2`：统一 Studio、Trace、Usage 和审计所消费的事实语义。

### 3.4 两类执行路径

- **默认深度托管路径**：KsADK Harness Provider 使用 Managed LangGraph Engine，平台
  控制 Agent Loop、Context/Memory/Policy，以及 Skill、MCP、Sandbox 和 Subagent。
- **兼容路径**：Codex、ADK、外部 LangGraph 或 BYO Runtime 通过 Adapter 映射能力与
  事件；平台不伪造无法观测的内部阶段。

### 3.5 共享状态、能力与证据

不同 Runtime 的运行事实统一沉淀为 Session、Checkpoint、Trace、Usage、Context
Manifest、Tool Receipt 和 Artifact，并复用 Model、Memory、MCP、Skill 与 Sandbox
的治理边界。

## 4. 已形成的核心能力

### 4.1 可靠 Agent Loop

- 支持非流式与增量流式回答、推理过程和 Tool Calling；
- 重组流式 Tool Call 分片，在参数完整且通过校验后只执行一次；
- 区分限流、鉴权、Context Overflow、无效请求和瞬时 Provider 故障；
- 支持有界重试、Fallback、取消、中断、审批挂起与恢复；
- Provider 调用受输出预算和运行控制约束，避免无界执行。

### 4.2 Context 与 Memory 治理

- Context 具有统一预算、Manifest、压缩、关键事实重注入和 Tool Pair 完整性；
- 本轮用户输入属于不可静默裁剪的 required 输入，超限时返回明确错误；
- Memory 具有租户/用户/Agent 作用域、来源、纠正、遗忘、锁定、TTL 与审计语义；
- 支持关键词与向量融合检索、可插拔 Reranker 和服务故障降级。

### 4.3 Skill、MCP 与 Sandbox

- Skill 与 MCP 使用 L0→L1→L2→L3 渐进披露，减少首轮上下文并阻止越级调用；
- MCP 支持动态风险审批、健康状态、熔断、Schema 漂移、断连恢复和大结果 Artifact
  Offload；
- Sandbox 通过统一 Backend 合同、租约、Fence、Journal 和 Conformance 保持后端可
  替换，业务逻辑不绑定 E2B、KOP 或特定私有实现。

### 4.4 持久恢复与副作用安全

- 本地 Studio 按 Workspace 使用 SQLite Checkpoint，云端可通过 DSN 使用 PostgreSQL；
- RunHandle、Session/EventStore、Checkpoint 与 Tool Receipt 使用一致命名空间；
- 进程重启后通过 `engine.attach()` 恢复，而不是重新模拟历史执行；
- 已确认完成的有副作用工具不会因恢复、审批续接或节点重放而重复执行。

### 4.5 长任务控制

默认 Managed Agent Loop 已接入可持久化的 RunController：

- 用目标、依赖里程碑和确定性验收证据定义“完成”；
- 对模型调用、工具调用、Token 和墙钟时间执行预算检查；
- 识别重复动作与重复错误，触发受控重规划；
- 在软预算阈值要求收口，在硬预算耗尽前阻止新的副作用；
- 记录实验 baseline/candidate，以外部评分决定保留或回滚；
- 将控制状态写入 Checkpoint，跨进程恢复后继续同一目标和预算。

模型不能仅凭一句“任务完成”改变最终状态；最终结果按证据区分 `verified`、
`partial`、`unverified`、`blocked`、`budget_exhausted` 和 `failed`。

## 5. 生命周期

Harness 保留两条互不污染但编译合同一致的路径：

1. **Draft 即时调试**：保存后可立即创建隔离会话，验证 Prompt、模型、Skill 和 MCP；
   不生成 BuildManifest，不注册正式 Route。
2. **正式发布**：Revision → Build → Approval → Deploy → Activate → Invoke；修改已激活
   配置会形成新版本，回滚只切换到已验证历史 Deployment。

这让 Studio 可以做到“保存即可试用”，同时保证调试状态不会成为线上事实源。

## 6. 我们形成的核心优势

1. **Runtime 可替换，平台能力保持一致**：不同 Runtime 按场景选择，外层版本、权限、
   会话、事件和生命周期不用重做。
2. **Context、Memory 与能力加载统一治理**：统一预算和关键事实保护；Skill/MCP 按需
   披露，在扩展能力规模时控制 Token 和安全边界。
3. **工具执行安全、可审计、可恢复**：Policy、Approval、Receipt、Checkpoint 和幂等
   机制形成完整证据链。
4. **长任务具备可验证闭环**：目标、里程碑、验收、停滞重规划与预算终止共同决定
   任务是否真正完成。
5. **能力和就绪度可以客观验证**：Capability Declaration、RuntimeEvent、Usage、
   Conformance 和就绪度门禁共同区分已支持、不可观测与尚未验证。

## 7. 验证与证据边界

本分支已经形成从单元/集成测试、真实模型与 MCP 矩阵、Sandbox Conformance 到本地
生命周期 E2E 的分层验证体系。最近一次长任务控制器及默认 Runtime 受影响测试为
`104 passed`；此前
Harness + Memory 回归为 `787 passed / 6 skipped`，OpenSpec Harness 合同测试为
`4 passed`。

这些结果证明仓内合同和本地路径可重复，但不替代下列目标环境验证：

- PostgreSQL 多 Pod 争用、接管和进程强杀；
- 企业 MCP 的真实凭证轮换；
- E2B、KOP 或私有 Sandbox 的目标模板与故障矩阵；
- 云端控制面的发布、路由与回滚；
- 24–72 小时持续负载和故障注入。

未配置项必须保持 `not_configured` 或升级为发布阻断，不能写成“已生产验证”。

## 8. 仓库边界

本仓负责 Python SDK、CLI 和数据面 Runtime，包括运行、恢复、Skill/MCP 消费、
Sandbox/Approval/Tool Safety 以及 RuntimeAdapter。完整 Registry、云端资源治理、Skill
Marketplace、远程 Sandbox 生命周期和云端路由控制仍属于相应平台服务。

因此，本分支的阶段结论是：

> KsADK Harness 已形成默认深度托管、外部 Runtime 兼容接入、统一治理和可靠恢复的
> 运行底座；仓内能力已经收口，生产级结论仍需在目标基础设施上完成对应矩阵验证。
