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
wss://{channel-host}/agentengine/connector/v1
Authorization: Bearer {jwt}
```

- 本地 Agent 主动建立出站 WSS 连接
- 生产环境 `token` 使用 HS256 compact JWT，含 `tenant_id`、`workspace_id`、`agent_id` 和未来时间的 `exp`
- 仅本地开发可使用 `tenant_id:agent_id:workspace_id[:user_id]` 三元组（服务端必须同时处于 `dev`/`test`/`studio` 且显式开启 `CONNECTOR_ALLOW_INSECURE_DEV_TOKENS=true`）；生产环境没有签名密钥时必须拒绝连接
- register 中的三元组必须与 token 身份一致；服务端返回有效身份时，客户端应校验 `tenant_id`、`workspace_id`、`agent_id`
- 连接后客户端必须在 10s 内发送 `register`

SDK 只允许远端 WSS；loopback WS 用于本机调试。SDK 通过 Authorization 头发送 token，不写入 URL、连接器状态或日志。服务端仍接受 query token；手工使用该方式时，服务入口和反向代理必须脱敏对应日志。`create_channel_connector(...)` 只创建客户端，宿主负责托管 `await connector.start()`，关闭时调用 `await connector.stop()`。

```python
import asyncio

from ksadk.connector import InvokeRequest, create_channel_connector


async def invoke(request: InvokeRequest) -> str:
    # 将完整的 session_id/channel/deadline 上下文交给本地 Agent runtime。
    return await agent_runtime.invoke(request.message, session_id=request.session_id)


async def main():
    connector = create_channel_connector(
        url="wss://channel.example.com/agentengine/connector/v1",
        token="<short-lived-jwt>",
        tenant_id="tenant-abc",
        workspace_id="ws-001",
        agent_id="agent-001",
        invoke=invoke,
    )
    try:
        await connector.start()
    finally:
        await connector.stop()


asyncio.run(main())
```

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
    "capabilities": ["invoke", "cancel"],
    "protocol_version": 1,
    "sdk": "ksadk/0.8.5"
  }
}
```

### register_ok（服务端 → 客户端）

```json
{ "type": "register_ok", "seq": 1, "payload": { "session_id": "conn-uuid", "heartbeat_sec": 30 } }
```

当前服务端在 `payload.identity` 返回最终采用的三元组；客户端发现任一字段与本地配置不一致时必须拒绝该连接。

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

### event（预留流式扩展；当前 SDK 不声明 stream 能力）

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

服务端等待 `heartbeat_sec` 后发送 ping，再给予配置的 pong 超时窗口（默认 30s）；客户端收到 ping 即回复 pong。首次 ping 之前的等待不计为 pong 超时。

## 断线重连

- 客户端指数退避重连：1s → 2s → 4s → … → 最大 60s
- 重连成功后重新 `register`
- 客户端用 `task_id` 去重；已发送 `complete`/`error` 的终态在有限缓存中保留，服务端重复投递时只重发终态，不重复执行 Agent
- 如果连接断开时一次派发的结果未知，SDK **不会自动重放 Agent 回调**，避免本地 Agent 产生重复副作用；当前服务端跨副本 relay 使用 Redis Pub/Sub，不是持久队列，也不保证 exactly-once；没有自动重投 Agent 请求。需要重试时，宿主必须按 `idempotency_key` 实现业务幂等
- 连接器重连期间仍在执行的回调会继续运行；完成结果会在新连接可用时尝试发送

`stop()` 会取消并在有界等待窗口内回收正在执行的回调。一个不响应取消的 Agent 不会无限期阻塞宿主关停，但其任务可能在本地后台结束；宿主应确保 Agent 的工具调用可取消。

## Studio 与高代码 Agent

Studio 的官方 Channel UI bundle 随 ksadk 制品分发。桌面启动后在后台写入默认 DSH Profile，首屏不等待 Core 或云端 Channel 服务；已禁用或卸载的插件不会在下次启动被自动恢复。插件是否默认装配可用 `KSADK_STUDIO_CHANNEL_DEFAULT=0` 控制。

渠道页配置服务 HTTPS 地址、账号和工作区，以及服务签发给指定本地 Agent 的 JWT。服务入口若需要额外认证，可以配置服务访问凭证。凭证写入工作区受限权限配置，不返回浏览器。插件默认出现并不等于所有 Agent 默认接收远端调用；只有明确连接的 Agent 才会建立出站 WSS。关闭渠道页面不会断开连接，退出 Studio 会断开。

渠道绑定显式保存目标：`ConnectorWorkspaceId` 非空表示本地工作区，离线时返回不可用；空值表示云端 Agent Runtime。同名本地/云端 Agent 可独立选择。Studio 将 IM 会话映射到隔离的稳定运行会话，并串行执行同一会话的消息。

基于 ksadk 的 ADK/LangGraph 高代码 Agent 可直接使用上面的 connector API，在 `invoke` 适配原有执行入口和会话存储，无需运行 Studio 或本地 Channel 服务。SDK 只依赖协议和轻量客户端，不安装 `agentengine-channel` Python 服务包。

当前 JWT 到期后需要更新凭证；自动签发和刷新属于部署方身份系统职责。不要将服务端签名密钥交给本地 Agent。

## 与 A2A 的边界

- Channel 负责"IM 消息到哪个 Agent"（路由）
- A2A 负责"Agent 如何调用另一个 Agent"（语义）
- Agent Connector 只是传输层；消息内容可以是 A2A 格式
