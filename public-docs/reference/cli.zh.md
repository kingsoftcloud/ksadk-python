# 命令行参考

`agentengine` 是 KsADK 的公开命令行入口。

```bash
agentengine [OPTIONS] COMMAND [ARGS]...
```

全局参数：

| 参数 | 含义 |
| --- | --- |
| `--output pretty/json` | 在支持时选择人类可读或 JSON 输出 |
| `--no-color` | 关闭彩色终端输出 |
| `--dry-run` | 对支持的操作打印计划请求，而不实际执行 |
| `--version` | 显示包版本 |
| `-h, --help` | 显示帮助 |

## 核心命令

```bash
agentengine --help
agentengine init --help
agentengine run --help
agentengine web --help
```

## 命令分组

| 分组 | 公开角色 | 是否本地优先 |
| --- | --- | --- |
| `init` | 创建或包装项目 | 是 |
| `config` | 管理模型和项目设置 | 是 |
| `run` | 运行终端循环或本地 API server | 是 |
| `web` | 启动本地浏览器 UI | 是 |
| `a2a` | 在配置后暴露 A2A surface | 是 |
| `mcp` | 构建或管理 MCP 资源 | 取决于目标 |
| `build` | 准备部署制品 | 取决于目标 |
| `deploy` | 部署到配置的云端运行时 | 否 |
| `launch` | 一次性构建并部署 | 否 |
| `agent` | 管理 hosted Agent 资源 | 否 |
| `dashboard` | 打开 hosted dashboard 链接 | 否 |
| `files` | 管理 hosted 工作区文件 | 否 |
| `version` | 管理 hosted Agent 版本 | 否 |
| `hermes` | 管理 Hermes 资源 | 否 |
| `openclaw` | 管理 OpenClaw 资源 | 否 |

公开 quickstart 应聚焦本地优先命令。Hosted 命令文档必须说明它们的凭证和基础设施
前置条件。

## 本地开发

### `agentengine init`

创建新项目：

```bash
agentengine init my-agent -f langgraph
agentengine init my-agent -f adk
agentengine init my-agent --from-agent ./existing_agent.py
```

支持的 framework flag 包括 `adk`、`langchain`、`langgraph`、`deepagents`、
`openclaw` 和 `hermes`。公开示例应优先使用不依赖内部基础设施就能本地运行的框架。

导入已有代码后，先检查 `agentengine.yaml` 再运行。

### `agentengine config`

管理项目和模型设置：

```bash
agentengine config
agentengine config show
agentengine config set OPENAI_MODEL_NAME=my-model
agentengine config model
```

### `agentengine run`

运行 Agent 项目：

```bash
agentengine run .
agentengine run . -i
agentengine run . --port 8080
agentengine run . --model my-model
```

| 参数 | 含义 |
| --- | --- |
| `--port` | 本地 server 端口，默认 `8080` |
| `--interactive` / `-i` | 终端交互模式 |
| `--model` | 单次模型覆盖 |
| `--show-thinking` | provider 可用时展示 reasoning 输出 |
| `--no-stream` | 关闭流式渲染 |
| `--no-trace` | 关闭 tracing |

手动测试用交互模式，API 客户端用 server 模式：

```bash
agentengine run . -i
agentengine run . --port 8080
```

### `agentengine web`

启动本地调用和调试 UI：

```bash
agentengine web .
agentengine web . --port 7860
agentengine web . --model my-model
agentengine web . --no-open
```

## 协议和集成

### `agentengine a2a`

暴露 A2A 协议 surface 和 Agent Card metadata：

```bash
agentengine a2a card --help
agentengine a2a serve --help
```

### `agentengine mcp`

管理 MCP 相关资源和运行时流程：

```bash
agentengine mcp --help
```

## 构建与 Hosted 操作

这些命令属于 SDK surface，但可能需要金山云凭证或经过批准的 hosted 基础设施。

```bash
agentengine build --help
agentengine deploy --help
agentengine launch --help
agentengine agent --help
agentengine dashboard --help
agentengine files --help
agentengine hermes --help
agentengine openclaw --help
```

解释或审核部署形态命令时，在支持的地方使用 `--dry-run`。

## JSON 输出

部分命令支持结构化输出：

```bash
agentengine --output json agent status
agentengine --output json build --help
```

交互命令和浏览器 UI 命令在结构化输出可能误导时可以拒绝 JSON。

## 公开发布 gate

发布 CLI 文档前，应从 release candidate 重新生成 help 输出，并检查是否包含内部 URL、
凭证、私有 registry 名称、kubeconfig 路径和部署假设。

推荐检查：

```bash
agentengine --help
agentengine init --help
agentengine run --help
agentengine web --help
agentengine config --help
```
