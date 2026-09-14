# Managed 执行策略与子执行治理

这是宿主 Python 接口的增量合同；不修改冻结的 AgentControl/v1、Plugin/v1
或 RuntimeEvent/v2 Schema。

## 授权入口

`ksadk.harness.execution_policy.ExecutionPolicyResolver` 提供：

```python
async def resolve(ref: str, *, request: StartRequest) -> ExecutionPolicy: ...
```

`ref` 是宿主产生的不透明引用。宿主必须校验主体、会话、`metadata.run_id`、
插件授权及有效期；引用本身不是权限证明。Kernel 仅传递
`metadata.execution_policy_ref`。Provider 每次启动、恢复、模型轮及工具执行前
重新调用 resolver。未装配 resolver、引用无效或权限撤销必须拒绝执行。

`ExecutionPolicy` 是冻结对象，字段为 `system_context`、`tools`、
`workspace_root`、`limits`、`approval_required`、`child_system_context`。工具与预算 Mapping 只读。
`limits` 支持 `max_total_tokens / max_tool_calls / max_model_calls / max_artifacts`。
策略只能收紧 Revision 已有预算。后续解析允许更新事实上下文及继续收紧预算，
本次 Run 的模型提示保留启动时快照；工具集合、工作区、审批边界变更需要新的引用。

策略工具按 Run 保存，不写共享 Engine 工具注册表，不能覆盖已有工具或披露工具。
原生 `call_id` 传到工具 handler；不会放进模型 arguments。
宿主可通过 `child_system_context` 单独指定子执行的精简上下文；默认 `None`
继续沿用父 `system_context`，显式空字符串不附加上下文。该字段只改变子模型
提示，委派 task 仍独立传入；原 resolver、授权主体、工具子集、审批、工作区
及父子预算全部保留，不授予额外权限。父子上下文在同一 Run 内保留启动快照。
内置工具桥直接使用宿主显式策略工作区作为文件根，不能再次拼接 identity 或
`.harness-tools/workspace`；相对路径与宿主产物工具一致。子执行继承相同工作区
与权限约束。没有显式策略工作区时保留原来的 identity 工作区布局。

真实能力通过 `adapter.capabilities().execution_policy` 返回
`RuntimeCapability`。没有 resolver 时为 `supported=false`；legacy Provider
不能从产品名称推断支持。DSH Harness Factory 接收
`services['execution_policy_resolver']`。Studio 的 `AgentSpec.subAgents` 接受类型化
`SubAgentBinding`（子项支持 `maxTurns / timeoutSeconds / inheritSkills / inheritMcp`
等 camelCase 字段）；Compiler 保留到不可变 Bundle 的 `subAgents`，装配器将其
编译进 `HarnessSpec.sub_agents`，同时兼容旧 `tool_contracts.sub_agents`。
子声明纳入 source/resolved digest；工具必须属于父 Agent 已锁定的工具范围，
重复名称、工具重名、未知依赖或依赖环均被拒绝。此入口仅支持 Harness。

Handle 的 `native_ref` 保存原始 `execution_policy_ref / execution_policy_run_id`。
跨进程 attach 重建原会话/用户/Run 请求并重新解析；不持久化 Python callable
或解析后的授权对象。宿主必须保护 Handle 与引用存储的完整性。

## 子执行状态与事件

每次委派以 `parent Run + child name + native call_id` 派生独立 Run 与
Checkpoint namespace。子引擎继承父审批集合、CapabilityRuntime、授权主体、
租户、工作区、模型策略和预算上限；MCP 仍需声明 `inherit_mcp`。
子预算消耗累计到父预算；模型尝试、工具执行、Artifact 写入都在执行前检查。
执行时间预算累计，人工审批等待时间不计入执行时间。

受治理工具各自提交 Checkpoint。子审批通过父图独立的 `child_approval`
节点暂停，保存 child Run、handle、原生 call_id；每次审批只适用于对应的原生调用。
连续子审批有不同 interaction id，不能用上次批准替代下次决定。
恢复会 attach 原子 Run，已完成子 Checkpoint 直接返回结果，不重启副作用。

父取消显式取消并等待活动子执行终态；审批等待中的子执行也可以取消。
取消/失败写入子 Checkpoint 终态，恢复不能将它视作普通 paused 执行。
执行超时会停止实际子任务。

子事件发生时立即进入父流，保留原始 event id、源序号、真实 child Run 与
parent Run。canonical 父 Run 流中的子生命周期使用 scoped status item，
`source.native_run_id` 和 `source.metadata.native_event_type / parent_run_id / native_seq`
保留原始事实。仅父执行发 canonical `run.*` 及可操作的审批 interaction；
因此子完成不会误结束 Kernel 父 Run。平台最终排序以持久事件账本序号为准。

没有工具、Skill、MCP、共享有限预算或审批策略的显式纯文本子任务可并行。
包含工具/审批/有限预算的委派按可恢复的 Checkpoint 顺序执行。
独立 Kernel 成员 Run 的并发调度不受该子工具序列约束。

## Studio 审批与本地账本

Durable Managed Harness 声明 `submit_interaction=true` 与
`interaction_mode=durable_resume`。Kernel 的 `HarnessInteractionProvider` 根据
权威 InteractionRecord 的原始 thread/call 恢复同一个检查点；approve/reject
显式映射为 decision，cancel 调用实际取消接口。内存装配仍不声明持久交互能力。

Studio 的 SQLite Kernel 与 LocalSessionService 使用同一文件，使 Inbox admission
及 InteractionLedger 的事件与状态同事务提交。旧每 Build Kernel 文件在启动时
迁入会话数据库，保留 Run、Inbox、fence、grant 与幂等回执；旧文件保留为只读备份。
迁移拒绝活动 lease、未知 schema、不同 Build 或事实冲突。源写屏障先于目标提交，
目标 receipt 与源内容指纹支持中途崩溃重试，并阻止旧事实覆盖已继续执行的 Run。

## 验证

`uv run --no-sync pytest tests/harness` 使用真实 Memory/SQLite LangGraph、
原生内置写文件子进程及可控 Reasoner，验证审批、连续审批、重启恢复、
父子取消、超时、预算、实时事件、能力继承、工具/工作区隔离和撤销。
这些测试不需要模型密钥。Postgres Checkpoint 与外部模型由宿主集成测试验证。

`KSADK_DSH_TOOLCHAIN_E2E=1 uv run --no-sync pytest tests/e2e/test_teams_studio_e2e.py`
经过官方 DSH Core、Studio 实际 Build、Kernel Run、协作工具与 scoped HTTP 操作，
覆盖两成员并行、原生 SubAgent 来源树、人工验收、Leader join、审批重启恢复及停止。
迁移与同库 admission 故障测试位于 `tests/studio/test_kernel_sqlite_migration.py`
和 `tests/kernel/test_sqlite_atomic_admission.py`。
