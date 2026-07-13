# ADK invocation resume 加固设计

## 背景与范围

`ADKRunner` 已接入框架无关的 checkpoint/resume 协议，但 ADK 原生恢复只能按
`invocation_id` 向前续跑，不能像 LangGraph 一样选择任意历史 checkpoint 回档。
本设计修复 SDK 侧的并发串用、缺失引用降级为新任务、能力误报、重复审计、空事件异常
和 LiteLLM JSON 补丁失效问题。

本次只修改 `ksadk-python`。`agentengine-server` 的控制台/KOP 适配作为独立交接项，
不在本次代码改动范围内。

## 目标与非目标

目标：

- 对同一 ADK invocation 只暴露一个最新、非终态的可恢复点。
- 恢复引用缺失或不可恢复时安全失败，绝不创建新 invocation。
- 能力声明与 session backend 的实际持久性一致。
- 运行时恢复审计事件只由 conversation runtime 写入一次。
- 以回归测试覆盖本次修复的行为边界。

非目标：

- 不实现 ADK 的任意历史 checkpoint 回档。
- 不升级或兼容 Google ADK 2.x。
- 不改动 `agentengine-server`、控制台或 `ksadk-web`。
- 不把 Skill、Sandbox 或部署路径纳入本次改动。

## 方案选择

选择保留 checkpoint 审计历史、在读取时投影恢复资格的方案。

每个 ADK 可恢复边界仍记录 `run_checkpoint`，用于诊断、预览和审计；但同一
`RunId` 的 ADK checkpoint 中，只有最后一个非终态记录可以作为 `invocation_id`
恢复入口。较早记录在列表、预览和恢复解析时统一标为不可恢复，并说明 ADK 仅支持
从 invocation 的当前状态继续执行。

这比只保存一个 checkpoint 更兼容现有 API，也不会把 ADK 的 forward-only 语义伪装成
时间回溯。

## SDK 设计

### Invocation 映射与并发

删除 runner 实例级的 `_last_adk_invocation_id`。每个 `_collect_*` 异步迭代器持有自己的
局部 ADK invocation id，checkpoint 仅使用该局部值。

持久化映射采用 session binding 的增量 state update：仅写入当前
`ksadk_invocation_id -> adk_invocation_id` 键，保留同一 session 内其他映射。若存储层
不能保证该更新的原子性，映射异常必须可见于日志，且恢复路径不得将其转换为新任务。

### 恢复资格与失败语义

ADK checkpoint metadata 写入 `backend`、`scope`、`durable` 与 `resume_mode=invocation_id`。
checkpoint 读取投影根据相同 run 的顺序和终态状态决定资格：旧点、终态点、进程内点均
不可恢复，并返回确定的原因。

恢复时只能使用由 checkpoint 存储提供的 `framework_ref.adk.invocation_id` 或同一 session
的持久映射。两者均缺失时抛出明确的 checkpoint-resume 错误；不得传入空消息调用
`run_async()`。恢复后 ADK 若产生零事件，`invoke()` 返回空输出和空事件列表，`stream()`
正常结束，不能访问未初始化的循环变量。

### 能力与连续性

`describe_checkpoint_capability()` 根据实际 ADK session backend 生成 backend、scope、
durable 与 SharedAcrossPods。内存/本地 backend 的 checkpoint 为 `process_local`：不声明
runtime checkpoint resume，SessionContinuity 降为 semantic。持久 backend 才可以声明
runtime 级 invocation resume；是否跨 pod 由 backend 的共享能力单独表达。

`ADKSessionAdapter` 使用同一规则，避免它与 `get_runtime_capabilities()` 给出矛盾等级。

### 事件与补丁

恢复审计的唯一 owner 是 conversation runtime；删除 `ADKRunner.invoke()` 与 `stream()`
中重复的 `append_run_resume_event()`。

LiteLLM 的消息转换包装器只围绕原函数调用捕获 `JSONDecodeError`，在最终工具参数无法
解析时生成安全的空参数结果或保留可处理的响应；保留 stdlib JSON 对流式片段完整性的
判定。JSON 与 MCP monkey patch 增加幂等标记，避免重复 `load_agent()` 叠层包装。

## 测试与验证

先添加失败测试，再作最小实现：

- 两个 session 交错事件时 checkpoint 使用各自的 ADK invocation id。
- 缺失 ADK 引用时 invoke/stream 失败且从未调用新 invocation。
- ADK 历史 checkpoint 被拒绝，最新非终态 checkpoint 可被解析。
- 空事件恢复不抛出 `UnboundLocalError`。
- memory/local backend 的 capability 与 continuity 降级；持久 backend 保持 runtime。
- 一次 checkpoint 恢复只产生一条 `run_resume` 审计事件。
- LiteLLM JSON 异常按包装器定义处理，重复 patch 不叠层。

验证顺序为：新增与受影响的单元测试、`ruff check`、相关 server/conversation 测试；若
具备可用 ADK 凭证与持久 backend，再运行真实中断恢复 E2E。缺少这些外部条件时，明确
记录而不把单测当作 E2E。

## 服务端交接项（不在本仓实现）

`agentengine-server` 需要：

1. `GetAgentUiBootstrap` 依据 runtime capability（而非 framework 名）显示恢复入口，
   获取失败时 fail closed。
2. `ResumeRun(Stream=true)` 仅在 runtime 上游响应为 `text/event-stream` 时包装 SSE；
   终态 checkpoint 的 JSON noop 按普通 ActionResponse 返回。
3. `SubscribeRunEvents` 代理到与 `ResumeRun` 相同的 runtime 事件源，并支持按
   `InvocationId` 和 `AfterSeqId` 续订。

控制台只提交 AgentId、SessionId、RunId、CheckpointId、ResumeAttemptId 与 InvocationId；
不得接受浏览器传入的 framework ref、ADK invocation id 或 cookie。
