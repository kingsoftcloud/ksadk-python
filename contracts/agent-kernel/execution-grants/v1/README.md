# 通用执行准入授权 execution-grants/v1

本契约为 AgentControl/v1 的可选扩展；原 v1 schema、digest 和无 grant 的调用行为保持兼容。它不定义群、Leader 或任务调度规则。Memory、SQLite、PostgreSQL 使用同一状态与回执模型；后两者持久保存授权与操作回执。

## 宿主接口

`ksadk.kernel.ExecutionGrantSpec` 包含 `grant_id / tenant_id / agent_instance_id / session_id / owner_ref`。标识由可信宿主生成，精确绑定执行主体和会话。一个长期会话可有多个授权，例如不同的定时任务 occurrence 或其他调用域的一次执行。`owner_ref` 是经宿主验证的授权主体或 authority 引用，不是模型参数。

通过 `AgentKernel` 或对应控制存储调用：

```python
record = await kernel.ensure_execution_grant(spec)
receipt = await kernel.set_execution_grant_state(
    spec, "suspended", expected_revision=record.revision,
    idempotency_key="stable-operation-id",
)
current = await kernel.get_execution_grant(spec)
```

这些是可信宿主端口，宿主负责认证、授权、撤权及业务 scope 校验。不能直接向浏览器或模型暴露其任意 scope 参数。每次状态写入仍验证完整 spec 与原记录一致；授权不能换 owner 或重绑 session。`ensure` 重复返回现状，不会重置 suspended/revoked。

执行请求在 `AgentControlCommand.payload` 附带非空 `execution_grant_id`。只有 enqueue 可携带该字段。授权不替代原 AgentControlPermit；permit 和 grant 都须有效。宿主对要求可撤销的工作必须附带该引用，不允许从不可信参数中删除或覆盖。

## 状态与屏障

- `active`：允许取得启动资格。
- `suspended`：保留已接收未取得资格的原 Inbox 命令；恢复 active 后处理同一 command/message，不重新 enqueue。已取得资格的工作仍为 in-flight。暂停期间的持久投递重试可进入队列，但不能启动。
- `revoked`：不可逆。状态变更与所有相关 accepted 命令的 discarded 状态在同一事务提交。未取得资格的旧命令不能启动；之后的新 enqueue 被拒绝。重试已接收的命令仍返回原 duplicate receipt，其当前状态须查证。

**Inbox claim 是取得启动资格的线性化点。** SQLite 的 grant 检查、claim 与 grant 状态变更使用同一个 `BEGIN IMMEDIATE` 控制库事务；Memory 使用同一个 session 锁；PostgreSQL 先锁 grant 行，再锁 Inbox 行。没有跨库、进程内检查后放行的替代路径。

若 claim 先于撤销提交，该命令已经取得资格，归入 in-flight，即使此时 adapter.start 尚未返回。宿主必须继续查询并取消其 Run，直到真实终态。撤销回执不是“所有模型已经停止”的证明。运行时不支持取消或远端还未确认的工作保持其真实状态。

`ExecutionGrantBarrier` 含授权状态与 revision，以及：

- `queued_message_ids`：尚未取得资格，状态 accepted。
- `in_flight_message_ids`：已取得资格，尚无可验证的 Run 终态；包括 Inbox completed 但模型仍运行的情况。
- `discarded_message_ids`：未执行或被明确丢弃的命令。
- `settled_message_ids`：关联 Run 已有持久终态。
- `commands`：对应的 command/message/idempotency/Inbox 状态和已知 Run ID/状态，供宿主核对和精确控制。

相同 `grant_id + idempotency_key`、相同请求内容返回原持久回执，即使后来状态已改变；同 key 不同内容拒绝。`expected_revision` CAS 拒绝过期写入。丢失回执时用同 key 重试；用 `get` 获取当前状态。不能将历史操作回执当作当前快照。

## 恢复与不确定性

授权状态、队列废弃和回执同库提交。SQLite 重启先初始化 schema，再读取已有 grant；不能重建为 active。撤销后，旧 owner 的过期 fence 仍被拒绝；新 owner 可接回撤销屏障前已经取得资格的命令。

携带 grant 的命令使用稳定 Run ID。若崩溃发生在 provider.start 可能已接受调用、但 native handle 尚未持久化的窗口，重试不会新建第二个 Run，而将原 pending Run 标为 interrupted，原因 `execution_start_uncertain`，交由宿主显示需处理。已有持久 handle 的恢复仍由原 Provider/Kernel recovery 处理。授权和 fencing 不承诺外部工具副作用 exactly-once。

SessionEventStore 可与 SQLite 控制库分离，因此 **屏障真相在控制库及其持久回执**，不以事件订阅是否收到某条日志为判断依据。群事件或其他业务投影可以引用此回执，不能反过来修改其执行资格。

## 验证

```sh
uv run --no-sync pytest tests/kernel/test_execution_grants.py tests/studio/test_scheduler_runtime.py
```

确定性用例包括 check/claim 之间停住执行、另一 SQLite 连接撤销、启动先于屏障、事务回滚、ACK 丢失、恢复同一命令、授权边界、未知启动结果以及无 grant 的旧调用。

设置专用测试变量 `KSADK_TEST_GRANTS_POSTGRES_DSN` 时，共用控制存储用例会追加真实 PostgreSQL 后端，在随机命名的独立 schema 内运行并清理；未配置时不连接数据库，也不将 Memory/SQLite 通过计为 PostgreSQL 验证。该集成测试隔离控制存储，完整 PostgreSQL SessionEvent、部署与端云恢复仍需相应环境的 E2E。
