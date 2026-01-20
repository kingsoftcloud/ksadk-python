# AgentEngine CLI (KsADK)

**[AgentEngine](https://www.ksyun.com/)** 是专为 **AI Agent 开发与部署** 设计的标准化工具链。它提供了一套统一的开发接口和命令行工具，支持多种主流 Agent 框架（**LangGraph**, **LangChain**, **Google ADK**），并能将 Agent 一键部署到金山云 Serverless 计算引擎。

> **v0.1.1 更新说明**: ksADK v0.1.1 架构全面升级，默认接入 **AgentEngine Server** (控制面)，由 Server 统一负责鉴权、路由分发及底层 Serverless 资源的调度。

## 🌟 核心特性

*   **多框架支持**: 原生支持 LangChain、LangGraph 和 Google ADK，自动识别并适配。
*   **统一管控**: 接入 AgentEngine Server，实现统一的 API Key 管理、限流保护及动态路由。
*   **Serverless 云原生**: 深度集成金山云 AgentEngine，支持秒级冷启动、自动扩缩容。
*   **双模式部署**: 支持 **Code 模式** (轻量级 zip 包) 和 **Container 模式** (自定义 Docker 镜像)。
*   **全链路可观测**: 内置 Langfuse 集成，一键开启 Trace、Metrics 和 Logs。

---

## 🏗️ 架构演进 (v0.1.0 vs v0.1.1)

### v0.1.0 (Legacy)
Client (CLI) 直接对接 Serverless 底层 API，架构简单但缺乏统一管控能力。
```
[CLI] --> [Serverless API] --> [Agent Instance]
```

### v0.1.1 (Current)
Client (CLI) 对接 **AgentEngine Server** (控制面)，实现了标准化的管理与接入。
```
[CLI] --> [AgentEngine Server] --> [Serverless API] --> [Agent Instance]
             (Auth/Route/Limit)
```

---

## 🚀 快速开始

### 1. 安装

```bash
# 安装核心 CLI
pip install agentengine

# 或者安装带特定框架依赖的版本
pip install "agentengine[langgraph]"
pip install "agentengine[adk]"
```

### 2. 初始化项目

创建一个基于 `LangGraph` 的 Agent 项目：

```bash
# 初始化项目 (支持 -f langgraph / langchain / adk)
agentengine init my_agent -f langgraph

cd my_agent
```

### 3. 配置环境变量

使用交互式命令配置 API Key 和云端凭证：

```bash
agentengine config
```

该命令会自动生成或更新 `.env` 和 `agentengine.yaml` 文件。

### 4. 本地运行与调试

**交互式调试模式** (在终端直接对话):

```bash
agentengine run -i .
```

**启动 API Server** (提供标准化 API):

```bash
agentengine web . --port 8080
```
> API 文档地址: `http://localhost:8080/docs`

### 5. 一键部署上云

将 Agent 部署到金山云 Serverless 环境 (自动完成构建、上传和部署):

```bash
# 需要先配置金山云凭证 (环境变量或参数)
export KSYUN_ACCESS_KEY=your-ak
export KSYUN_SECRET_KEY=your-sk
export KSYUN_ACCOUNT_ID=your-account-id

# 一键部署 (默认 Code 模式)
agentengine launch . --target serverless --region cn-beijing-6
```

---

## 📖 CLI 命令详解

### 1. 项目管理

#### `agentengine init`
初始化一个新的 Agent 项目模板。

```bash
agentengine init <project-name> --framework [langgraph|langchain|adk]
```
*   生成的项目包含推荐的目录结构和基础代码模板。
*   自动生成 `agentengine.yaml` 配置文件。

### 2. 开发调试

#### `agentengine config`
交互式配置向导，用于管理 `agentengine.yaml` 和 `.env`。
*   支持配置 Agent 名称、API Key、OSS/云厂商凭证等。
*   **幂等设计**: 可以重复运行，会自动读取现有配置作为默认值。

#### `agentengine run`
本地运行 Agent。支持自动检测框架类型并加载环境。
*   `-i, --interactive`: 进入终端交互模式。
*   `--no-trace`: 禁用 Langfuse 链路追踪。

#### `agentengine web`
启动生产级 API Server，提供标准化的 RESTful 接口：
*   `POST /run_sse`: 流式对话接口
*   `GET /health`: 健康检查
*   `GET /traces`: 查看 Trace 信息

### 3. 构建 (Build)

#### `agentengine build`
将 Agent 打包为可部署的制品。

**模式 1: Code (推荐)**
打包代码和依赖描述文件为 zip 包，上传至 KS3。Serverless 运行时会自动安装依赖。
```bash
agentengine build . --mode code --push
```

**模式 2: Container**
构建 Docker 镜像并推送至仓库。适用于有系统级依赖的复杂场景。
```bash
agentengine build . --mode container --tag my-agent:v1 --push
```

### 4. 部署 (Deploy)

#### `agentengine deploy`
将构建好的制品部署到目标环境。

```bash
# 部署到 Serverless (Code 模式)
agentengine deploy . --target serverless --ks3-path ks3:// bucket/path/code.zip

# 部署到 Serverless (Container 模式)
agentengine deploy . --target serverless --image my-registry/my-agent:v1 --artifact-type Container
```

参数说明：
*   `--target`: 部署目标，支持 `serverless` (默认), `k8s`, `docker` (本地测试)。
*   `--observability`: 是否开启可观测性 (默认开启)。
*   `--cpu / --memory`: 覆盖 CPU/内存配置 (如 `--cpu 2 --memory 4Gi`)。

#### `agentengine launch`
**构建 + 部署** 的组合命令，自动串联 `build --push` 和 `deploy` 流程。

### 5. 运维管理

#### `agentengine status`
查看 Agent 的运行状态、Endpoint 地址和副本数。
```bash
agentengine status --agent <agent-name> --watch
```

#### `agentengine invoke`
调用已部署的 Agent 进行测试。
```bash
agentengine invoke --agent <agent-name> --message "你好"
```

#### `agentengine destroy`
下线并删除 Agent 实例。
```bash
agentengine destroy --agent <agent-name>
```

---

## ⚙️ 配置文件 (agentengine.yaml)

在项目根目录下的 `agentengine.yaml` 用于定义 Agent 的元数据和部署配置。

```yaml
name: my_agent_demo
version: "1.0.0"

# 框架类型
framework: langgraph

# 入口定义
entry_point: my_agent/agent.py
agent_variable: root_agent  # 代码中 Agent 对象的变量名

# 资源配置
resources:
  cpu: "2"          # 2 vCPU
  memory: "4Gi"     # 4 GB 内存

# 扩缩容策略
scaling:
  min_replicas: 1   # 最小副本数 (0 为缩容到零)
  max_replicas: 10  # 最大副本数
  concurrency: 20   # 单实例最大并发请求数

# 部署配置
deploy:
  serverless:
    region: cn-beijing-6
```

## 🌐 环境变量

AgentEngine 会自动加载 `.env` 文件。常用变量如下：

| 变量名 | 说明 |
|--------|------|
| `OPENAI_API_KEY` | 模型服务 API Key |
| `OPENAI_API_BASE` | 模型服务 Base URL |
| `MODEL_NAME` | 使用的模型名称 |
| `LANGFUSE_PUBLIC_KEY` | Langfuse 公钥 (用于 Tracing) |
| `LANGFUSE_SECRET_KEY` | Langfuse 私钥 |
| `LANGFUSE_BASE_URL` | Langfuse 服务地址 |
| `KSYUN_ACCESS_KEY` | 金山云 AK (部署用) |
| `KSYUN_SECRET_KEY` | 金山云 SK (部署用) |
| `KSYUN_ACCOUNT_ID` | 金山云账号 ID (部署用) |
| `KSYUN_REGION` | 默认部署区域 (如 cn-beijing-6) |

---

## 🏗️ 架构说明 (KSADK Architecture)

AgentEngine CLI 通过适配器模式支持多框架：

1.  **Framework Detection**: 自动扫描项目代码，识别框架类型 (LangChain/LangGraph/ADK)。
2.  **Unified Runner**: 针对不同框架提供统一的运行包装器 (`UnifiedRunner`)，屏蔽底层差异。
3.  **Standardized API**: 所有 Agent 最终都暴露为兼容 OpenAI 格式的 REST API，便于客户端集成。
4.  **Auto Instrumentation**: 自动注入 OpenTelemetry/Langfuse 探针，无需修改业务代码即可实现可观测性。
