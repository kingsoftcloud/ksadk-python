# Design: Durable Harness Checkpoint 与跨进程会话恢复

## Context

实测报告（`docs/2026-09-02-harness-studio-e2e-report.md` §4.3）确认 Harness 恢复能力三层现状。内核基建已完备，逐项核实：

- **引擎层**：`ManagedLangGraphEngine.attach()`（`ksadk/harness/engine/langgraph.py:281`）从 durable Checkpointer 的 graph snapshot 推断状态（未决 interrupt → awaiting_approval；pending 节点 → paused），幂等（已持有 `_runs` 直接返回）；`_is_durable`（`langgraph.py:936`）排除 `MemorySaver`；`sqlite_checkpointer`（AsyncSqliteSaver）与 `postgres_checkpointer(dsn)` 均已实现。
- **服务层范式**：`runtime_server.initialize()`（`ksadk/harness/runtime_server.py:155-185`）已是完整装配范式——DSN → Postgres；有 `state_dir` → SQLite（`checkpoints.sqlite`）+ `ToolReceiptStore`（`tool_receipts.sqlite`）+ `DeploymentRunStore`（`runs.json`，持久化 RunHandle 与事件投影）；皆无 → 内存。
- **测试范式**：`tests/harness/test_managed_langgraph_engine.py:455-463` 走正式 `attach()` 的跨进程恢复测试；`test_runtime_server` 验证 Run 索引在 Runtime 对象重建后仍可恢复。
- **缺口**：DSH Provider 装配（`ksadk/plugins/providers/harness_managed.py:71`）硬编码 `memory_checkpointer()`；`runtime_catalog.py:11` 对 harness 硬编码 `resume/checkpoint: not_supported`。

## Goals / Non-Goals

**Goals**

- DSH Provider 的 Managed 引擎按 runtime_server 范式装配持久 Checkpointer（SQLite/Postgres/内存回退三档）。
- Provider 路径的 RunHandle 持久化与跨进程重连（attach → resume/stream）。
- Checkpoint/Handle/Receipt 命名空间一致并按 Workspace 隔离。
- Studio 能力目录读取 Adapter 真实声明。
- 跨进程审批恢复 E2E（含工具不重复执行断言）。

**Non-Goals**

- 不改 native 直连会话路径（`_build_direct_backend`）的执行语义；其一次性会话行为与既有恢复语义无关。
- 不新增 Checkpoint 后端实现（复用内核 SQLite/Postgres/Memory 三档）。
- 不改 LangGraph 图结构、审批协议与事件 schema。
- 不在本 change 内做云端部署编排（Postgres DSN 的供给由部署侧负责）。

## Decisions

### D1. 装配收口到既有范式，不新建抽象

`build_managed_provider_adapter` 增加可选 `state_dir`/DSN 入参，内部按 `runtime_server.initialize` 同款三分支选择 Checkpointer，并同步装配 `DeploymentRunStore` 与 `ToolReceiptStore`（同一 `state_dir`）。理由：`runtime_server` 的装配已被测试覆盖，复制该范式（或抽取共享 helper）都远小于引入新配置层的成本；优先抽一个共享装配 helper 供 runtime_server 与 provider 共用，避免两处分叉。

### D2. 重连以 `native_ref.thread_id` 为锚，走正式 `attach()`

Provider 激活时若 `DeploymentRunStore` 存在同 Agent/Session 的持久 Handle，则经 `engine.attach(handle, compiled)` 重建（幂等由 attach 自身保证），不手工篡改内部状态——与 `test_managed_langgraph_engine` 的正式路径一致。`durable_restore` 语义由 attach + DeploymentRunStore.load 组合承载，不新增第二套恢复入口。

### D3. 状态目录归属 Workspace

本地 Studio 的 `state_dir` 取 `<workspace>/.agentkit/plugin-runtime/state`（与现有 builtin capability factories 的 state 根一致），天然满足每 Workspace 隔离与命名空间一致；`DeploymentRunStore`/`ToolReceiptStore`/`checkpoints.sqlite` 同目录。云端经 DSN 共享 Postgres，RunStore/Receipt 的租户维度沿用现有键（不改变存储 schema，仅保证装配传参一致）。

### D4. 能力目录读真实声明

`runtime_catalog` 对 harness 不再查静态表：从 Provider 激活/装配产物读取能力声明（含 `durable_across_process`），无激活信息时按装配档位推导（内存回退 → 不支持跨进程恢复）。ADK/LangGraph/Codex 等外部 Runtime 声明维持静态表（它们无本 change 引入的动态性）。

## Risks / Trade-offs

- SQLite Checkpointer 并发写：本地单进程 Studio 场景无冲突；文档标注"云端多进程必须用 Postgres DSN"。
- Provider 激活持有 Workspace 状态目录路径：需从 `StudioPluginRuntime` 传入（builtin capability factories 已使用同一 state 根，来源现成）。
- attach 依赖 Checkpointer 与 RunStore 版本一致：恢复失败时必须诚实报错（引擎 attach 已实现），不得静默降级为内存重建。
- 共享装配 helper 的抽取会触碰 `runtime_server`：小步重构 + 既有测试护航。

## Migration Plan

纯加法装配：无 state_dir/DSN 的部署行为不变（内存回退）；本地 Studio 获得默认 SQLite 持久化。无数据迁移；旧会话（无持久 Handle）按现状重放上下文。

## Open Questions

无（云端 RunStore 的多实例共享如需 runs.json 之外的后端，留待云端部署 change 处理，本 change 以 DSN Postgres Checkpointer + 单激活进程为边界）。
