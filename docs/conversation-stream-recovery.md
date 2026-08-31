# 对话流断线恢复

声明 `ConversationSurface/v1` 的本地 Agent 使用可回放的
`ConversationItem/v1` 事件流。Studio 在一次发送已经创建 Run 后，如果连接中断或流提前结束，
会使用最后一个 SSE 游标读取同一 Run 的后续事件，不会再次发送创建 Turn 的请求。

这保证模型调用、工具调用和审批等副作用只属于原来的 Run。重放边界上的 Item 按
`itemId + sourceEventId` 幂等归并；正文相同但身份不同的 Item 仍会分别保留。

恢复有以下边界：

- 只有服务端返回过合法 `ConversationItem`、能够确认权威 `runId` 后才自动续流；
- 自动续流最多尝试八次，退避时间从 200 毫秒增长到 2 秒；
- 用户取消会立即终止等待和后续读取；
- 到达 `completed`、`failed` 或 `cancelled` 终态后不再重连；
- 超过重试上限时，Run 仍可在后台继续，用户可刷新会话读取持久事件；
- 没有声明 `ConversationSurface/v1` 的历史 Agent 保持原有单次 Responses 流行为，
  不猜测新协议的 Run 或游标。

该机制只负责同一页面内的网络断线续流。浏览器整页关闭后的运行恢复依赖服务端持久事件；
客户端重新打开会话后读取历史状态，而不是重发原始消息。
