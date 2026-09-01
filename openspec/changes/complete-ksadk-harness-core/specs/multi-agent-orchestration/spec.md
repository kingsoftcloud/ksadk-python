## Purpose

定义父 Agent 对子 Agent 的委派、并行执行、独立恢复、预算、失败传播和结果验收合同，使多 Agent 协作可控、可取消且可验证。

## ADDED Requirements

### Requirement: 子 Agent 必须具有独立执行身份和状态
每个子 Agent 运行 MUST 拥有独立 Run 标识、Checkpoint、预算和事件父子关系，同时继承经策略允许的能力绑定与作用域。

#### Scenario: 两个子 Agent 并行运行
- **WHEN** 父 Agent 并行委派两个互不依赖的任务
- **THEN** 两个子运行独立持久化，任一恢复不会覆盖另一运行状态

### Requirement: 调度必须尊重依赖与预算
Orchestrator SHALL 只并行调度依赖已满足且资源允许的节点，并 MUST 在启动前分配可执行的 Token、时间、工具和 Artifact 预算。

#### Scenario: 子任务预算耗尽
- **WHEN** 子 Agent 达到分配预算
- **THEN** 该子任务以 budget_exhausted 结束，且不得继续消耗父任务未授权预算

### Requirement: 失败和取消必须按策略传播
父 Agent MUST 根据节点策略处理子任务失败，可选择 fail-fast、允许部分结果或重试；父运行取消 SHALL 传播到所有活动子运行。

#### Scenario: 必需子任务失败
- **WHEN** 标记为 required 的子任务不可恢复失败
- **THEN** 父节点不得将计划标记成功，并按配置取消依赖它的待执行节点

### Requirement: 子 Agent 结果必须经过结构化验收
子 Agent SHALL 返回结构化状态、输出、Artifact、使用量与证据；父节点 MUST 在合并前校验声明的输出合同。

#### Scenario: 子任务输出不符合 Schema
- **WHEN** 子 Agent 返回缺少必填字段的结果
- **THEN** 父节点拒绝合并并产生 validation_failed 结果

### Requirement: 多 Agent 恢复不得重复已完成副作用
Orchestrator MUST 在 Checkpoint 恢复时复用已完成节点和工具 Receipt，仅重新调度未完成或按策略可重试的节点。

#### Scenario: 父进程在部分完成后重启
- **WHEN** 一个子任务已完成、另一个仍在运行时父进程重启
- **THEN** 恢复后已完成子任务不再执行，未完成节点从一致状态继续或重试
