# KsADK Harness 能力就绪度与外部验证清单

> 适用分支：`feat-ksadk-harness`  
> 更新日期：2026-09-01
> 目的：区分“仓内已实现”“已具备合同但待真实环境验证”和“明确属于外部控制面”的工作，避免把未配置的外部服务描述为已经打通。

## 1. 总体结论

KsADK Harness 的默认运行主链已经具备可用于 MVP 的完整语义：模型与 Tool Loop、Context/Memory、Skill/MCP 渐进披露、审批、Artifact、Sandbox 合同、Draft 调试、正式生命周期、事件与发布准入。

当前主要剩余工作不再是补一套新的 Agent Loop，而是扩大真实环境兼容矩阵，并由 Studio、云控制面和外部服务消费已经稳定的合同。

## 2. 仓内已经实现并可回归验证

| 维度 | 已实现能力 | 主要验证入口 |
|---|---|---|
| Agent Loop | 模型调用、流式文本与碎片化 Tool Call 重组、重试、Provider 回退、Context Overflow 单次恢复、中断与恢复 | `tests/harness/test_managed_langgraph_engine.py`、`test_model_matrix_eval.py` |
| Context | Stable Prompt/动态 Context 分层、预算、Manifest、压缩、关键事实重注入、Cache 诊断、可视化、调优建议、安全后台整理 | `test_context_*`、`test_prompt_cache.py` |
| Memory | Scope、受控写入、纠错/遗忘/锁定、TTL、审计、语义混检、专用 Reranker、异步整理、多模态来源 | `tests/memory`、`test_memory_*` |
| Memory 评测 | 4 条真实模型 smoke；100 条冻结金标规模回归，显式开关可运行完整真实模型集 | `python -m ksadk.harness.real_model_eval --memory-dataset full` |
| Skill | L0→L1→L2→L3、越级保护、意图推荐、子 Agent 透传、反馈投影、发布就绪矩阵 | `test_skill_*`、`skill_e2e_eval.py` |
| MCP | L0→L1→L2→L3、动态审批、健康/熔断、凭证轮换、Schema 刷新、大结果 Offload、兼容矩阵 | `test_mcp_*`、`mcp_e2e_eval.py` |
| Sandbox | Backend 合同、能力声明、租约、Fence、Journal、重连恢复、取消与 Conformance 矩阵 | `test_sandbox_*` |
| 生命周期 | Draft 保存即可试用；Revision→Build→Deploy→Activate→Invoke→Rollback 控制面一致性合同 | `test_draft_runtime.py`、`test_lifecycle_conformance.py` |
| 多 Agent | 动态子 Agent、父子事件、并行执行、独立 Checkpoint 命名空间、依赖拓扑、时间/Token/Tool/Artifact 预算、结构化结果与 Schema 验收 | `test_subagent.py` |
| 多 Runtime | 统一声明指令、历史、压缩、记忆、工具、流式、审批、Checkpoint、恢复和事件的支持程度与责任方；不可观测能力不伪装支持 | `test_runtime_capabilities.py`、`test_runner_conformance_matrix.py` |
| 发布准入 | 聚合 Runtime、Model、Skill、MCP、Sandbox、Lifecycle 报告为 ready/warning/blocked 和修复项 | `test_release_readiness.py`、`test_skill_matrix.py` |

### 2.1 OpenSpec 收口规格映射

下表是 `complete-ksadk-harness-core` 七项正式能力规格的实现与验证索引。“缺口/外部责任”列为空不表示生产环境已经联通，只表示仓内合同已有对应实现或回归入口。

