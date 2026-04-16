# ksadk (AgentEngine CLI)

`ksadk` 是金山云 Agent 开发与部署工具链，统一提供本地调试、构建部署、远端资源管理，以及平台级 KB/LTM、MCP、OpenClaw、Hermes 接入能力。

当前版本：`0.5.0`

## 0.5.0 亮点

- `Hermes` 正式进入 ksadk 主线，新增独立资源组、共享 runtime 镜像和原生远程 TUI。
- OpenClaw 新增用户自定义镜像模板，适合直接打包给业务或客户做二次定制。
- `agentengine openclaw deploy` 新增 `--env KEY=VALUE` 透传，可把业务自定义环境变量直接注入容器。
- 新增 `agentengine openclaw repair` 与 `agentengine openclaw gateway doctor --fix`，补齐控制面修复入口。

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
- [Hermes Agent 本地安装、云端部署与远程 TUI 参考](./docs/hermes-agent-v2026.4.13_本地安装配置与ksadk接入流程.md)
- [Hermes Runtime 共享镜像与运行时约定](./deploy/hermes/README.md)
- [Runner Approval 架构草案](./docs/Runner_Approval_Architecture.md)

## 说明

- `README` 现在只保留入口信息，不再承载完整命令说明。
- 完整 CLI 路径、环境变量、KB/LTM、MCP、OpenClaw、Hermes、JSON 输出等说明统一收口到 [使用文档](./docs/ksadk_usage_guide.md)。
- 当前实现设计、核心子系统、关键调用链和与 `agentengine-server` 的边界统一收口到 [技术文档](./docs/ksadk_technical_design.md)。
