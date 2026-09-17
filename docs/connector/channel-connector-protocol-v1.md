# Channel Connector Protocol v1

## 概述

Agent Connector 是本地 Agent 运行时与云端 Channel 服务之间的出站 WebSocket 协议。
本地 Agent 主动连接云端，不需要开放本地端口，适合 NAT、内网和桌面环境。

```
IM (WPS/飞书/企微)
  └── agentengine-channel (云端)
        ├── IM 适配器（长连接）
        ├── 绑定关系和路由
        └── Agent Connector Gateway
              ▲
              │ outbound WSS
              │
本地 Agent Runtime (ksadk/connector/channel.py)
```

## 连接

```
wss://{channel-host}/agentengine/connector/v1?token={jwt}
```

- 本地 Agent 主动建立出站 WSS 连接
- `token` 为 JWT，含 `tenant_id`、`workspace_id`、`agent_id`、`exp`
- 连接后客户端必须在 10s 内发送 `register`

## 消息信封

所有消息为 JSON 文本帧：

```json
{
  "type": "register | register_ok | register_err | invoke | event | complete | error | cancel | pong",
  "seq": 1,
  "ts": "2026-09-17T12:00:00Z",
  "payload": { ... }
}
```

- `seq`：发送方递增序号（客户端和服务端各自独立计数）
- `ts`：ISO 8601 UTC 时间戳

## 消息类型

### register（客户端 → 服务端）

连接后第一条消息。服务端在 5s 内回复 `register_ok` 或 `register_err`。

```json
{
  "type": "register",
  "seq": 1,
  "payload": {
    "tenant_id": "tenant-abc",
    "workspace_id": "ws-001",
    "agent_id": "agent-001",
    "capabilities": ["invoke", "stream", "cancel"],
    "protocol_version": 1,
    "sdk": "ksadk/0.8.5"
  }
}
```

### register_ok（服务端 → 客户端）

```json
{ "type": "register_ok", "seq": 1, "payload": { "session_id": "conn-uuid", "heartbeat_sec": 30 } }
```

### register_err（服务端 → 客户端）

```json
{ "type": "register_err", "seq": 1, "payload": { "code": "auth_expired", "message": "token expired" } }
```

服务端 close code 4001 = 认证失败，4002 = agent_id 重复。

### invoke（服务端 → 客户端）

IM 消息到达，需要 Agent 处理。

```json
{
  "type": "invoke",
  "seq": 2,
  "payload": {
    "task_id": "task-uuid",
    "session_id": "im-session-001",
    "channel": "feishu",
    "message": "你好",
    "deadline": "2026-09-17T12:05:00Z",
    "idempotency_key": "idem-uuid"
  }
}
```

### event（客户端 → 服务端，流式响应）

```json
{ "type": "event", "seq": 3, "payload": { "task_id": "task-uuid", "event": "delta", "text": "你好" } }
```

### complete（客户端 → 服务端）

```json
{ "type": "complete", "seq": 4, "payload": { "task_id": "task-uuid", "result": "你好！有什么可以帮你的？" } }
```

### error（客户端 → 服务端）

```json
{ "type": "error", "seq": 5, "payload": { "task_id": "task-uuid", "code": "timeout", "message": "execution exceeded deadline" } }
```

### cancel（双向）

```json
{ "type": "cancel", "seq": 6, "payload": { "task_id": "task-uuid", "reason": "user cancelled" } }
```

### ping / pong（双向心跳）

```json
{ "type": "ping", "seq": 7, "payload": {} }
```

服务端 `heartbeat_sec` 间隔发送 ping，客户端 5s 内回复 pong。30s 无 pong 则断开。

## 断线重连

- 客户端指数退避重连：1s → 2s → 4s → … → 最大 60s
- 重连成功后重新 `register`
- 未 complete 的 task 由服务端 `idempotency_key` 去重后重投
- 客户端用 `task_id` 去重避免重复执行

## 与 A2A 的边界

- Channel 负责"IM 消息到哪个 Agent"（路由）
- A2A 负责"Agent 如何调用另一个 Agent"（语义）
- Agent Connector 只是传输层；消息内容可以是 A2A 格式
