# OpenClaw 网关服务技术文档（Dashboard + HTTP/WS 代理）

> 文档范围：`agentengine-server` 网关路由与短链接访问链路，聚焦 OpenClaw trusted-proxy 模式与能力层协同。  
> 读者：平台架构、后端、安全、解决方案与售前技术团队。

## 1. 技术目标

网关目标是同时满足三件事：

1. 浏览器直接访问 OpenClaw UI（体验友好）。
2. 身份可信透传（安全合规）。
3. 与技能能力层协同（业务可扩展）。

## 2. 设计原则

1. 访问入口统一：短链接 `/s/{link_id}`。
2. 会话统一：cookie session 承载 UI 上下文。
3. 身份统一：`trusted-proxy` 头由网关注入，前端不伪造。
4. 协议统一：HTTP 与 WebSocket 共享同一认证上下文。
5. 能力统一：网关路由与技能执行层协同，支撑安全/搜索/浏览器工具场景。

## 3. 核心组件

- `dashboard_access_link_service.py`  
  负责创建/解析短链接，并解析 OpenClaw trusted-proxy 用户头配置。
- `router_service.py`  
  负责短链接入口、HTTP 代理、WS 代理、session 校验、trusted-proxy 头注入。
- `bootstrap_actions.py`  
  负责下发客户端启动配置（默认镜像、升级提示、公告）。

## 4. 架构图（网关 + 能力层）

```mermaid
graph TD
    %% 样式定义
    classDef client fill:#e1f5fe,stroke:#03a9f4,stroke-width:2px,color:#01579b
    classDef gateway fill:#fff3e0,stroke:#ff9800,stroke-width:2px,color:#e65100
    classDef backend fill:#e8f5e9,stroke:#4caf50,stroke-width:2px,color:#1b5e20
    classDef runtime fill:#f3e5f5,stroke:#9c27b0,stroke-width:2px,color:#4a148c
    classDef skill fill:#fff8e1,stroke:#ffc107,stroke-width:2px,color:#f57f17
    
    %% 节点定义
    U["🌐 浏览器 (Browser)"]:::client
    G["🚪 网关路由 (Agent Router)"]:::gateway
    S["⚙️ 引擎服务 (AgentEngine)"]:::backend
    O["🧠 OpenClaw 运行时"]:::runtime
    SK["🧩 技能能力层"]:::skill
    
    SEC["🛡️ 安全 (ClawSec)"]:::skill
    REACH["🔍 搜索 (agent-reach)"]:::skill
    BROWSER["🌍 浏览器工具"]:::skill
    MEMORY["💾 记忆上下文"]:::skill
    SCHEDULER["⏱️ 定时任务"]:::skill

    %% 连线关系
    U -->|"1. 访问短链接 /s/[link_id]"| G
    G -->|"2. 解析链接映射"| S
    S -.->|"3. 返回 agent/鉴权信息"| G
    G -->|"4. 代理透传 (注入 trusted-proxy)"| O

    O -->|"触发与集成"| SK
    SK --> SEC
    SK --> REACH
    SK --> BROWSER
    SK --> MEMORY
    SK --> SCHEDULER

    O -.->|"5. UI/API/WS 数据响应"| G
    G -.->|"6. 页面呈现与交互"| U
```

## 5. 关键时序（短链接进入 UI 并触发能力层）

```mermaid
sequenceDiagram
    autonumber
    
    actor C as 💻 CLI 客户端
    actor B as 🌐 浏览器 (Browser)
    participant A as ⚙️ AgentEngine API
    participant R as 🚪 网关路由 (Router)
    participant O as 🧠 OpenClaw 运行时
    participant K as 🧩 技能能力层 (Skill Layer)

    note over C, A: 1. 短链接生成阶段
    C->>+A: CreateDashboardAccessLink<br/>(请求创建访问链接)
    A-->>-C: 返回短链接地址 (/s/[link_id])
    
    note over C, R: 2. 授权重定向阶段
    C->>B: 自动唤起浏览器打开链接
    B->>+R: 发起 GET 请求 (/s/[link_id])
    R->>+A: 解析短链接 ID (Resolve)
    A-->>-R: 返回 Agent 及鉴权元数据
    R-->>-B: 302 重定向并写入 Cookie 会话
    
    note over B, R: 3. 代理访问阶段
    B->>+R: 携带 Cookie 访问主页 (GET /)
    R->>+O: 发起代理请求 (Proxy)<br/>注入身份认证头部信息
    
    note over O, K: 4. 技能协同阶段
    O->>+K: 调用所需技能能力<br/>(安全/搜索/浏览器/记忆/定时)
    K-->>-O: 返回能力执行结果
    
    note over O, B: 5. 响应渲染阶段
    O-->>-R: 响应 HTML/JS 或建立 WS 连接
    R-->>-B: 最终呈现 UI 页面与交互数据
```

## 6. trusted-proxy 身份模型

### 6.1 注入头集合

当 `framework=openclaw` 时，网关注入：

- 自定义用户头（默认 `x-forwarded-user`）
- `x-forwarded-user`
- `x-authenticated-user`
- `x-authenticated-account-id`（存在 account_id 时）
- `x-ksc-account-id`（存在 account_id 时）

用户值优先取 `account_id`，缺失时回退 `anonymous`。

### 6.2 安全收益

- 前端仅持有短期 cookie，不持有后端长期身份凭证。
- OpenClaw 只信任代理链路身份头，减少前端伪造风险。
- 分享链接可过期/可撤销，便于审计与风控。

## 7. Session 与生命周期策略

- private 链接：短时有效，范围严格校验。
- share 链接：支持更长有效期，`expires=0` 可表示长期分享。
- 网关将 link 剩余 TTL 同步为 session TTL，避免会话提前失效。
- 预发环境默认不强制 secure cookie，线上按 https 决定 `Secure`。

## 8. HTTP / WS 代理细节

### 8.1 HTTP

- Agent 解析优先级：`X-Auth-Agent-Id` > `Authorization` 校验 > cookie session。
- 代理前过滤冲突头，再注入 trusted-proxy 头。
- SSE/stream 走流式转发，降低缓冲带来的延迟。

### 8.2 WebSocket

- 复用 cookie/authorization 的统一身份解析。
- 透传 origin/subprotocol，提升前端兼容性。
- trusted-proxy 头通过 `additional_headers`（或兼容 `extra_headers`）注入上游连接。

## 9. 客户端联动与动态配置

CLI 在 `openclaw deploy` 中调用 `GetClientBootstrapConfig`，可动态下发：

- `bootstrap.default_image` / `openclaw.default_image`
- `upgrade.latest_cli_version`
- `upgrade.min_cli_version`
- `notices`

收益：默认镜像、升级策略、运营公告可服务端热更新。

## 10. 可观测与告警建议

建议最少观测项：

1. short-link resolve 成功率与耗时。
2. cookie session 命中率与失效率。
3. HTTP/WS 错误码分布（400/401/404/410/502/1011）。
4. trusted-proxy 头注入命中率。
5. 技能调用成功率（ClawSec / agent-reach / 浏览器工具）。

## 11. 总结

这套网关方案将“访问体验、安全边界、能力扩展”三者统一在一条可运营链路：

- 入口安全：短链接。
- 会话稳定：cookie session。
- 身份可信：trusted-proxy。
- 能力可扩展：安全技能、搜索技能、浏览器工具协同。

对外可表述为：OpenClaw 不只是一个可访问 UI，而是可控可审计的企业级智能体能力底座。
