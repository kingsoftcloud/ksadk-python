# OpenClaw 客户端环境部署与 API 接入指南

> 主入口已迁移：OpenClaw 的标准 CLI 路径与统一入口说明请先看 [ksadk_usage_guide.md](./ksadk_usage_guide.md)。
>
> 本文保留为 OpenClaw 专项部署与 SDK 接入参考，重点放在一键部署、渠道接入和自动化接入样例。

> 目标：通过 AgentEngine CLI，快速完成 OpenClaw 云端部署与安全访问开通。  
> 核心卖点：**开箱即用、极速启动、安全免配置、内置技能能力栈**。  
> 行业场景口径（养虾）：**快速养虾，想养几只就养几只**（按需部署、按需扩缩）。

## 1. 方案价值

- **开箱即用**：默认预构建 OpenClaw 镜像，免本地构建。
- **安全免配置**：默认 `trusted-proxy` 身份模式，浏览器无需携带后端长期凭证。
- **极速上线**：`agentengine openclaw deploy` 一条命令直连控制面。
- **能力预集成**：模型映射、Dashboard 短链接、状态文件自动持久化、渠道插件/默认技能全部内建。
- **弹性养殖**：按需扩缩实例与并发，业务高峰“多养几只”，平峰“少养几只”。

## 2. 养虾场景表达

- 把一个 OpenClaw 实例理解成一只“数字虾苗”：部署后即可开工。
- 新业务上来时，直接扩容“多放几只虾苗”；淡季时再缩容回收成本。
- 安全能力和搜索能力通过默认 bundled skills / 严格模式附加技能接入，部署后可按场景启用能力链路。

## 3. 内置能力亮点

### 3.1 默认技能组合

- 默认 bundled skills 为：`clawhub-store`、`agent-browser-clawdbot`、`kdocs`。
- 当 `OPENCLAW_EXEC_STRICT_MODE=true` 时，会额外补充 `self-improving-agent` 与 `tuanziguardianclaw`。
- 对外可表达为：默认镜像即具备技能发现、浏览器自动化和文档协同能力；严格模式下再附加更强的安全/自优化技能。

### 3.2 技能商店与搜索能力

- 镜像侧不再内置 `skillhub`；默认改为使用 `clawhub-store` 技能，并将 `CLAWHUB_SITE` / `CLAWHUB_REGISTRY` 预设到 `https://cn.clawhub-mirror.com`。
- 默认搜索主路径为 OpenClaw 原生内建 `browser`；`agent-browser` 作为增强/备选能力保留，适合需要更强 CLI 可控性、会话隔离或可重放步骤的场景。如需轻量文本多搜索源策略，可按需显式启用可选的 `multi-search-engine`，例如把 `OPENCLAW_PRESET_SKILLS_ALLOWLIST` 设为 `clawhub-store,agent-browser-clawdbot,kdocs,multi-search-engine`。
- 如需手动安装技能，可直接使用 `clawhub install <slug> --registry=https://cn.clawhub-mirror.com`；若运行环境里没有全局 CLI，也可用 `npx clawhub@latest install <slug> --registry=https://cn.clawhub-mirror.com`。
- 对外可表达为：平台在部署即具备“技能商店 + 浏览器搜索/检索”基础能力，不需要再单独准备初始化脚本。

### 3.3 浏览器工具内置

- 运行时已包含浏览器工具相关配置（headless/no-sandbox/executable path 等），并默认启用原生 browser 能力，便于开箱使用网页自动化与 JS 渲染能力。
- 对外可表达为：客户无需额外采购或安装浏览器自动化运行环境。

### 3.4 默认插件与技能预置

