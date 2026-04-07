# ksadk (AgentEngine CLI)

`ksadk` 是金山云 Agent 开发与部署工具链，统一提供本地调试、构建部署、远端资源管理，以及平台级 KB/LTM、MCP、OpenClaw 接入能力。

当前版本：`0.4.0`

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

### 2. 一键部署

```bash
export KSYUN_ACCESS_KEY=your-ak
export KSYUN_SECRET_KEY=your-sk
export KSYUN_ACCOUNT_ID=your-account-id
export KSYUN_REGION=cn-beijing-6

agentengine launch . --target serverless
```

### 3. 打开云端 UI

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
- [Runner Approval 架构草案](./docs/Runner_Approval_Architecture.md)

## 说明

- `README` 现在只保留入口信息，不再承载完整命令说明。
- 完整 CLI 路径、环境变量、KB/LTM、MCP、OpenClaw、JSON 输出等说明统一收口到 [使用文档](./docs/ksadk_usage_guide.md)。
- 当前实现设计、核心子系统、关键调用链和与 `agentengine-server` 的边界统一收口到 [技术文档](./docs/ksadk_technical_design.md)。
