# Durable Harness Checkpoint 与跨进程会话恢复

## Why

Harness 的会话恢复/Checkpoint 目前分三层：多轮历史（平台 EventStore，已支持）、进程内 Checkpoint 与审批恢复（Managed LangGraph 内存 Checkpointer，已支持）、重启后的跨进程持久恢复（未打通）。内核侧持久化基建已齐备——`engine.attach()` 跨进程重建、`_is_durable` 探测、SQLite/Postgres Checkpointer、`DeploymentRunStore`/`ToolReceiptStore`、`KSADK_CHECKPOINT_DSN` 装配范式与跨进程恢复测试（`test_managed_langgraph_engine`）——但 DSH Provider 路径仍硬编码内存 Checkpointer（`harness_managed.py`），Studio 能力目录对 harness 的声明也停留在硬编码的 `not_supported`，与 Adapter 真实声明不一致。收口到已接入 DSH 的 Managed 路径，即可用最小改动补齐持久恢复，避免为 native 路径另造一套恢复语义。

## What Changes

- DSH Provider 的 Managed 引擎装配（`ksadk/plugins/providers/harness_managed.py`）按 `runtime_server` 既有范式接入持久 Checkpointer：本地 Studio 每 Workspace 独立 SQLite Checkpointer（`checkpoints.sqlite`）；云端部署经 `KSADK_CHECKPOINT_DSN` 使用 PostgreSQL；无状态目录时保持内存行为不变。
- 为 DSH Provider 路径补齐跨进程重连：持久化 RunHandle（`native_ref.thread_id`）与事件投影，重启后按 `native_ref.thread_id` 经 `engine.attach()` 重建内部 Handle 并恢复执行。
- Checkpoint 后端、RunHandle 索引（`DeploymentRunStore`）与 ToolReceiptStore 使用一致的租户/Agent/Session/Run 命名空间，落在同一 Workspace 状态目录。
- Studio Runtime Catalog（`ksadk/studio/runtime_catalog.py`）改为读取 RuntimeAdapter 的真实能力声明（含 durable 探测），不再硬编码 harness 的 resume/checkpoint。
- 新增跨进程 E2E：进程 A 运行到审批中断 → 关闭进程 → 进程 B 加载同一 Build 与 Checkpoint → 恢复执行且工具不重复调用。

不改变：native 直连会话路径（`_build_direct_backend`）的执行语义与一次性会话行为；MemorySaver 作为无状态回退的合法性；LangGraph 图结构与既有 resume 审批协议。

## Capabilities

### New Capabilities

- `harness-durable-recovery`: Harness 托管路径的持久 Checkpoint 与跨进程恢复契约——Checkpointer 装配分档（Postgres/SQLite/内存回退）、RunHandle 持久化与重连、命名空间一致性、能力声明真实性。

### Modified Capabilities

<!-- 无：以新 capability 承载，避免与尚未从 complete-ksadk-harness-core 同步的主 spec 产生 delta 冲突。 -->

## Impact

- 代码：`ksadk/plugins/providers/harness_managed.py`（Checkpointer/RunStore/Receipt 装配与重连）、`ksadk/studio/runtime_catalog.py`（能力声明读取）、必要时 `ksadk/studio/plugin_runtime.py`（Provider 激活持有状态目录信息）。
- 测试：`tests/plugins/`（Provider 持久装配与重连）、`tests/harness/`（复用/扩展 `test_managed_langgraph_engine` 跨进程模式）、`tests/studio/`（目录能力读取）按 TDD 先红后绿。
- 用户可见：Studio 中 Harness Agent 的审批挂起在进程重启后可恢复；能力目录如实展示 resume/checkpoint 状态。