- 镜像默认预置 `openclaw-weixin`、`openclaw-lark`、`agent-browser` 与 `clawhub` CLI。
- 同时预装 `curl`、`jq`、`python3` 等常用基础工具，保证默认搜索、JSON 解析与技能初始化链路可直接运行。
- 镜像默认让 `openclaw-weixin` 与 `openclaw-lark` 直接跟随 npm 官方 `latest` 稳定版；其中微信远端扫码链路额外保留一个极小 shim，对官方稳定版 `2.1.7+` 自动补齐 `web.login.start/web.login.wait` 的 gateway methods 暴露，避免高速迭代期因显式锁版本导致默认镜像快速过时。
- 启动时会通过 `sync_default_extensions()` 将默认插件同步到用户挂载的 `~/.openclaw`，缺失时自动补齐，用户手动升级后的插件版本不会被强制回滚。
- 默认 bundled skills 包括：`clawhub-store`、`agent-browser-clawdbot`、`kdocs`。
- 当前自定义镜像构建默认固定基础镜像为 `alpine/openclaw:2026.4.5`。
- fresh deploy 下默认启用 OpenClaw 内建 `browser`，并额外修复 `127.0.0.1` loopback Gateway 调用误入 pairing 的兼容问题。
- 从旧默认集合迁移时，`find-skills` 这类已下线的预置技能只会在“之前由镜像同步且用户未改动”的情况下被自动清理；用户自管目录会被保留。
- 对外可表达为：默认镜像即具备“渠道接入 + 浏览器自动化 + 技能商店”基础能力，不需要再单独准备初始化脚本。

### 3.5 可选上下文/记忆插件位

- 运行时保留 OpenClaw 原生插件扩展位，可按需启用上下文/记忆类插件，而不是默认绑定第三方实现。
- 对外可表达为：上下文增强能力支持按场景选装，默认镜像更轻、更稳、供应链面更小。

### 3.6 默认支持定时任务

- 运行时默认支持定时任务能力（内置调度守护进程启动链路），无需客户再手动搭建额外调度组件。
- 对外可表达为：可将例行巡检、日报汇总、定时抓取等任务纳入标准化自动执行流程。

## 4. 先决条件

1. Python 3.10+ （推荐 3.12 版本）
2. 金山云账号与 AK/SK
3. OpenAI 兼容模型服务 API Key

安装 CLI：

```bash
pip install -U ksadk
agentengine --version
```

## 5. 标准部署流程

```bash
# 1) 创建 OpenClaw 项目模板
agentengine init my-openclaw -f openclaw
cd my-openclaw

# 2) 写入最小必需环境变量
cat > .env <<'ENV'
KSYUN_ACCESS_KEY=你的AK
KSYUN_SECRET_KEY=你的SK
KSYUN_REGION=cn-beijing-6
KSYUN_ACCOUNT_ID=你的账号ID

OPENAI_API_KEY=你的模型APIKey
# OPENAI_BASE_URL=https://你的openai兼容网关/v1
# OPENAI_MODEL_NAME=glm-5.1
# CLAWHUB_SITE=https://cn.clawhub-mirror.com
# CLAWHUB_REGISTRY=https://cn.clawhub-mirror.com
ENV

# 3) 一键部署 OpenClaw
agentengine openclaw deploy

# 4) 查询状态
agentengine openclaw status

# 5) 打开云端 UI（短链接）
agentengine dashboard open --share

# 6) 连接默认渠道（本地终端打印二维码）
agentengine openclaw channel connect --channel weixin
agentengine openclaw channel connect --channel feishu
```

## 6. 关键行为说明

### 6.1 `openclaw deploy`

- 自动读取 `.env` 并补齐运行所需变量。
- 未显式指定镜像时，优先使用服务端下发默认镜像。
- 部署成功后写入 `.agentengine.state`，后续命令可直接复用目标实例。

### 6.2 `openclaw status`

- 建议确认 `RUNNING` 后再打开 Dashboard。
- 若非 `RUNNING`，等待后重试即可。

### 6.3 `dashboard open`

- 默认创建短链接并打开浏览器。
- 在 OpenClaw 工作目录内可直接运行 `agentengine dashboard open`，会自动读取当前目录 `.agentengine.state` 的 OpenClaw 实例。
- 适用于企业内部演示、测试与受控分享访问。

### 6.4 `openclaw channel connect`

- `agentengine openclaw channel connect --channel weixin` 会在本地终端打印 ASCII 二维码，并在扫码成功后把账号配置写回远端实例。
- `agentengine openclaw channel connect --channel feishu` 会复用官方 onboarding 流程，在本地终端完成飞书扫码与 Bot 配置，然后通过 gateway `config.apply` 写回远端。
- `agentengine openclaw channel status --probe` 可用于检查账号是否已真正落到远端实例中。

### 6.5 默认执行审批策略

