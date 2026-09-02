# Tasks

## 1. Studio 用量上报（TDD：先红后绿）

- [x] 1.1 在 `tests/studio/` 找到/新建 `_StudioHarnessReasoner`（或经 `StudioPluginRuntime` 可达入口）的单测落点；写失败测试：伪造 `ModelResponse`（usage 非零，含 cached/reasoning）→ 断言 reasoner 返回的 `HarnessReasoningTurn.usage` 四键映射正确
- [x] 1.2 写失败测试：`response.usage` 为缺失态时，turn.usage 不得是伪造的全零 dict（保持 None/缺失语义）
- [x] 1.3 实现 `_StudioHarnessReasoner.complete` 的 usage 映射（design D1），两个测试转绿
- [x] 1.4 链路级测试：经 `StudioPluginRuntime`/`_persist_plugin_result_events` 断言 completed 运行含非零 `usage.reported` 且 `record.usage.reported=True`（复用 `tests/studio/test_scheduler_dsh_harness_product.py` 的 fake reasoner 模式，让 fake 携带 usage）
- [x] 1.5 全量搜索测试中依赖"用量恒 0"的既有断言并同步更新

## 2. DSH 插件源路径解析（TDD）

- [x] 2.1 在 `tests/plugins/` 写失败测试：相对路径源在 bridge cwd 下存在时，`_prepare_source`/`install_plugin` 产出绝对路径源（monkeypatch cwd 到临时目录，避免依赖仓库布局）
- [x] 2.2 写失败测试：相对 `.tgz` 不存在时抛 `DshPluginSourceError`（而非生成 `link:` 依赖）
- [x] 2.3 写回归测试：npm 包名 spec（如 `@scope/pkg@1.0.0`）与非本地 URL 仍按原样传给 dsh，不被绝对化
- [x] 2.4 实现 design D2，三个测试转绿
- [x] 2.5 真实冒烟：`agentengine plugin install ksadk/plugins/providers/bundles/ksadk-harness --accept-host-permissions`（相对路径）在干净 Profile 上成功

## 3. 工具链错误指引（TDD）

- [x] 3.1 在 `tests/cli/` 写失败测试：`DshToolchainVersionMismatchError("expected pnpm 11.7.0, got 11.22.0")` → CLI 错误 details/hint 含期望与实际版本、`AGENTENGINE_PNPM_BIN` 与 corepack 指引
- [x] 3.2 写失败测试：`DshToolchainUnavailableError` → 错误含 `agentengine plugin toolchain install`
- [x] 3.3 实现 design D3（`ksadk/cli/cmd_plugin.py` 错误映射），测试转绿

## 4. 验证与收尾

- [x] 4.1 `pytest tests/studio tests/plugins tests/cli -q`（受影响范围）全绿；再跑 `tests/harness/test_runner_comparison_eval.py tests/studio/test_scheduler_dsh_harness_product.py`
- [x] 4.2 E2E 复验：重启 Studio，重放 `POST /v1/responses`（agentkit-f2b37d5f），确认 `usage.reported` 非零且运行检查器不再显示 Token 0
- [x] 4.3 定向 secret scan（改动文件无 token/凭证）；更新实测报告 §5 问题清单状态（1、3、5 已修复）
- [x] 4.4 `openspec validate` 通过；（提交动作待用户批准后执行）

## 5. 推理内容透出（TDD：先红后绿）

- [x] 5.1 在 `tests/studio/test_model_client.py` 写失败测试：chat-completions 响应 message 含 `reasoning_content` → `ModelResponse.reasoning` 捕获该文本；无 reasoning_content 时为空
- [x] 5.2 在 `tests/studio/test_studio_harness_reasoner.py` 写失败测试：`response.reasoning` 非空 → `turn.reasoning` 透传；为空 → None
- [x] 5.3 在 `tests/harness/` 既有 runtime 流测试落点写失败测试：reasoner 返回 `turn.reasoning` 非空 → `_stream` 产出 `item_kind="reasoning"` 的 ItemStarted/ItemCompleted（文本一致）；为空 → 不产出
- [x] 5.4 实现 design D4（model_client 捕获、reasoner 透传、引擎事件），全部转绿
- [x] 5.5 E2E 复验：Studio 中 Harness 会话出现思考过程（thinking.completed 事件/思考卡片）

## 6. Harness 授权体验（TDD）

- [x] 6.1 在 `tests/studio/` 写失败测试：`create_studio_agent(runtime=harness)` 且不提供 spec → 生成 spec 含 `process:host-user`；显式提供不含该权限的 spec → 不被改写；runtime=codex 默认模板 → 不预置
- [x] 6.2 写失败测试：authoring coordinator `create(runtime_type=harness)` 不提供 spec → spec 含 `process:host-user`
- [x] 6.3 写失败测试：`_preflight_bundle` 权限拒绝错误的 details 含 `permission` 与修复指引文案
- [x] 6.4 实现 design D5，全部转绿；确认 `test_scheduler_dsh_harness_product` 的 denied 用例仍通过（显式 spec 语义不变）
- [x] 6.5 E2E 复验：模板默认创建 harness Agent 可直接运行

## 7. 报告完善与提交

- [x] 7.1 完善 `docs/2026-09-02-harness-studio-e2e-report.md`：§3 增补思考过程展示对比、§5 五项问题终态、§7 验证记录补全
- [x] 7.2 `openspec validate` 通过；受影响测试全绿
- [x] 7.3 分主题提交：docs 报告 399ad413、fix 主提交 60d42fd7（test_managed_runtime_adapter.py 仅暂存本次新增 reasoning 测试，用户既有的 current_input 断言行保留在工作区未提交）
