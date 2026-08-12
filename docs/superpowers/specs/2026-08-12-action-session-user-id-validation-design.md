# Action Session User ID 校验调整设计

## 背景

`_require_action_session` 当前在读取 action session 时，同时校验 session 是否存在、`agent_id` 是否匹配以及 `user_id` 是否匹配。调用方传入的 `UserId` 可能与 session 中记录的 user id 不同，但该差异不应导致 session 被视为不存在。

## 目标

- `_require_action_session` 不再强制要求请求中的 user id 与 session 中记录的 user id 相等。
- 保留 session 存在性校验和 `agent_id` 范围校验。
- 不改变列表接口等其他位置独立执行的 user 过滤逻辑。

## 实现方案

保留 `_require_action_session` 的 `user_id` 参数和所有现有调用方式，以控制改动范围并维持调用兼容性；仅移除该方法内部基于 `session.user_id` 和 `user_id` 的拒绝分支。同步更新方法说明，使其不再声称校验所有传入范围。

## 测试方案

将现有“指定 SessionId 时 user id 不匹配返回 404”的测试改为验证：

1. session 属于目标 agent，但存储的 user id 与请求 `UserId` 不同；
2. `ListSessionEvents` 仍返回 200；
3. 返回事件来自指定 session。

测试先在旧实现上运行并确认因 404 而失败，再修改生产代码并重新运行相关测试。该测试可防止未来重新引入 `_require_action_session` 的 user id 强制比较。

## 非目标

- 不删除请求模型或调用链中的 `UserId` 字段。
- 不放宽 `agent_id` 匹配规则。
- 不调整无 `SessionId` 场景下按 user id 过滤列表结果的行为。