- 默认采用宽松执行模式：`exec.host=gateway` + `exec.security=full` + `exec.ask=off`。目标是优先贴近原生 OpenClaw 体验，不在默认冷启动时强行切到白名单执行。
- 默认 `askFallback=full`：若后续显式开启审批模式而审批 UI 不可达，则按 `full` 语义继续处理，避免渠道/自动化流程被无意阻断。
- 默认 `autoAllowSkills=false`：不自动放行 Skill CLI，避免第三方或自定义 Skill 借助宿主机执行路径扩大敏感面。
- 若启用严格模式或显式开启默认白名单，镜像会通过 `/opt/openclaw/safe-bin` 安全包装器为 `main` 智能体预置一组常用只读/开发命令白名单（如 `pwd`、`ls`、`whoami`、`id`、`uname`、`date`、`ps`、`df`、`du`、`stat`、`find`、`cat`、`head`、`tail`、`wc`、`git`），并对工作区边界与状态目录访问做额外检查。
- 严格模式下还会默认放行少量直接二进制，如 `curl`、`jq`、`openclaw`、`agent-browser`、`clawhub`，避免常见检索、JSON 解析与渠道运维动作被基础白名单拦截。
- 默认 `tools.fs.workspaceOnly=false`：文件工具不再被强制锁死在工作区，便于读取技能目录、项目外挂资料和常见挂载路径；敏感目录访问仍应结合 `tools.exec` 白名单与提示词安全边界一起约束。
- 如需切到更保守的执行策略，可在部署时使用 `--exec-profile strict`，或通过环境变量显式设置 `OPENCLAW_EXEC_SECURITY=allowlist` / `OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED=true`。
- 模型 API Key 默认不再以运行时环境变量方式提供给 Gateway 进程，而是在启动阶段转存到 `${OPENCLAW_STATE_DIR}/secrets.json` 并通过 OpenClaw `file` SecretRef 读取，以降低 `printenv` / 环境转储类泄露风险。
- 默认工作区会内置一版安全导向的 `SOUL.md` 和 `AGENTS.md`，为 prompt injection、防泄密、外发审批、Skill 安装审批等场景提供软约束。

## 7. 企业常用扩展命令

```bash
# 列出实例
agentengine openclaw list

# 指定实例查询
agentengine openclaw status ar-xxxx

# 指定镜像
agentengine openclaw deploy --image hub.kce.ksyun.com/myns/openclaw:v2

# 显式模型网关
agentengine openclaw deploy \
  --model-base-url https://api.example.com/v1 \
  --model-api-key sk-xxx \
  --default-model glm-5.1

# 删除实例
agentengine openclaw delete ar-xxxx -y

# 高峰时段：多养几只（扩容）
agentengine openclaw deploy --name openclaw-gateway-peak
```

## 8. 常见问题

### Q1: 部署后模型调用失败

检查模型环境变量：

```bash
cat .env | rg 'OPENAI_API_KEY|OPENAI_BASE_URL|OPENAI_MODEL_NAME|OPENCLAW_'
```

### Q2: Dashboard 打开异常（401/403）

优先使用统一入口：

```bash
agentengine dashboard open --share
```

### Q3: 私有镜像拉取失败

补齐镜像仓库凭证：

```bash
export KCR_PASSWORD=你的KCR临时密码
export KSYUN_ACCOUNT_ID=你的账号ID
agentengine openclaw deploy --image hub.kce.ksyun.com/你的命名空间/openclaw:你的tag
```

## 9. 对外一句话版本

OpenClaw 基于 AgentEngine 提供“**一键部署 + 安全免配置 + 内置安全/搜索/浏览器能力栈 + 按需弹性伸缩**”的企业级智能体上线方案。

## 10. SDK 自动化接入 (CRUD 与面板管理 API)

除了使用 `agentengine` CLI 外，您的业务系统代码还能通过 `ksadk` 的 Python Client 轻松实现 OpenClaw 实例的全生命周期管理（创建、更新、查询、删除）及前端免密 UI 面板（Dashboard）链接的颁发。

### 10.1 核心全生命周期时序

以下是典型的业务研发端，通过 SDK 孵化、使用并最终销毁一只“龙虾（OpenClaw 实例）”的交互流：

