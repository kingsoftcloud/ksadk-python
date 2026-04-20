# ksadk (AgentEngine CLI)

`ksadk` 是金山云 Agent 开发与部署工具链，统一提供本地调试、构建部署、远端资源管理，以及平台级 KB/LTM、MCP、OpenClaw、Hermes 接入能力。

当前版本：`0.5.1`

## 0.5.1 亮点

- code mode 构建新增 Linux Runtime 兼容性 / ABI 校验，关键原生扩展不兼容时会在构建阶段提前终止，而不是把问题拖到远端运行时。
- Hermes 默认共享 runtime 镜像更新为 `hub.kce.ksyun.com/agentengine-public/hermes-agent:2026.4.16`；对 `glm-5.1`，runtime 在未显式配置时会补齐 `context_length=200000`，并把 fallback model 默认收敛到 `kimi-k2.5`。
- OpenClaw 默认基线切到官方 `ghcr.io/openclaw/openclaw:2026.4.15@sha256:0e6bebecf4623216420851f5edd133a748335f45c3508b635f7c5c4bfbc6da7d`，方便和最新 upstream 行为保持一致。
- OpenClaw heartbeat 默认收口为隔离轻上下文：`every=30m`、`target=none`、`isolatedSession=true`，避免心跳占用当前聊天窗口。
- OpenClaw 默认模型目录和自动补齐的 primary model 项不再把 `maxTokens` 写死为 `8192`，当前基线提升到 `20000`。

## 安装

```bash
pip install -U ksadk
```

可选依赖：

```bash
pip install "ksadk[langgraph]"
pip install "ksadk[langchain]"
pip install "ksadk[deepagents]"
pip install "ksadk[adk]"
pip install "ksadk[kb]"
```

命令入口等价：

```bash
agentengine --help
ksadk --help
```

## 最快路径

### 1. 本地运行

```bash
agentengine init my_agent -f langgraph
cd my_agent
agentengine config
agentengine run -i
# 或
agentengine web --port 8080
```

### 2. 一键部署代码框架

```bash
export KSYUN_ACCESS_KEY=your-ak
export KSYUN_SECRET_KEY=your-sk
export KSYUN_ACCOUNT_ID=your-account-id
export KSYUN_REGION=cn-beijing-6

agentengine launch . --target serverless
```

### 3. Hermes 云端托管

```bash
export KSYUN_ACCESS_KEY=your-ak
export KSYUN_SECRET_KEY=your-sk
export KSYUN_ACCOUNT_ID=your-account-id
export KSYUN_REGION=pre-online
export OPENAI_API_KEY=your-model-key
export OPENAI_BASE_URL=http://kspmas.ksyun.com/v1
export OPENAI_MODEL_NAME=glm-5.1

agentengine init demo-hermes -f hermes
cd demo-hermes
agentengine hermes deploy --name demo-hermes
agentengine invoke demo-hermes
```

说明：

- `agentengine hermes deploy` 默认使用共享 Hermes runtime 镜像，不走本地 build/push。
- 如果注入的是 `kspmas.ksyun.com` 公网模型地址，CLI 会在云端 runtime 配置里自动改写为 `kspmas-internal.sdns.ksyun.com`，避免预发 / 线上 Pod 访问公网网关超时。
- `agentengine invoke <hermes-agent>` 默认进入 Hermes 原生远程 TUI；浏览器聊天页请用 `agentengine hermes open --chat`。
- `agentengine hermes open <hermes-agent> --manage` 或 `agentengine dashboard open --path / --share` 打开的 Hermes 管理 UI，会保留 Hermes 自身 `/api/*` session bearer token，不再把它误当作平台 API Key。
- Hermes 管理 UI 首次打开默认写入中文 locale；如需覆盖，可显式设置 `HERMES_UI_LOCALE=en`。
- 对 `glm-5.1`，Hermes runtime 在未显式配置时会补齐 `context_length=200000`，并把 fallback model 默认设为 `kimi-k2.5`；如需覆盖可显式设置 `HERMES_CONTEXT_LENGTH` / `HERMES_FALLBACK_MODEL`。

### 4. 打开云端 UI

```bash
agentengine dashboard open
# 或显式指定 Agent
agentengine dashboard open --agent ar-xxxx
```

## 主文档

- [使用文档](./docs/ksadk_usage_guide.md)
- [技术文档](./docs/ksadk_technical_design.md)

## 专题参考

- [DeepAgents 框架参考](./docs/deepagents.md)
- [KB / LTM 示例参考](./docs/knowledge_base_and_memory_examples.md)
- [ADK 记忆能力专项参考](./docs/memory_usage_guide.md)
- [OpenClaw 一键部署与接入参考](./docs/openclaw_client_one_click_deploy.md)
- [OpenClaw 用户自定义镜像模板参考](./deploy/openclaw-user-template/README.md)
- [Hermes AgentEngine 实战手册（从初始化到 IM 连接）](./docs/hermes-agentengine-guide.md)
- [Hermes Agent 本地安装、云端部署与远程 TUI 参考](./docs/hermes-agent-v2026.4.16_本地安装配置与ksadk接入流程.md)
- [Hermes Runtime 共享镜像与运行时约定](./deploy/hermes/README.md)
- [Runner Approval 架构草案](./docs/Runner_Approval_Architecture.md)

## 说明

- `README` 现在只保留入口信息，不再承载完整命令说明。
- 完整 CLI 路径、环境变量、KB/LTM、MCP、OpenClaw、Hermes、JSON 输出等说明统一收口到 [使用文档](./docs/ksadk_usage_guide.md)。
- 当前实现设计、核心子系统、关键调用链和与 `agentengine-server` 的边界统一收口到 [技术文档](./docs/ksadk_technical_design.md)。