| 能力规格 | 主要实现 | 主要回归/矩阵 | 缺口或外部责任 |
|---|---|---|---|
| `agent-loop-reliability` | [`engine/langgraph.py`](../ksadk/harness/engine/langgraph.py)、[`model_provider.py`](../ksadk/harness/model_provider.py)、[`tool_reliability.py`](../ksadk/harness/tool_reliability.py) | [`test_managed_langgraph_engine.py`](../tests/harness/test_managed_langgraph_engine.py)、[`test_model_matrix_eval.py`](../tests/harness/test_model_matrix_eval.py)、[`test_tool_reliability.py`](../tests/harness/test_tool_reliability.py) | 目标 Provider 的真实兼容矩阵 |
| `context-memory-governance` | [`context_engine.py`](../ksadk/harness/context_engine.py)、[`session_store.py`](../ksadk/harness/session_store.py)、[`memory_runtime.py`](../ksadk/harness/memory_runtime.py) | [`test_session_store.py`](../tests/harness/test_session_store.py)、[`test_context_maintenance.py`](../tests/harness/test_context_maintenance.py)、[`tests/memory`](../tests/memory) | 目标模型上的完整金标评测 |
| `capability-runtime-resilience` | [`skill_runtime.py`](../ksadk/harness/skill_runtime.py)、[`mcp_runtime.py`](../ksadk/harness/mcp_runtime.py)、[`engine/mcp_disclosure.py`](../ksadk/harness/engine/mcp_disclosure.py) | [`test_skill_progressive_disclosure_e2e.py`](../tests/harness/test_skill_progressive_disclosure_e2e.py)、[`test_mcp_disclosure_loop.py`](../tests/harness/test_mcp_disclosure_loop.py)、[`test_mcp_matrix.py`](../tests/harness/test_mcp_matrix.py) | 企业 MCP 鉴权轮换与故障矩阵 |
| `sandbox-backend-conformance` | [`sandbox_backend.py`](../ksadk/harness/sandbox_backend.py)、[`sandbox_lease.py`](../ksadk/harness/sandbox_lease.py)、[`sandbox_recovery.py`](../ksadk/harness/sandbox_recovery.py) | [`test_sandbox_conformance.py`](../tests/harness/test_sandbox_conformance.py)、[`test_sandbox_recovery_conformance.py`](../tests/harness/test_sandbox_recovery_conformance.py)、[`test_sandbox_matrix_eval.py`](../tests/harness/test_sandbox_matrix_eval.py) | E2B、KOP/私有后端真实模板 |
| `agent-lifecycle` | [`draft_runtime.py`](../ksadk/harness/draft_runtime.py)、[`lifecycle.py`](../ksadk/harness/lifecycle.py)、[`lifecycle_conformance.py`](../ksadk/harness/lifecycle_conformance.py) | [`test_draft_runtime.py`](../tests/harness/test_draft_runtime.py)、[`test_lifecycle_conformance_e2e.py`](../tests/harness/test_lifecycle_conformance_e2e.py) | 云控制面联合 E2E |
| `multi-agent-orchestration` | [`subagent.py`](../ksadk/harness/subagent.py)、[`strategies.py`](../ksadk/harness/strategies.py)、[`event_tree.py`](../ksadk/harness/event_tree.py) | [`test_subagent.py`](../tests/harness/test_subagent.py)、[`test_strategies.py`](../tests/harness/test_strategies.py)、[`test_event_tree.py`](../tests/harness/test_event_tree.py) | 跨进程多分支恢复继续收口 |
| `runtime-adapter-conformance` | [`runtime.py`](../ksadk/harness/runtime.py)、[`runtime_capabilities.py`](../ksadk/harness/runtime_capabilities.py)、[`conformance/contract.py`](../ksadk/harness/conformance/contract.py) | [`test_runtime_capabilities.py`](../tests/harness/test_runtime_capabilities.py)、[`test_runner_conformance_matrix.py`](../tests/harness/test_runner_conformance_matrix.py)、[`test_conformance_suite.py`](../tests/harness/test_conformance_suite.py) | 外部 Runtime 私有状态按合同标记 `opaque` 或 `unavailable` |

规格源位于 [`openspec/changes/complete-ksadk-harness-core/specs`](../openspec/changes/complete-ksadk-harness-core/specs)，后续实现任务以该目录的 Requirement/Scenario 为完成口径。

### 2.2 默认托管引擎能力声明证据（task 8.2）

默认 LangGraph 托管引擎的能力声明由 [`managed_langgraph_capabilities()`](../ksadk/harness/runtime_capabilities.py) 产出，逐能力列出来源对象与回归证据，不可观测字段不得伪造。下表是声明与其证据引用的完整对照，由 `tests/harness/test_runtime_capabilities.py` 和联合 E2E 共同约束。

