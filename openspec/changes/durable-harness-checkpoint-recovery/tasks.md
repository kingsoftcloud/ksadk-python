# Tasks

## 1. 抽取共享 Checkpointer/状态装配 helper（TDD：先红后绿）

- [x] 1.1 阅读并固化 `runtime_server.initialize` 的三分支装配行为基线（DSN→Postgres / state_dir→SQLite+RunStore+Receipt / 内存回退），确定共享 helper 的签名与归属模块
- [x] 1.2 写失败测试：helper 按三分支返回正确的 Checkpointer 类型与 durable 探测结果（Postgres 分支可用 fake/跳过标记）
- [x] 1.3 实现 helper（`runtime_server` 改为复用），既有 `test_runtime_server` 全绿

## 2. DSH Provider 持久装配（TDD）

- [x] 2.1 写失败测试：`build_managed_provider_adapter` 传入 Workspace `state_dir` → 引擎 Checkpointer 为 SQLite 文件、`durable_across_process=true`、同目录出现 `runs.json` 与 `tool_receipts.sqlite`；不传 → 内存回退且行为与现状一致
- [x] 2.2 写失败测试：`StudioPluginRuntime` 激活 DSH provider 时传入 `<workspace>/.agentkit/plugin-runtime/state`（与 builtin capability factories 同根）
- [x] 2.3 实现（`harness_managed.py` 装配 + `plugin_runtime` 传参），测试转绿；`test_scheduler_dsh_harness_product` 全绿（不回归）

## 3. 跨进程重连（TDD）

- [x] 3.1 写失败测试：进程内运行至审批中断 → 释放引擎实例 → 新建引擎（同一 state_dir）经持久 Handle `attach()` → 状态为 `awaiting_approval` → 提交决策后恢复至完成（复用 `test_managed_langgraph_engine:455` 的正式 attach 模式）
- [x] 3.2 写失败测试：已持有活跃状态的引擎重复 `attach` 同一 Handle → 幂等返回，事件不重复
- [x] 3.3 写失败测试：恢复后事件流不含对同一 `call_id` 的第二次工具执行，Receipt 不重复记账
- [x] 3.4 实现 Provider 侧重连入口（激活时检查 DeploymentRunStore → `engine.attach`），测试转绿

## 4. 能力目录真实化（TDD）

- [x] 4.1 写失败测试：持久装配下 `runtime_catalog` 对 harness 报告 resume/checkpoint 支持；内存回退下如实报告不支持跨进程恢复；外部 Runtime（adk/langgraph/codex）静态声明不受影响
- [x] 4.2 实现 `runtime_catalog` 读取 Adapter 能力声明，测试转绿

## 5. E2E 与收尾

- [x] 5.1 跨进程 E2E：进程 A（Studio/Provider）运行到审批中断 → 退出 → 进程 B 加载同一 Build 与 state → 恢复执行且工具不重复调用（脚本化双进程或 pytest 两阶段）
- [x] 5.2 受影响面全量回归：`pytest tests/harness tests/plugins tests/studio -q` 全绿
- [x] 5.3 更新实测报告 §4.3 三层现状表（跨进程恢复层转为 ✅）与能力矩阵图
- [x] 5.4 `openspec validate` 通过；受影响面回归 1355 passed（唯一失败为既有测试未适配 builder 异步化，已同步修复）；跨进程审批恢复 E2E 复用既有 subprocess fixture（test_lifecycle_process），Provider 路径以两实例 restore 测试覆盖
