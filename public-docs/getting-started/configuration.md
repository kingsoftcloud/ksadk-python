# 配置项

KsADK 从命令行参数、项目 YAML、项目 `.env` 和可选全局配置读取设置。越靠近
本次命令的值，通常优先级越高。

## 优先级

单次本地运行可以按这个顺序理解配置：

1. 命令行参数，例如 `--model` 和 `--port`。
2. 当前 shell 导出的环境变量。
3. 项目 `.env` 加载的值。
4. 项目 YAML，例如 `agentengine.yaml`。
5. 全局开发者默认值。

临时实验用命令行参数；应该随示例项目一起走的设置写进项目文件。

## 配置来源

| 来源 | 常见文件或命令 | 目的 |
| --- | --- | --- |
| CLI option | `agentengine run --model glm-5.1` | 单次覆盖 |
| 项目 YAML | `agentengine.yaml` | 框架、入口、region、打包提示 |
| 项目 env | `.env` | 本地模型凭证和 provider URL |
| 全局配置 | 由 `agentengine config --global` 类流程管理 | 跨项目开发默认值 |

## 模型设置

本地运行时示例使用 OpenAI 兼容配置：

```bash
OPENAI_API_KEY=sk-test
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL_NAME=my-model
```

文档和测试只能使用占位值。真实值只放在本地 `.env` 或 CI secrets。

| 变量 | 作用 |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI 兼容 provider 的 API Key |
| `OPENAI_BASE_URL` | provider base URL，通常以 `/v1` 结尾 |
| `OPENAI_MODEL_NAME` | 本地运行默认模型 |
| `MODEL_NAME` | 一些项目使用的兼容别名 |

当请求显式传入 `model` 时，支持的 Runner 可以把它作为单次请求的模型覆盖。

## 项目配置

项目级设置保存在 `agentengine.yaml`。兼容模式也会检测 `ksadk.yaml` 和
`ksadk.yml`。

```yaml
name: my-agent
framework: langgraph
entry_point: agent.py
agent_variable: root_agent
```

| 字段 | 含义 | 示例 |
| --- | --- | --- |
| `name` | 显示名称和默认运行时名称 | `my-agent` |
| `framework` | 框架适配器 | `adk`、`langchain`、`langgraph`、`deepagents` |
| `entry_point` | 本地运行时加载的 Python 文件 | `agent.py` |
| `agent_variable` | 导出的对象名 | `root_agent` |
| `region` | 部署形态命令使用的可选云 region | `cn-example-1` |

公开示例优先写显式项目 YAML。它比依赖自动检测更容易审核。

## 框架说明

| 框架 | 推荐公开配置 |
| --- | --- |
| ADK | 设置 `framework: adk`，`entry_point` 指向导出 ADK agent 的模块 |
| LangGraph | 设置 `framework: langgraph`，导出编译后的 graph 或配置的变量 |
| LangChain | 设置 `framework: langchain`，导出 chain/runnable 对象 |
| DeepAgents | 设置 `framework: deepagents`，避免 import 阶段启动服务 |

如果项目使用自定义 state 或 input 准备逻辑，把 hook 放在配置的入口模块里，Runner
才能稳定导入。

## 配置命令

交互式向导：

```bash
agentengine config
```

显示当前设置：

```bash
agentengine config show
```

非交互设置：

```bash
agentengine config set region=cn-beijing-6 OPENAI_MODEL_NAME=my-model
```

切换默认模型：

```bash
agentengine config model
```

## 本地覆盖

比较模型时使用 CLI option，而不是频繁编辑 `.env`：

```bash
agentengine run . --model another-model
agentengine web . --model another-model
```

## 运行时功能开关

有些能力是可选能力。公开示例应明确它们不是第一条 quickstart 的必需项：

| 变量 | 作用 |
| --- | --- |
| `KSADK_WORKSPACE_FILES_ENABLED` | 启用工作区文件路由 |
| `KSADK_WORKSPACE_MAX_UPLOAD_BYTES` | 设置工作区文件上传上限 |
| `KSADK_LTM_BACKEND` | 启用长期记忆后端集成 |
| `KSADK_LTM_INDEX` | 隔离长期记忆数据 |
| `KSADK_KB_DATASET_ID` | 启用知识库集成 |
| `KSADK_KB_TOP_K` | 设置知识检索数量 |
| `KSADK_BUILD_ENABLE_ATTACHMENT_OCR` | 在明确需要时把 OCR 相关依赖纳入构建 |

除非页面专门讲某个能力，否则第一条 quickstart 不应设置这些变量。

## Secrets 与文件

推荐本地结构：

```text
my-agent/
  agentengine.yaml
  agent.py
  requirements.txt
  .env              # 仅本地使用
  .gitignore
```

推荐 `.gitignore`：

```gitignore
.env
.agentengine/
dist/
build/
*.egg-info/
```

不要提交 `.pypirc`、API Key、cookies、kubeconfig、私有 registry 凭证、客户数据、
本地 session 数据库或上传文件。

## 公开文档规则

公开文档不能发布真实内部 endpoint、access key、cookies、kubeconfig 路径、
registry 名称、客户数据或私有支持 URL。

公开文档应当：

- 使用 `https://api.example.com/v1` 作为 provider URL 占位符。
- 使用 `sk-test` 或 `<YOUR_API_KEY>` 作为 token 占位符。
- 把云端设置标记为可选。
- 优先使用本地运行时示例，而不是 hosted 基础设施示例。