| 能力维度 | 责任方 | 声明值 | 实现来源 | 证据引用 |
|---|---|---|---|---|
| `instructions` | PLATFORM | supported | [`spec.py`](../ksadk/harness/spec.py) `PromptSpec` | `tests/harness/test_prompt_contract.py` |
| `history` | PLATFORM | supported | [`context_engine.py`](../ksadk/harness/context_engine.py) | `tests/harness/test_context_pipeline.py` |
| `compaction` | PLATFORM | supported | [`context_engine.py`](../ksadk/harness/context_engine.py) 单次恢复 | `tests/harness/test_context_pipeline.py` |
| `memory` | PLATFORM | supported | [`memory_runtime.py`](../ksadk/harness/memory_runtime.py) | `tests/memory` |
| `tools` | PLATFORM | supported | [`tools.py`](../ksadk/harness/tools.py)、[`loop/tools.py`](../ksadk/harness/loop/tools.py) | `tests/harness/test_loop_tools.py` |
| `streaming` | PLATFORM | supported | [`reasoner.py`](../ksadk/harness/reasoner.py) SSE 重组 | `tests/harness/test_harness_reasoner.py` |
| `approval` | PLATFORM | supported | [`engine/graph_builder.py`](../ksadk/harness/engine/graph_builder.py) interrupt | `tests/harness/test_engine_langgraph.py` |
| `checkpoint` | PLATFORM | supported | [`engine/langgraph.py`](../ksadk/harness/engine/langgraph.py) Checkpointer | `tests/harness/test_engine_langgraph.py` |
| `recovery` | PLATFORM | supported | [`engine/langgraph.py`](../ksadk/harness/engine/langgraph.py) attach/resume | `tests/harness/test_runner_conformance_matrix.py` |
| `events` | PLATFORM | supported | [`events.py`](../ksadk/harness/events.py) RuntimeEvent v2 | `tests/harness/test_event_v2_envelope.py` |

联合 E2E 证据：[`test_lifecycle_conformance_e2e.py`](../tests/harness/test_lifecycle_conformance_e2e.py) 在真实子进程上完成 Build → Deploy → Approve → Activate → Invoke → Rollback，并验证回滚后调用命中历史版本；多 Agent 跨进程恢复由 [`test_subagent_orchestration_gaps.py`](../tests/harness/test_subagent_orchestration_gaps.py) 的 7.5 用例证明已完成子任务经 Receipt 回放不重跑、未完成节点从一致状态继续。外部 Runtime（Codex/ADK）按各自能力声明运行适用 Conformance，不可观测字段标记 `opaque`/`unavailable`，强制安全用例不可被声明规避。


### 2.3 仓内基线（2026-09-01）

- 环境：仓库 `.venv`，由仓库内 `uv` 和 lock/optional extras 装配。
- 命令：`.venv/bin/uv run --no-sync .venv/bin/python -m pytest -q tests/harness tests/memory tests/architecture`
- 结果：`639 passed, 7 skipped, 13 warnings`，耗时 `61.41s`。
- 7 个 skipped 均为需要显式真实环境开关或外部配置的用例；不计作通过。Warnings 主要来自第三方弃用提示与架构模块尺寸提示，当前无测试失败。

## 3. 已有合同、必须在真实环境继续验证

以下项目不能靠本仓单元测试宣称生产打通，但已经有可执行矩阵。未提供端点、模板或凭证时必须报告 `not_configured`；required 项应报告 `blocked`。

| 外部条件 | 本仓提供 | 真实验证责任 |
|---|---|---|
| 多模型厂商端点 | Model 发现、Chat、Tool Calling、Streaming、Usage、能力要求矩阵 | 在目标模型网关配置只读测试凭证并运行矩阵 |
| 企业 MCP | 健康、tools/list、Schema、只读探针、凭证轮换和错误脱敏矩阵 | 每类 Transport、鉴权和企业网关在联调环境运行 |
| E2B/KOP/私有 Sandbox | Backend/Lease/Recovery Conformance 与就绪矩阵 | 使用真实模板 ID、网络策略和短期凭证执行 |
| 云生命周期 | 不可变 Revision、Build、审批、Route、Invoke、Rollback Conformance | `agentengine-server`、审批服务和网关联合 E2E |
| 云端观测与 Memory | 稳定 RuntimeEvent/Insights/Memory Provider 合同 | 平台事件存储、组织权限和治理页面接入 |

## 4. 明确不搬入本仓的能力

- Skill 注册、CRUD、安装升级、市场、版本治理属于 Skill Service；Harness 只消费固定版本包并验证运行就绪度。
- Agent Registry、云端 Route、Hosted Runtime、正式审批与回滚属于 `agentengine-server` 和平台控制面；Harness 提供数据面协议与 Conformance。
- 企业凭证不进入 HarnessSpec、Bundle、测试快照或报告；只使用 Secret 引用和宿主注入。
- 不为 E2B、KOP 或任意云服务伪造“已通过”结果。

