# 架构

KsADK 的公开架构可以理解为一层 Agent Runtime Platform：业务 Agent 仍由你选择的框架
编写，KsADK 负责统一运行、调试、协议、工具、沙箱、部署和观测。

![KsADK Agent Runtime Platform 架构](../assets/ksadk-runtime-architecture.png)

## 总览

```text
ADK / LangGraph / LangChain / DeepAgents
                    │
                    ▼
                  KsADK
                    │
    ┌───────────────┼────────────────┐
    ▼               ▼                ▼
 Skill Runtime   Workspace        Sandbox
    │               │                │
    └───────────────┼────────────────┘
                    ▼
              AgentEngine
                    │
                    ▼
          Hermes / OpenClaw Runtime
```

## 核心边界

| 层 | 责任 |
| --- | --- |
| Agent 框架 | 编排业务逻辑、状态、工具调用和模型交互 |
| KsADK CLI | 创建项目、加载配置、本地运行、Web UI 启动和打包 |
| Runner | 把 ADK、LangGraph、LangChain、DeepAgents 适配到统一调用接口 |
| 本地 Server | 暴露 `/v1/responses`、`/v1/chat/completions` 和本地 Web UI API |
| Toolsets | 提供 Skill、Workspace、Platform、Sandbox 等工具入口 |
| AgentEngine / Hermes / OpenClaw | 承接远端运行、部署和更完整的 runtime backend |
| OpenTelemetry | 输出标准 tracing，接入外部观测系统 |

## 本地运行路径

当你执行 `agentengine run` 或 `agentengine web`：

1. CLI 解析项目目录、`.env` 和 `agentengine.yaml`。
2. 框架检测器识别 ADK、LangGraph、LangChain 或 DeepAgents。
3. Runner Factory 创建对应 Runner。
4. Runner 加载用户 Agent。
5. 本地终端、Web UI 或 OpenAI-Compatible API 调用 Runner。
6. 会话、附件、workspace 文件、工具调用和 tracing 由 KsADK 统一处理。

## 为什么这个架构重要

这层边界让团队可以保留各自熟悉的 Agent 框架，同时共享：

- 同一套本地命令。
- 同一套浏览器调试体验。
- 同一套 OpenAI-Compatible API。
- 同一套 Skill / Workspace / Sandbox 工具模型。
- 同一套部署和观测入口。

更详细的内部本地运行时实现见 [运行时架构](../guides/runtime-architecture.md)。