```mermaid
sequenceDiagram
    autonumber
    
    actor Dev as 👨‍💻 开发者 / 业务系统
    participant SDK as 📦 ksadk Client
    participant API as ⚙️ AgentEngine API

    note over Dev, API: 1. 孵化与配置生命周期 (Create & Update)
    Dev->>+SDK: create_agent<br/>(底层调用 CreateAgentProduct 创单接口)
    SDK->>+API: 发起后端商品创建 (CreateAgentProduct)
    API-->>-SDK: 返回 agent_id
    SDK-->>-Dev: 实例创建成功得到 "龙虾 ID"

    Dev->>+SDK: update_agent<br/>(动态热更新, 比如调整 scaling)
    SDK->>+API: 发起热更新请求
    API-->>-SDK: 更新立即生效
    SDK-->>-Dev: 弹性扩缩容及配置下发完成

    note over Dev, API: 2. UI 面板授权 (Get Link)
    Dev->>+SDK: create_dashboard_access_link<br/>(请求分享免密链接)
    SDK->>+API: 颁发短期 Token 链接
    API-->>-SDK: 返回包含 link_id 的网关 URL
    SDK-->>-Dev: 后端将此 URL 分发给前端供最终用户展示

    note over Dev, API: 3. 销毁回收 (Delete)
    Dev->>+SDK: delete_agent<br/>(确认生命周期结束)
    SDK->>+API: 执行彻底清理
    API-->>-SDK: 云端底层算力及状态回收
    SDK-->>-Dev: "龙虾" 被彻底物理销毁
```

### 10.2 客户端初始化

```python
import asyncio
from ksadk.api.client import AgentEngineClient

async def init_client():
    # 自动从环境加载 KSYUN_ACCESS_KEY / KSYUN_SECRET_KEY / KSYUN_REGION
    async with AgentEngineClient() as client:
        pass # 调用 CRUD 方法
```

### 10.3 OpenClaw 实例生命周期管理 (CRUD)

> **核心提示**：无论 CLI 还是 API，创建 OpenClaw 的本质是调用 Agent 协议层，并将参数 `framework` 设为 `openclaw`。

```python
async def manage_openclaw(client: AgentEngineClient):
    # 1. 创建 OpenClaw 实例 (本质调用 CreateAgentProduct 创单接口)
    # framework 必填 openclaw，artifact_type 选填 Container
    create_req = {
        "name": "my-openclaw-api-agent",
        "description": "通过 SDK API 创建的 OpenClaw 实例",
        "framework": "openclaw", 
        "artifact_type": "Container", 
        "artifact_path": "hub.kce.ksyun.com/ksyun-ksadk/openclaw:latest",
        "resources": {"cpu": 2, "memory": "4Gi"},
        "env_vars": {
            "OPENAI_API_KEY": "sk-xxx",
            "OPENAI_BASE_URL": "https://api.openai.com/v1"
        }
    }
    create_res = await client.create_agent(create_req)
    agent_id = create_res["agent_id"]
    print(f"Created Agent ID: {agent_id}")

    # 2. 查询实例状态 (Read)
    status_res = await client.get_agent(agent_id=agent_id)
    print(f"Agent Status: {status_res['status']}")

    # 3. 更新实例配置 (Update) -> 调整并发与 CPU 等
    update_req = {
        "resources": {"cpu": 4, "memory": "8Gi"},
        "scaling": {"min_replicas": 1, "max_replicas": 5}
    }
    await client.update_agent(agent_id, update_req)

    # 4. 删除实例 (Delete)
    # await client.delete_agent(agent_id)
```

### 10.4 获取与管理 Dashboard 免密授权链接

基于 `trusted-proxy` 机制，业务系统可为当前前端终端用户实时生成专属授权链接，实现 OpenClaw 控制台页面的鉴权安全嵌入。

```python
async def manage_dashboard_links(client: AgentEngineClient, agent_id: str):
    # 1. 创建带有授权凭证的短期分享链接
    link_res = await client.create_dashboard_access_link(
        agent_id=agent_id,
        link_type="share",      # 支持 "private" / "share"
        expires_seconds=3600    # 链接有效时长 (秒)
    )
    link_id = link_res["link_id"]
    
    # 最终分发给前端用户的 URL，包含 link_id
    # 该地址进入网关后会完成 302 Cookie 种植与 x-forwarded-user 信任头注入
    ui_url = f"https://console.agentengine.ksyun.com/s/{link_id}"
    print(f"Secure UI Access Link: {ui_url}")

    # 2. 查询当前颁发的可用链接列表
    links = await client.list_dashboard_access_links(agent_id=agent_id)
    
    # 3. 后端主动撤销某特定人员的访问短链接 (安全阻断)
    await client.delete_dashboard_access_link(link_id=link_id)
```

## 11. 结语

一只龙虾看起来需要成长的空间很大，可是他已经出生了。  
给他目标，帮他拉弓，让他帮我们飞向靶心。
