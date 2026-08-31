# KsADK Harness 能力就绪度与外部验证清单

> 适用分支：`feat-ksadk-harness`  
> 更新日期：2026-08-31  
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
| 多 Agent | 动态子 Agent、父子事件、并行执行、独立 Checkpoint 命名空间、并发上限、超时与失败传播策略 | `test_subagent.py` |
| 发布准入 | 聚合 Runtime、Model、Skill、MCP、Sandbox、Lifecycle 报告为 ready/warning/blocked 和修复项 | `test_release_readiness.py`、`test_skill_matrix.py` |

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