## 5. 收口验收顺序

1. 仓内执行 `pytest tests/harness tests/memory -q` 和 Ruff；
2. 使用目标模型网关运行 Model、长任务、Memory、Skill、MCP 真实模型评测；
3. 使用目标 MCP 与 Sandbox 环境运行兼容矩阵；
4. 与云控制面执行 Build→Deploy→Activate→Invoke→Rollback E2E；
5. 将所有报告汇入 `release_readiness()`，required 检查全部 ready 后方可宣称目标环境可部署。

这份清单只描述可由证据支持的状态。外部环境尚未配置，不等同于代码缺失；同样，已有合同也不等同于目标生产环境已经联通。

## 6. 完成度审计（task 9.5，2026-09-01）

逐项核对 `openspec/changes/complete-ksadk-harness-core/tasks.md` 的未勾选项，区分「真缺」「验收口径差」「诚实 not_configured」和「不在本变更范围」。

### 6.1 本轮补齐

| Task | 之前状态 | 本轮处置 | 证据 |
|---|---|---|---|
| 7.1 子 Agent 独立 Run/Checkpoint | 已实现，验收口径差 | 补断言测试：并行子 Agent 独立 run_id + 独立 Checkpoint 命名空间 + 事件树互不覆盖 | `tests/harness/test_subagent_orchestration_gaps.py::test_7_1_*` |
| 7.2 依赖图/预算 | 已实现，验收口径差 | 补断言测试：max_tool_calls=0 副作用不启动 + 依赖图 b 不先于 a 启动 | `test_7_2_*` |
| 7.3 fail-fast/部分成功/重试/父取消 | 框架有，缺组合测试 | 补多分支故障注入矩阵：fail_fast 取消 sibling / partial 保留 sibling / retry 重试一次成功 | `test_7_3_*` |
| 7.5 跨进程多 Agent 恢复 E2E | 单 Agent 有，多 Agent 无 | 补多分支跨进程 E2E：两子任务完成 + 审批中断，恢复后均经 Receipt 回放不重跑、审批副作用一次 | `test_7_5_cross_process_multi_branch_recovery_does_not_rerun_completed_children` |
| 8.2 联合 E2E + 完整能力声明证据 | E2E 有，缺文档化 | 补能力声明证据表（见 §2.2），逐能力列责任方/实现/证据引用 | `docs/KsADK-Harness能力就绪度与外部验证清单.md` §2.2 |
| 9.5 代码评审与完成度审计 | 未做 | 本节 | 本节 |

### 6.2 诚实 `not_configured`（非缺陷）

以下项缺少真实环境凭证/端点，矩阵如实报告 `not_configured`，不伪造通过：

- Codex 事件流 Conformance：需 app-server 进程与 API 凭证，本机无环境；capability 声明层 Conformance（26 测试）已真跑通过。
- E2B 真实 Conformance：需 `E2B_API_KEY` 与模板 ID；私有 Subprocess 后端始终执行真实 Conformance 作为基线。
- 目标模型网关 / 企业 MCP / 云控制面：需联调环境凭证，矩阵入口已就绪。

### 6.3 明确不在本变更范围

- **财务编排迁移（`docs/KsADK-Harness统一运行时技术方案.md` §14.3）**：要求把 `ksadk.orchestration` 财务 Manager/Executor/Reviewer 拆分为通用 `SubtaskContract` / `ExecutionReport` / `AuditReport` + 财务 Plugin + `plan-execute-review` Graph Compiler。该工作属于「现有财务编排迁移」的独立重构，不在 `complete-ksadk-harness-core` 七项收口规格内；本变更只交付多 Agent 编排合同（`multi-agent-orchestration`），不吸收财务领域拆分。`ksadk/orchestration` 旧模块已清空源码（仅留 `__pycache__`），其迁移应另立 OpenSpec change。
- Studio 展示/查询（第 7 项原计划的展示面）未混入本变更：本仓只交付数据面合同与回归，不引入 Studio UI 或查询面。

### 6.4 审计结论

`complete-ksadk-harness-core` 的 39 项 task 中，本轮补齐 6 项未勾选项后全部对齐验收口径；剩余未完成均为外部环境条件（§6.2），已明确记录而非宣称通过。仓内合同与回归完整，真实环境联通责任归各外部服务。
