# Changelog

All notable changes to the **Kingsoft AgentEngine SDK (ksadk)** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-01-22

### 🚀 新特性 (New Features)

- **架构升级 - AgentEngine Server 集成**:
    - 全面对接 AgentEngine Server，CLI 不再直接调用底层 Serverless API。
    - 新增 `AgentEngineClient` 统一 API 客户端 (`ksadk/api/client.py`)，提供标准化的 Agent/MCP 管理接口。
    - 架构解耦使得部署源替换更灵活，为未来多云支持打下基础。
- **本地状态管理 (`.agentengine.state`)**:
    - 新增部署状态持久化模块 (`ksadk/deployment/state.py`)。
    - 部署后自动记录 `agent_id`, `endpoint`, `api_key` 等信息到本地状态文件。
    - `invoke` 命令可自动读取状态，无需重复指定 Agent 名称或 Endpoint。
- **星流模型选择 (`agentengine model`)**:
    - 新增 `model` 命令，支持从 OpenAI 兼容 API 动态获取可用模型列表。
    - 交互式选择后自动更新 `.env` 中的 `MODEL_NAME` 配置。
- **Thinking 模型支持 (深度推理渲染)**:
    - Runner 层新增 `reasoning_content` / `thinking` 字段解析 (`patch_langchain.py`)。
    - 流式输出时支持模型思考过程的实时渲染（使用 Rich Panel 折叠展示）。
    - `agentengine run` 默认展示思考过程，增强调试体验。
- **CLI Invoke Markdown 实时渲染**:
    - `agentengine invoke` 交互模式集成 Rich Live + Markdown 渲染。
    - 流式输出时实现 5 FPS 限流刷新，大幅减少终端闪烁。
- **MCP Server 管理 (预览)**:
    - 新增 `agentengine mcp` 命令组 (`deploy`, `list`, `status`, `delete`)。
    - 新增 MCP 检测器 (`MCPDetector`) 和构建器 (`MCPBuilder`)，支持 FastMCP 项目自动检测与打包。
- **Memory SDK (预览)**:
    - 引入 `MemoryManager` SDK (`ksadk/memory/`)，支持可插拔的存储后端（InMemory, Redis）。
    - 为 Agent 后续提供标准化的长短期记忆管理能力做准备。
- **Web UI 品牌焕新**:
    - 界面 Branding 全面升级为 "Kingsoft Cloud Agent Engine"，提供更统一的控制台体验。
- **API 协议重构 ("Path-based Refactor")**:
    - 客户端与服务端通信协议全面升级。
    - 摒弃了旧的 Query Parameters (`?Action=...`) 模式，迁移至符合 RESTful 规范的路径模式 (如 `/agentengine/api/v1/CreateAgent`)。
    - 更新 Swagger 文档以匹配新架构。

### 🛠 改进 (Improvements)

- **全局配置管理 (`~/.agentengine/settings.json`)**:
    - 新增 `global_config` 模块，支持跨项目凭证复用。
    - `agentengine create` / `agentengine config` 支持全局配置的保存和读取。
- **环境变量统一**:
    - 规范化变量名: `MODEL_NAME` → `OPENAI_MODEL_NAME`, `OPENAI_API_BASE` → `OPENAI_BASE_URL`。
- **多租户一致性 (Account ID Unification)**:
    - 废弃 `user_id`，统一使用 `account_id` 作为系统内唯一的租户标识。
    - 实现了 `X-Ksyun-Account-Id` 在 CLI、Server 和 Serverless 后端之间的完整透传。
- **Serverless 部署增强**:
    - **KS3 智能区域配置**: 支持动态推导区域 Bucket 名称 (`agentengine-{account_id}-{region}`)。
    - **状态透视**: `agentengine status` 现可实时显示 Agent 的副本数 (`replicas`) 和就绪副本数 (`ready_replicas`)。
- **可观测性优化 (Langfuse)**:
    - 修复 Trace 重复上报问题。
    - Trace Name 直接显示具体的 **Agent Name**，统一 `LANGFUSE_BASE_URL` 配置。
    - 为 LangChain/LangGraph Runner 添加 `input.value` / `output.value` 属性，便于 Langfuse 可视化展示。
- **CLI 体验增强**:
    - 帮助文本使用彩色输出和更好的格式化。
    - 交互式运行体验优化 (questionary + rich 渲染)。
- **Python 版本支持**:
    - 新增 Python 3.13 / 3.14 支持。

### 🐛 修复 (Bug Fixes)

- **Windows 离线安装**: 将 `langchain`, `langgraph` 等核心库移至 `dependencies`，解决 `ModuleNotFoundError` 问题。
- **Windows BOM 兼容**: 全面使用 `utf-8-sig` 编码处理配置文件，确保 Windows 兼容性。
- **构建系统**:
    - 修复 Web UI 构建时因 Google Fonts 网络问题导致的构建失败（禁用字体内联）。
    - Makefile `build` 目标自动删除 `tar.gz` 和临时目录。
- **环境路由**: 修正了 Serverless Client 连接预生产/生产环境时的 Endpoint 路由逻辑。
- **依赖约束**: `fastapi` 版本上限 `<0.124.0` 以兼容 `google-adk`，`pydantic` 上限 `<3.0.0`。

### ⚠️ 破坏性变更 (Breaking Changes)

- **通信协议不兼容**: v0.2.0 版本的 CLI/SDK 必须配合 v0.2.0+ 的 AgentEngine Server 使用。
- **环境变量重命名**: `MODEL_NAME` → `OPENAI_MODEL_NAME`, `OPENAI_API_BASE` → `OPENAI_BASE_URL`。

---

## [0.1.0] - 2026-01-15

### 🎉 初始发布 (Initial Release)

- **AgentEngine CLI**:
    - 发布 `agentengine` 命令行工具。
    - 支持 `create`, `build`, `deploy`, `run`, `status`, `destroy`, `invoke`, `config`, `web`, `launch` 等全生命周期管理命令。
    - 支持 Local (Docker) 和 Cloud (Serverless) 双模态部署。
- **Agent 框架集成**:
    - 原生支持 **LangGraph**、**LangChain** 和 **Google ADK** 框架应用的开发与托管。
    - 提供统一的 `BaseRunner` / `UnifiedRunner` 运行时，封装 HTTP 服务与 SSE 事件流。
    - 自动检测项目类型 (`AgentDetector`)。
- **构建系统**:
    - 支持 `code` 模式 (ZIP 打包) 和 `container` 模式 (Docker 镜像) 双构建模式。
    - 自动依赖分析与打包。
- **Web UI 控制台**:
    - 内置 Angular 开发的本地可视化控制台。
    - 支持 Agent 聊天调试、Trace 查看及基本管理功能。
- **基础设施**:
    - 实现基于 KS3 的代码包上传与分发机制 (`KS3Uploader`)。
    - 集成 Langfuse 提供基础的可观测性支持 (`ksadk/tracing/`)。
    - 支持 OpenTelemetry 标准 tracing。
- **配置管理**:
    - 支持 `agentengine.yaml` / `ksadk.yaml` 项目配置文件。
    - 支持 `.env` 环境变量加载。
