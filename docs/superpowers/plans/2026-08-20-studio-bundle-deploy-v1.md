# Studio 一键 Bundle 部署（cloud-deploy-v1）技术方案

> 状态：草案待评审。作者：Claude（Fable 5），2026-08-20。
> 前置：Phase 1 Agent Kernel 已完成（AgentInstance/bundle_digest/contract_digest/Interaction/v1 全部可用，预发 gate 27/27 绿）。
> 本方案复用 Phase 1 底盘，不新造身份/审计/合同体系。

## 1. 目标与非目标

**目标**：Studio 页面上把本地 agent（Code 模式：ADK/LangGraph/Codex 等）一键部署到云上，获得可对话的 hosted 入口。全流程有进度反馈、可回滚、可删除。

**非目标（明确不做）**
- 不做 Serverless 弹性伸缩（Phase 1 租约/fencing 已支持多副本，但 v1 固定 1 副本）
- 不做流量灰度/多环境（pre 单环境闭环，online 走既有 promotion 流程）
- 不改 wire protocol（AgentControlChannel/v1、Interaction/v1 冻结面不动）
- 不做 Codex workspace 跨 Pod 持久化（PVC/KS3FS 另立项，见 §9）

## 2. 现状断点（为什么现在点不了这个按钮）

| 环节 | 现状 |
|---|---|
| Studio UI | 无"部署到云"入口 |
| 代码打包 | `ksadk build` 已产出 zip（含 provenance），但无上传通道 |
| Server API | CreateAgent 只接受**自带镜像**（--image）；无 Bundle admission（上传→校验→build→实例化） |
| 镜像构建 | agentengine-images 的 build 全在本地 make，无服务端流水线 |
| 部署 | 协议链完整（这次预发已验证），但实例化靠手工 DB 行 + kubectl |

## 3. 架构：谁干什么

```
Studio(0.3.2)           agentengine-server              镜像构建器            Runtime Service/Operator
   │ ①上传 bundle.zip ──> ②Bundle admission            ③build job            ④AgentRuntime CR
   │    (progress SSE)     (校验/审计/落库)              (kaniko/自建)          (既有 agentKernel 投射)
   │                       ⑥GetDeployStatus ──────────── build 状态 <─────────── Pod ready
   └──⑤Studio 轮询/SSE <─── deployment 进度 <────────── digest-pinned image <── AGENT_KERNEL_* env
```

- **Server（控制面）**：唯一 admission 点。校验包 → 生成 bundle_digest（zip 全量 sha256）→ 排 build → 建 AgentInstance 行（复用 Phase 1 表，`bundle_digest` 字段已存在）→ 审计。
- **镜像构建器（新增组件，最小形态）**：K8s Job（kaniko 或 buildkit rootless），输入 = bundle.zip + 基础镜像模板，输出 = `hub.kce.ksyun.com/agentengine/tenant-bundles/{tenant}/{agent_id}:{bundle_digest[:12]}`。**为什么独立 Job 而不是 Server 内置 docker**：Server pod 无特权 docker socket；Job 天然并发/隔离/可重试。
- **Runtime Service**：复用既有 create_agent_runtime + Phase 1 的 agentKernel 投射（AGENT_KERNEL_STORE_DSN/JWKS/INSTANCE_ID env 注入已实现），仅新增"从 AgentInstance 行取参数"的数据源。
- **Studio**：部署向导 UI + 进度流 + 部署后的 hosted 会话入口（复用 0.3.2 的 InteractionTray/cursor 体系，零新协议）。

## 4. 数据模型（全部复用/微扩）

```sql
-- 新表：server 侧 bundle 存储与构建状态
CREATE TABLE agent_bundles (
  id VARCHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  agent_id VARCHAR(64) NOT NULL,
  bundle_digest VARCHAR(64) NOT NULL,        -- zip sha256，与 AgentInstance.bundle_digest 同源
  framework VARCHAR(32) NOT NULL,            -- adk|langgraph|codex|...
  entrypoint VARCHAR(256),                    -- agent.py/root_agent 等
  storage_uri VARCHAR(512) NOT NULL,          -- KS3 路径，zip 不进 PG
  size_bytes BIGINT NOT NULL,
  created_by VARCHAR(128) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (tenant_id, agent_id, bundle_digest)
);

CREATE TABLE agent_bundle_builds (
  id VARCHAR(64) PRIMARY KEY,
  bundle_id VARCHAR(64) REFERENCES agent_bundles(id),
  status VARCHAR(16) NOT NULL,               -- queued|building|pushed|failed|cancelled
  image_tag VARCHAR(512),
  image_digest VARCHAR(128),                  -- sha256:...，部署必 pin digest
  job_name VARCHAR(128),                      -- K8s Job 名，查日志用
  error TEXT,
  started_at/finished_at TIMESTAMPTZ,
  UNIQUE (bundle_id, image_digest)
);
```

AgentInstance 不改表：`bundle_digest` 已有列直接指向 agent_bundles.bundle_digest；`runtime_id` 指向 build 产出的镜像 digest。**协议合同零改动**。

## 5. API（Server 新增，全部走既有鉴权/审计/RBAC）

