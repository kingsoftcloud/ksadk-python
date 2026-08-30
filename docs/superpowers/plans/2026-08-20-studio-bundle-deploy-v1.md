# Studio 一键 Bundle 部署（cloud-deploy-v1）技术方案

> 状态：草案 v2（2026-08-20，基于 server 现有代码实况重写，推翻 v1 的"新增构建流水线"设计）。
> 前置：Phase 1 Agent Kernel 完成（AgentInstance/digest/Interaction/v1 可用，预发 gate 27/27 绿）。

## 1. 目标与非目标

**目标**：Studio 页面把本地 Code 模式 agent（ADK/LangGraph/Codex 等）一键部署到云，获得 hosted 可对话入口，含进度反馈/回滚/删除。

**非目标**：镜像构建流水线（kaniko）、Serverless 弹性、流量灰度、Codex workspace 跨 Pod 持久化、wire protocol 改动。

## 2. 关键事实：server 已支持 Code 部署（v1 方案误判，已纠正）

`CreateAgent(DeploymentType=Code, CodeConfig.Path="ks3://bucket/code.zip")` 全链已存在（`agent_service.py` code_backed 分支）：KS3 路径解析 → STS 临时凭证 → 内网 endpoint 强制 → `CodeConfiguration` → Serverless API 运行时下载执行。另有 ManagedRuntime 模式（RuntimeConfig.Name/Version/ManifestSha256）。

**因此不新增任何 Server 部署 API，不建镜像构建器。** 缺口只有两个：Studio 入口 UI + Kernel 装配投射。

## 3. 架构（全部复用）

```
Studio(0.3.3)                 agentengine-server              Serverless/Runtime Service/Operator
 ①选 zip ──上传 KS3──────────> ②CreateAgent(Code,CodeConfig)   ③Pod 起时下载 zip 运行
 ②'部署向导                    （admission/审计/STS 全既有）      ④agentKernel spec 投射 AGENT_KERNEL_* env ←缺口B
 ⑤进度/回滚/删除 <───────────── GetAgent 既有状态 + Phase1 readiness
```

### 缺口 A：Studio 部署入口（纯前端 + CLI 上传）

- Studio 向导 3 步：入口文件确认 → 环境变量脱敏预览 → 确认部署
- zip 上传复用 Studio 既有 KS3 通道（`ksadk build` 产物 + provenance）；CLI 侧 `agentengine deploy` 已有 KS3 上传逻辑可复用
- 上传后调既有 `CreateAgent(Code)`；部署后状态轮询 `GetAgent`（扩展返回 Phase 1 readiness 字段）

### 缺口 B：Code 模式 Pod 的 Kernel 装配（本方案唯一的真正开发量）

现状：Phase 1 的 AgentInstance/AGENT_KERNEL_* env 注入只在预发 canary（手工 deployment）验证过。需要打通：

1. **CreateAgent(Code) 时同步建 AgentInstance 行**（`agent_instances` 表已有）：instance_id 稳定、bundle_digest=zip sha256、contract_digest=d4a66a72…
2. **Runtime Service/Operator 投射**：Code 模式的 AgentRuntime CR 加 agentKernel spec（Task 10 已实现投射逻辑，补"从 AgentInstance 行取参数"的数据源分支）；Operator 注入 AGENT_KERNEL_STORE_DSN（SecretKeyRef）/JWKS/INSTANCE_ID/CONTRACT_DIGEST——这些 env 注入代码 Phase 1 已交付，只差接线
3. **zip 内 ksadk runtime 启动时读 env**：`bootstrap_agent_kernel_from_env` 已实现（AGENT_KERNEL_ENABLED=1 时自动装配 worker/lease/fenced events），需确保 zip 打包的 ksadk 版本 ≥ Phase 1 分支合并版本

## 4. 数据模型（零新表）

全部复用：`agents`（既有）+ `agent_instances`（Phase 1）+ `agent_control_audits`（Phase 1）。bundle_digest 即 zip sha256，由 CreateAgent 时计算写入 AgentInstance；不需要 v1 方案的 agent_bundles/agent_bundle_builds（那是镜像构建方案的表，zip 直载模式无构建态）。

## 5. API（零新增 Action）

| 需求 | 复用 |
|---|---|
| 创建部署 | `CreateAgent(DeploymentType=Code, CodeConfig)` 既有 |
| 进度/状态 | `GetAgent` + 扩展 Phase 1 readiness 投影（AgentKernelReady/lease/digest 三方一致） |
| 更新/回滚 | `UpdateAgent`（新 zip 上传后更新 CodeConfig.Path；回滚=改回旧 Path，旧 zip 留 KS3 append-only） |
| 删除 | `DeleteAgent` 既有 |
| 上传 zip | KS3 直传（Studio/CLI 既有通道），不经 server 中转 |

仅有的 additive 改动：CreateAgent 响应/GetAgent 增加 `deployment_phase` 与 kernel readiness 字段（optional 新字段，向后兼容）。

## 6. Studio UI（web 0.3.3）

1. workspace 侧栏"部署到云"向导（3 步，见缺口 A）
2. 部署管理页：版本（bundle_digest）/状态/时间列表；回滚按钮（切历史 Path）；删除
3. 就绪 CTA："打开对话"跳 hosted-ui 该 agent 会话页（0.3.2 交互体系直接可用）
4. 密钥隔离：zip 内无密钥；环境变量部署时经 Advanced.EnvironmentVariables 单独录入（既有机制），运行时凭据 STS（既有）

## 7. 已知风险与延后项

| 项 | 处理 |
|---|---|
| Code 模式 Pod 冷启动下载 zip 慢 | 观测；慢则后续加镜像缓存（届时才引入构建流水线，作为优化而非前置） |
| zip 内 pip 依赖在运行时装 | 依赖 zip 自带 venv/requirements 预装（ksadk build 已处理）；运行时无外网的场景文档明示 |
| Kernel env 是否会被 Serverless API 链路丢掉 | 缺口 B 第 2 步要实测验证（Runtime Service 透传字段清单核对），这是最大不确定点 |
| glm-5.3 工具调用缺口 | 部署后 e2e 用审批 fake 或 LangGraph 验证 |
| Codex workspace 持久化 | 延后（durable Inbox 保消息不丢） |
| 多副本 | 延后（协议支持） |

## 8. 实施拆分与工作量

| 任务 | 仓 | 估时 |
|---|---|---|
| C1 CreateAgent(Code) 建 AgentInstance + readiness 投影进 GetAgent | server | 2d |
| C2 agentKernel spec 数据源分支 + env 注入接线 + 实测投射 | runtime-service / operator | 3d |
| C3 zip 内 ksadk 版本门槛校验（<Phase1 版本给明确错误） | server + ksadk | 1d |
| C4 Studio 向导/管理页/回滚 UI | web | 4d |
| C5 预发端到端：真实 LangGraph agent Studio 一键部署→hosted 对话含审批→Pod kill 不丢→回滚→删除 | 跨仓 | 3d |
| 合计 | | **约 13 人日**（C1-C3 与 C4 并行，日历 ~2 周） |

验收（C5 即 gate）：上述端到端全绿 + 审计可追溯 + 旧路径（自带镜像/Ding 回调）回归零破坏。

## 9. 依赖与顺序

C1 → C2 → C3 → C5；C4 只依赖 CreateAgent/GetAgent 合同，可 mock 先行。
前置：Phase 1 六仓分支合并（AgentInstance/admission/env 注入代码是地基）。
