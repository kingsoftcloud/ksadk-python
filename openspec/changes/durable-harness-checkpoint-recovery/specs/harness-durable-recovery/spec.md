# Harness 托管路径持久 Checkpoint 与跨进程恢复

## Purpose

定义 KsADK Harness 托管（Managed LangGraph）路径的持久 Checkpoint 与跨进程恢复契约：Checkpointer 按部署形态分档装配、RunHandle 可持久化并在新进程重建、状态索引命名空间一致、对外能力声明必须反映真实（含 durable 与否），使审批挂起等中断状态能在进程重启后恢复且不产生重复工具调用。

## ADDED Requirements

### Requirement: Checkpointer 必须按部署形态分档装配

Provider 装配 Managed 引擎时 SHALL 按以下优先级选择 Checkpointer：显式 DSN（如 `KSADK_CHECKPOINT_DSN`）→ PostgreSQL；有 Workspace 状态目录 → 每 Workspace 独立 SQLite；两者皆无 → 内存 Checkpointer（现状回退，能力声明如实标注非 durable）。

#### Scenario: 本地 Studio 默认装配

- **WHEN** 本地 Studio 以 Workspace 状态目录装配 DSH Provider 的 Managed 引擎
- **THEN** Checkpointer 为该 Workspace 独有的 SQLite 文件，且 Capability 声明 `durable_across_process=true`

#### Scenario: 云端 DSN 装配

- **WHEN** 部署环境提供 Checkpoint DSN
- **THEN** Checkpointer 为 PostgreSQL，多个进程/实例共享同一持久后端

#### Scenario: 无状态目录回退

- **WHEN** 无 DSN 且无状态目录
- **THEN** 装配内存 Checkpointer，行为与现状一致，能力声明标注不支持跨进程恢复

### Requirement: RunHandle 必须可持久化并在新进程重建

运行产生的 Handle（含 LangGraph `thread_id`）与状态投影 SHALL 持久化；新进程加载同一 Build 后，SHALL 能按持久化 Handle 经 `engine.attach()` 重建内部运行态并继续 `resume()`/`stream()`，恢复过程不得要求重放已完成的事件。

#### Scenario: 审批中断后跨进程恢复

- **WHEN** 进程 A 中运行进入审批中断（awaiting_approval）后进程退出，进程 B 加载同一 Build 与持久化 Handle
- **THEN** `attach` 后运行状态为 `awaiting_approval`，提交审批决策后恢复执行至完成

#### Scenario: 重复 attach 幂等

- **WHEN** 同一 Handle 在已持有活跃状态的进程内再次 attach
- **THEN** 返回既有内部运行态，不重建、不产生重复事件

### Requirement: 恢复后工具调用不得重复

审批中断前已执行的工具调用 SHALL 通过既有 ToolReceipt/工具配对机制被识别；恢复执行后不得对同一 `call_id` 重复执行工具或重复记账。

#### Scenario: 恢复后继续而非重做

- **WHEN** 审批中断前工具已执行并记账，恢复后图从 Checkpoint 快照继续
- **THEN** 后续事件流不出现对同一 `call_id` 的第二次工具执行事件，Receipt 记账不重复

### Requirement: 状态命名空间必须一致

Checkpoint 后端数据、RunHandle 索引与 ToolReceipt 记账 SHALL 使用一致的 Workspace（租户）/Agent/Session/Run 命名空间，确保按 Handle 恢复时三者可互相关联且不跨 Workspace 串扰。

#### Scenario: 跨 Workspace 隔离

- **WHEN** 两个不同 Workspace 各自运行同名 Agent 与 Run
- **THEN** 任一 Workspace 的恢复操作只能访问本 Workspace 的 Checkpoint、Handle 索引与 Receipt

### Requirement: 对外能力声明必须反映真实

对外暴露的 Runtime 能力目录 SHALL 来自 RuntimeAdapter 的实际能力声明（含 durable 探测结果），不得对 resume/checkpoint 使用与实现不符的硬编码值。

#### Scenario: 目录展示持久恢复能力

- **WHEN** Provider 以持久 Checkpointer 装配完成
- **THEN** 能力目录对 harness 报告 resume/checkpoint 支持（含 durable 语义）；以内存回退装配时如实报告不支持跨进程恢复