| Action | 说明 |
|---|---|
| `UploadAgentBundle` | POST multipart（zip ≤50MB 校验：必含 agent.py 或 ksadk.yaml、无绝对路径/软链穿越、非可编辑安装告警透传）。返回 bundle_id+digest。幂等：同 digest 秒回 |
| `CreateAgentDeployment` | 入参 bundle_id → 排 build Job + 建/更新 AgentInstance（desired_state=active）。返回 deployment_id |
| `GetAgentDeployment` | 状态聚合：bundle→build→instance→runtime ready（复用 Phase 1 readiness：AgentKernelReady + digest 三方一致）。返回阶段化 status 供 UI 进度条 |
| `CancelAgentDeployment` | cancelled（未 push 镜像前）/否则走删除流程 |
| `DeleteAgentDeployment` | 下线：instance desired_state=deleted → Operator 缩容 → 镜像 tag 留存（append-only 原则，只删运行面） |
| `ListAgentDeploymentEvents` | build/部署日志事件流（SSE，复用 SubscribeSessionEvents 的 cursor 语义但独立 topic） |

**admission 决策复用 Phase 1 的 `AgentControlAdmission` 模式**：租户绑定、配额（每租户 N 个活跃 deployment、bundle 存储 M GB）、审计行（成功/失败都落 `agent_control_audits` 同款脱敏结构）。

## 6. 镜像构建 Job 细节

- 基础镜像：复用 `agent-runtime-codex` / managed-runtime 系列模板，选型由 bundle 的 framework 决定（构建器只做"模板 + COPY bundle.zip + pip install"两步）
- 构建：K8s Job（namespace `agent-kernel-build`，ResourceQuota 限 2 并发/租户），kaniko cache 卷挂 KS3；超时 10min → failed
- 产出 tag：`tenant-bundles/{tenant}/{agent_id}:{digest12}`（**不可变 tag，永不重推**，同 digest 幂等跳过）
- 失败反馈：Job 日志按行进 build events 流（脱敏：不回显 env/secret）
- **安全**：bundle 内代码以非 root 运行、无 hostPath、network 默认仅 registry+PYPI 内网代理；`pip install` 依赖走内部 mirror；bundle 里 setup.py 的任意代码在 build 阶段执行是已知风险，v1 用"构建沙箱无凭据 + 产物扫描"缓解并在文档明示

## 7. Runtime 部署（全部复用）

- Runtime Service `create_agent_runtime` 增加 bundle 来源分支：image=build 产物 digest、AGENT_KERNEL_* env 从 AgentInstance/digests 组装（Operator 侧 SecretKeyRef 注入逻辑 Phase 1 已交付）
- lease TTL 45s / Inbox limit 100 / postgres driver：直接用 Phase 1 预发 values 已验证的组合
- Studio 会话入口：hosted-ui 0.3.2 已支持按 agent 路由，部署完成即可对话

## 8. Studio UI（web 0.3.3 范围）

1. 部署入口：workspace 侧栏"部署到云"向导（3 步：选入口文件 → 环境变量脱敏预览 → 确认）
2. 进度页：阶段化进度（上传→校验→构建→部署→就绪），失败展开 build 日志尾部
3. 部署管理：列表（版本/状态/digest/时间）、回滚（选历史 bundle_digest 重新 CreateDeployment）、删除
4. 就绪后 CTA："打开对话"（跳 hosted-ui 该 agent 的会话页）
- 环境变量处理：**上传时只传键名白名单**，值由用户在部署时单独录入 → Server 写 Secret（与 model_proxy 凭据同机制），bundle zip 内不含任何密钥

## 9. 已知风险与延后项

| 项 | 处理 |
|---|---|
| Codex workspace 跨 Pod 持久化 | 延后：v1 重启即新会话（Kernel durable Inbox 保证消息不丢）；PVC/KS3FS 单独立项 |
| build 阶段任意代码执行 | 沙箱无凭据 + 镜像产物扫描 + 文档明示；长期走无 Dockerfile 的 zip 直载运行时 |
| 大依赖 install 慢 | kaniko layer cache + 内网 mirror；>10min 失败给"预构建依赖镜像"提示 |
| glm-5.3 工具调用缺口 | 部署后 e2e 用 fake approval 验证链路，真实工具调用等 model proxy 修复 |
| 多副本/HPA | 延后（协议已支持，运营策略未定） |

## 10. 实施拆分与工作量

| 任务 | 仓 | 估时 |
|---|---|---|
| B1 Bundle 存储/admission API + 审计 | server | 3d |
| B2 构建 Job（kaniko 模板/状态回写/日志流） | server + 新 chart | 4d |
| B3 Runtime Service bundle 分支 + Operator digest 校验 | runtime-service/operator | 2d |
| B4 Studio 向导/进度/管理 UI | web | 4d |
| B5 预发端到端（真 langgraph agent 一键部署→对话→Pod kill 恢复→回滚→删除） | 跨仓 | 3d |
| 合计 | | **约 16 人日**（单人 3 周半；B1/B2 与 B4 可并行缩到 2 周） |

验收（B5 即 gate）：Studio 创建的 LangGraph agent 一键上云 → hosted 对话含 interaction 审批 → Pod kill 消息不丢 → 回滚到上一 digest → 删除无残留运行面；全程审计可追溯。

## 11. 依赖与顺序

B1 → B2 → B3 → B5；B4 只依赖 B1/B2 的 API 合同，可先行 mock。
前置条件：Phase 1 分支合并（agent_instance/admission/审计代码是本方案直接复用的地基）。
