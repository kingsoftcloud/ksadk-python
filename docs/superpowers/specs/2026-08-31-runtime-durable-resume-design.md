# Runtime 持久化恢复修复设计

## 背景

Runtime v2 将跨进程恢复建模为 `attach` 后再 `resume`。当前 ADK 和 LangGraph runner 能消费持久化 checkpoint，但没有实现 `attach_runtime_handle()`。服务重启后，`RuntimeExecutor` 无法找回内存 handle，恢复请求会在真正进入框架恢复逻辑前失败。

`ResumeRun` 的流式入口还存在两个错误传播缺口：`Stream=true` 创建 detached stream 时没有传递 `session_id`，后台异常无法写入终态；`Background=true` 仍引用已移除的 legacy `active_runner`。结果是接口可能已经返回成功并写入 `resuming`，但后台执行失败且会话永久停留在恢复中。

## 目标

- ADK 和 LangGraph 在共享、持久化 backend 就绪时支持服务重启后的真实恢复。
- 同步、`Stream=true` 和 `Background=true` 使用同一套 RuntimeExecutor 恢复语义。
- 恢复能力声明与实际 attach 行为一致；SQLite、memory、local 或 degraded backend 不得被误报为跨进程恢复。
- detached 执行无论在哪个阶段失败，都必须持久化 `failed` 或 `cancelled` 终态。
- 不改变公开 API 字段、版本号或发布记录。

## 方案

### Runner 原生 attach

ADKRunner 和 LangGraphRunner 分别实现 `attach_runtime_handle(handle)`，custom runner 通过继承获得相同行为。

attach 只接受与 runner 框架匹配、包含完整持久化引用的 handle：

- LangGraph 要求 `checkpoint_id` 和 `thread_id`，并要求 checkpoint capability 为 supported、durable、shared。runner 准备 managed checkpointer 后验证 checkpoint 能被当前 graph/checkpointer 解析。
- ADK 要求 `invocation_id`，并要求 resumability 已启用、共享数据库 session backend 就绪。ADK 的 invocation 由 session service 在真实 `run_async` 阶段解析；attach 负责验证部署能力和引用完整性，不伪造 invocation 已存在。

attach 返回成功后，RunnerRuntimeAdapter 才将 handle 纳入 `_known_runs`。失败保持 fail closed，并保留原异常原因供 detached 终态和日志使用。

RunnerRuntimeAdapter 的 capability matrix 继续以 callable attach seam 加持久化 checkpoint capability 计算 `attach` 和 `durable_restore`，因此声明和执行使用同一来源。

### ResumeRun 路径统一

- `Stream=true` 继续使用 `stream_runtime_responses_conversation_turn()`，但向 detached response 传入 `session_id`。
- `Background=true` 移除 legacy `active_runner` 调用，改用同一个 RuntimeExecutor stream source，并传递 `session_id`、resume key、run mode 和 trigger。
- 同步路径保持 `invoke_runtime_conversation_once()`，与后台路径共享 `_resume_runtime_handle()`。
- 起始 `resuming` 事件维持当前幂等语义；真正执行阶段的异常由 detached stream 写入终态。

### 错误与状态语义

- 数据库不可达、认证失败、依赖缺失、引用缺失和 checkpoint 不存在均 fail closed。
- 已经返回 HTTP 200/202 的 detached 请求，通过 session event 暴露最终失败；不会继续停留在 `resuming`。
- 同步请求在 attach 或 resume 失败时返回对应 HTTP 错误，不伪装成功。
- resume key 在 detached task 完成或失败后释放，允许修复配置后重试。

## 测试设计

先增加会在当前实现上失败的行为测试：

1. 新建 RuntimeExecutor 模拟进程重启，LangGraph 使用持久化 checkpoint ref attach 并恢复。
2. ADK 使用共享数据库和 invocation ref attach 并恢复。
3. LangGraph/ADK 在 SQLite、memory、local、degraded 或引用缺失时拒绝跨进程 attach。
4. custom LangGraph runner 不需要重复实现 attach。
5. `ResumeRun Stream` 的 detached 异常写入 `failed`，不残留 `resuming`。
6. `ResumeRun Background` 使用 RuntimeExecutor，能够成功执行且不存在 `active_runner` NameError。
7. 三种 ResumeRun 模式继续校验 checkpoint 归属、具体 checkpoint 可恢复性和并发 resume key。

完成单测后运行 server/runtime/session 受影响测试集，并用 `examples/deepsearch-langgraph` 的实际 PostgreSQL 配置执行一次服务重启后恢复。ADK 仅在本机具备可用 PostgreSQL session backend 和依赖时执行真实 smoke；缺失条件会明确记录。

## 非目标

- 不为 BaseRunner、Harness 或不具备共享持久化的 runner 增加 durable restore。
- 不改变 checkpoint list、preview 或 ResumeRun 的公开 schema。
- 不自动清理历史数据库中的旧 `resuming` 记录；若需要 reconciliation，将作为独立行为设计。
- 不改版本、CHANGELOG 或执行发布。
