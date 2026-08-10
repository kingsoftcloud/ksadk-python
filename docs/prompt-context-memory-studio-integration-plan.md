# KsADK Prompt、Context 与 Memory Studio 产品化与全链路技术方案

> 适用分支：`feature-prompt-context-optimize`  
> 文档定位：指导 Prompt、Context、Memory 从 Runtime 能力走向 Studio 可配置、可调试、可评测和可部署  
> 当前状态：实施方案，不代表文中新增页面和 API 已经完成  
> 关联文档：[统一建设方案](./prompt-context-memory-unified-proposal.md)、[技术实现方案](./prompt-context-memory-implementation.md)、[评测方案](./prompt-context-memory-evaluation-plan.md)、[云端验证手册](./prompt-context-memory-cloud-validation.md)

## 1. 结论与建设重点

当前仓库已经包含完整的本地 AgentKit Studio 代码：

- Studio 后端：`ksadk/studio/`；
- Studio 前端源码：`ksadk/studio/web/src/`；
- Studio 构建后静态资源：`ksadk/studio/static/`；
- CLI 入口：`ksadk/cli/cmd_studio.py`；
- 统一运行时入口：`ksadk/runtime/`、`ksadk/conversations/`；
- Prompt、Context、Memory 新底座：`ksadk/prompts/`、`ksadk/context_engine/`、`ksadk/memory/`。

当前真正的问题不是“没有 Studio”，也不是“Runtime 后端完全没做”，而是下面三层尚未收口：

1. **Studio 配置层没有把新能力完整暴露给用户。** 用户可以编辑 system/task Prompt，但不能在 UI 中选择 Prompt ownership、Context policy、Memory policy，也看不到 CompiledPrompt 和 ContextPlan。
2. **Studio 调试层缺少解释能力。** 运行后只能看到回答、事件和基础 Trace，不能回答“这轮用了哪些 Prompt、Memory 为什么被召回、哪段 Context 被裁剪、估算是否精确”。
3. **本地 Studio、SDK Code 项目和云端 AgentVersion 的合同还没有完全统一。** 已发现标准 LangGraph 根目录的 `agentengine.yaml` 被 Studio Codex Manifest 读取路径误判；本地 SQLite Memory 也尚不能代表生产长期记忆。

因此，本期建议把建设目标定义为：

> 在不改变 RuntimeAdapter ownership 边界的前提下，让 Studio 成为 Prompt、Context、Memory 的配置、预览、调试、评测和发布入口；Runtime 继续负责执行，AgentEngine 控制面和 Memory Service 继续负责云端治理与生产存储。

首要交付不是一套新的 Agent Loop，而是以下四件事：

- 修复多 Runner 项目识别和导入；
- 将 Context/Memory 合同纳入 Agent Revision 和 Build；
- 在 Studio 中提供 Context Inspector；
- 建立本地调试与云端运行的一致性和差异说明。

---

## 2. 当前代码架构与完成度

### 2.1 Studio 当前链路

```text
agentengine studio <workspace>
        │
        ▼
ksadk/cli/cmd_studio.py
        │  创建本地 session/csrf，启动 FastAPI
        ▼
ksadk/studio/api.py
        │
        ├── Studio Web：ksadk/studio/static/
        ├── Agent CRUD：StudioService / AgentDraftRepository
        ├── Build：AgentCompiler / AgentBundleBuilder
        ├── Run：StudioRunService
        ├── Evaluation：EvaluationService
        └── Deployment：CloudGateway
                         │
                         ▼
                   RuntimeExecutor
                         │
              RuntimeAdapter Registry
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
       Codex            ADK          LangGraph
    native_owned   framework_assisted  assisted/hosted
```

### 2.2 已有能力

| 层级 | 已有能力 | 当前证据 |
|---|---|---|
| Studio 启动 | 本地 FastAPI、session/csrf、静态 UI | 本地启动和 bootstrap smoke 已通过 |
| 多 Runtime | RuntimeCatalog、RuntimeRegistry、每 Agent RuntimeRef | ADK/LangGraph 当前环境 Ready，相关测试通过 |
| Agent 定义 | AgentSpec、Instructions、Bindings、ContextSpec | `ksadk/studio/contracts.py` |
| Build | Revision 固化、ResolvedAgentSpec、Runtime Lock、不可变 Bundle | `AgentCompiler`、`AgentBundleBuilder` |
| Run | Build 经统一 RuntimeExecutor 执行并持久化 RuntimeEvent | `StudioRunService` |
| Prompt | system/task 已进入 Build；LangGraph 可投影为 ksadk hosted | `FrameworkRunSpecResolver` 与主链测试 |
| Context | ContextPlan、Planner、Assembler、Contributor、预算和降载 | 核心模块与集成测试通过 |
| Memory | Candidate、Policy、SQLite Provider、Recall/Flush | 本地及同一云部署版本已验证 |
| 云端 LangGraph | Prompt/Context/Memory hosted 主链 | `ksadk-pcm-canary` 真实预发已跑通 |

### 2.3 当前用户可见完成度

| 范围 | 估算完成度 | 说明 |
|---|---:|---|
| PCM 核心数据模型和 Runtime 主链 | 约 90% | Prompt/Context/Memory/Compaction/Working State 主链已接线并完成真实云端复测；真实 PTL 故障注入仍缺证据 |
| Studio 后端合同接入 | 约 85% | AgentSpec、导入、Preview、Evidence API 和单一 PreparedTurn 已完成；Operation/AgentVersion 控制面合同仍需收口 |
| Studio 前端产品体验 | 约 60% | 已有 PCM 配置、Prompt/Context 预览和 Context Inspector；Memory 治理、A/B 评测和发布体验仍需产品化 |
| 云端生产化 | 约 60% | 单副本 canary、Working State 与开关回退已通过；生产 Memory、多副本、滚动存储和 PTL 演练未完成 |

### 2.4 当前剩余的 Studio 缺口

ManifestResolver、ownership 配置入口、Prompt/Context Preview、Context Inspector、环境变量别名
以及原 Studio 回归均已在本期关闭。当前剩余项为：

1. rollout 仍需从环境变量兼容入口升级为 AgentVersion 级 `off/shadow/enabled` 发布策略；
2. Memory 页面目前偏调试，缺生产 Provider 的查询、删除、冲突和权限治理入口；
3. A/B 评测、版本对比、灰度和回退还未形成完整 Studio 产品流程；
4. Build/Run/Deploy 的 Operation、Idempotency-Key 和控制面状态合同尚未完全统一；
5. native Runtime 只能展示 projected/runtime-reported/opaque 证据，不能伪装成 exact；前端仍需
   更清晰地解释这一边界。

---

## 3. 产品目标与非目标

### 3.1 产品目标

用户在 Studio 中应能完成以下闭环：

1. 创建或导入 ADK、LangGraph、Codex Agent；
2. 明确看到当前 Runner 和 Context ownership；
3. 编辑稳定 Prompt，并查看编译后的来源、版本和覆盖关系；
4. 配置 Context 窗口、输出预留、Memory、Contributor 和 Compaction 策略；
5. 在不调用真实模型的情况下预览一次请求的 ContextPlan；
6. 本地运行后查看 planned/projected/actual 三种口径；
7. 查看 Memory 的召回、未采用、候选、写入和拒绝原因；
8. 运行固定 Case，对比旧路径和 V2 的效果、成本和稳定性；
9. 将同一个 Agent Revision/Build 部署到云端，明确哪些行为保持一致、哪些由云端 Provider 替换；
10. 能按 AgentVersion 独立灰度和回退，不依赖手工修改 Pod 环境变量。

### 3.2 非目标

本方案不在 `ksadk-python` 内建设：

- 完整 Agent Registry Server；
- 云端 SkillHub 管理面；
- 生产 Memory Service 的服务端实现；
- Serverless Pod/PVC 生命周期控制面；
- 对 Codex、Claude 等 native runtime 最终 Prompt 的强行接管；
- Skill 自进化和正式 Skill 自动发布。

这些能力分别属于 `agentengine-server`、Skill Service、Memory Service、Sandbox Service 或原生 Runtime。

---

## 4. 总体目标架构

```text
┌──────────────────────────────── Studio Web ────────────────────────────────┐
│ Agent 创建/导入 │ Prompt 编辑 │ Context Policy │ Memory Policy │ 评测/发布 │
│                                 │                                         │
│                       Context Inspector                                   │
│     Prompt Manifest │ Budget │ Selected/Dropped │ Memory │ Working State  │
└─────────────────────────────────┬──────────────────────────────────────────┘
                                  │ Studio API v1
                                  ▼
┌──────────────────────────── Studio Backend ────────────────────────────────┐
│ ManifestResolver → AgentDraft/Revision → Compiler → Immutable Build        │
│         │                 │                   │                            │
│         │                 ├─ ContextPolicy     ├─ CompiledPrompt manifest  │
│         │                 └─ MemoryPolicy      └─ Runtime/Provider lock     │
│         │                                                                    │
│ ContextPreviewService   RunService   EvaluationService   DeploymentGateway │
└─────────────────────────────────┬──────────────────────────────────────────┘
                                  │ canonical contract
                                  ▼
┌──────────────────────────── KsADK Runtime ─────────────────────────────────┐
│ PromptCompiler → Contributors → ContextPlanner → Assembler                │
│                         │                     │                             │
│              Session/WorkingState      RuntimeAdapter Projection           │
│                         │                     │                             │
│                         └──── MemoryCoordinator ────┐                       │
└─────────────────────────────────┬────────────────────┼───────────────────────┘
                                  │                    │
                ┌─────────────────┼─────────────┐      │
                ▼                 ▼             ▼      ▼
          Codex Native       ADK Assisted   LangGraph  Memory Provider
          honest observe     framework      hosted     local/http/sdk
                │                 │             │      │
                └─────────────────┴──────┬──────┴──────┘
                                        ▼
                           RuntimeEvent / Trace / Evaluation
                                        │
                                        ▼
                         AgentEngine 云端控制面与数据面
```

核心原则：

- Studio 负责**配置、解释、预览和编排**，不复制 Runtime 实现；
- Runtime 负责**真实执行和证据采集**；
- Build 固化**可迁移的语义合同**；
- Adapter 决定**如何投影给具体 Runner**；
- 云端替换 Provider，不改变 AgentVersion 的 Prompt/Context 语义；
- 对无法看到最终模型输入的 native runtime，只展示 projected/estimated，不伪装为 actual/exact。

---

## 5. Agent 与 Build 合同设计

### 5.1 AgentSpec 扩展

保留现有 `instructions` 和 `context`，逐步扩展为 AgentVersion 级声明。建议结构：

```yaml
spec:
  runtime:
    type: langgraph
    projectPath: runtimes/demo

  instructions:
    system: 你是企业部署助手。
    task: 所有部署变更先给 dry-run。

  context:
    ownership: auto                 # auto | ksadk | framework | native
    maxInputTokens: 65536
    reserveOutputTokens: 8192
    policyVersion: context-v2
    tokenizer: auto
    compaction:
      enabled: true
      softThresholdRatio: 0.50
      hardThresholdRatio: 0.85
      preserveWorkingState: true
    contributors:
      workspaceRules: true
      skillManifest: true
      memoryRecall: true

  memory:
    enabled: true
    providerRef: local-default       # Build 中只保存引用，不保存凭证
    recall:
      enabled: true
      maxTokens: 1600
      topK: 8
    write:
      mode: candidate                # off | explicit_only | candidate
      flushBeforeCompaction: true
    scopes:
      - tenant
      - workspace
      - agent
      - user

  rollout:
    contextEngine: shadow            # off | shadow | enabled
    memoryWrite: shadow              # off | shadow | enabled
```

### 5.2 ownership 不允许任意选择

Studio 应根据 Runtime capability 限制选项：

| Runtime | 默认值 | Studio 可选范围 | 说明 |
|---|---|---|---|
| KsADK Harness | `ksadk` | `ksadk` | 平台拥有 Agent Loop 和最终输入 |
| LangGraph | `framework` | `framework` / `ksadk` | 只有标准 hosted 投影链可选择 ksadk |
| ADK | `framework` | `framework`，后续开放 assisted | 防止重复注入 instruction/history |
| Codex | `native` | 只读 `native` | 使用 base_instructions 投影，不能声称拥有最终 Context |
| Claude Agent SDK | `native` | 只读 `native` | 同 Codex 原则 |

前端显示的不是简单开关，而是“平台接管级别”。当用户选择不受支持的组合时，后端必须返回 capability mismatch，不能静默降级。

### 5.3 Build 必须固化的内容

`resolved-agent-spec.json` 和 Runtime Lock 应新增或确认包含：

- Prompt source/version/hash；
- Prompt section manifest，不默认包含敏感正文；
- Context policy/version；
- Memory policy 和 Provider capability requirement；
- Runtime type/version/integration mode；
- Tokenizer 标识与精度预期；
- Contributor 清单、版本、信任等级和预算；
- rollout mode；
- capability hash；
- 不可变的 source/resolved/bundle digest。

Build 时执行 Conformance 校验：

- `ksadk` ownership 是否被 Adapter 支持；
- history/compaction/memory 是否发生双 ownership；
- Memory Provider 是否满足 scope、delete、version 等能力；
- Context 预算是否合法；
- required Prompt/安全规则是否可能被覆盖；
- local 与 cloud Provider 差异是否被显式记录。

---

## 6. Studio 后端建设方案

### 6.1 P0：统一 ManifestResolver，修复多 Runner 导入

新增统一入口，例如：

```text
ManifestResolver
├── detect_root_manifest()
├── detect_studio_workspace()
├── import_standard_project()
├── load_codex_manifest()
└── load_agent_drafts()
```

解析顺序建议：

1. 先读取 `framework`/`runtime.type` 的明确声明；
2. 明确为 Codex 时才进入 `CodexAgentManifest`；
3. 明确为 ADK/LangGraph 时走 `FrameworkDetector` 和标准 Code 项目导入；
4. `agents/*/agent.yaml` 继续作为 Studio 原生 workspace；
5. 无法判定时返回 `MANIFEST_KIND_AMBIGUOUS`，列出候选，不猜测为 Codex。

标准项目导入后建议生成 Studio sidecar，而不是改写用户根配置：

```text
.agentkit/imports/<agent-id>.json
```

sidecar 记录源路径、source digest、检测类型和导入时间；Build 时继续快照原始 Runtime 源码。

### 6.2 ContextPreviewService

新增只读预览服务，复用真实 PromptCompiler、Contributor、Planner，不复制算法。

建议接口：

```http
POST /api/v1/agents/{agentId}/prompt:compile
POST /api/v1/agents/{agentId}/context:preview
POST /api/v1/builds/{buildId}/context:preview
```

`prompt:compile` 返回：

- `promptVersion`、`contentHash`、`stablePrefixHash`；
- section 的 name/source/priority/tokenEstimate/status；
- `overriddenBy`、`droppedReason`；
- Runner projection 目标；
- 默认不返回敏感正文，local debug 可显式 `includeContent=true`。

`context:preview` 请求应允许传入模拟用户输入、Session、Tool Result 和附件 manifest；响应返回：

```json
{
  "accuracy": "estimated",
  "policyVersion": "context-v2",
  "budget": {
    "maxInputTokens": 65536,
    "reserveOutputTokens": 8192,
    "availableInputTokens": 57344
  },
  "items": [],
  "decisions": [],
  "totalsByKind": {},
  "warnings": [],
  "projection": {
    "runtimeType": "langgraph",
    "integrationMode": "ksadk_hosted"
  }
}
```

预览不得写 Session、Memory 或运行 Trace；需要 Contributor 外部调用时使用明确超时，并标注 `preview=true`。

### 6.3 Runtime Context Evidence API

Runtime 当前已产生 shadow/real ContextPlan 和 Usage，需要把证据投影给 Studio，而不是前端解析日志。

建议新增：

```http
GET /api/v1/runs/{runId}/context
GET /api/v1/runs/{runId}/prompt
GET /api/v1/runs/{runId}/working-state
GET /api/v1/runs/{runId}/memory-events
```

统一响应口径：

| 字段 | 含义 |
|---|---|
| `planned` | Planner 计划选择的内容 |
| `projected` | Adapter 实际投影给 Runner 的内容 |
| `actual` | Provider/Runner 明确回报的模型输入统计 |
| `accuracy` | `exact/reported/estimated/opaque` |
| `ownership` | prompt/history/memory/compaction 分别由谁负责 |
| `evidence` | usage、runtime event、provider response 等证据类型 |

若 Runtime 不可见最终上下文，`actual.items` 必须为空或明确 opaque，不能用 planned 内容冒充实际内容。

### 6.4 Memory Debug API

本地 Studio 只提供运行时调试接口，不承担生产 Memory 管理后台。

建议接口：

```http
POST /api/v1/memory:search
GET  /api/v1/sessions/{sessionId}/memory-candidates
POST /api/v1/memory-candidates/{candidateId}:approve
POST /api/v1/memory-candidates/{candidateId}:reject
DELETE /api/v1/memories/{memoryId}
```

限制：

- local Provider 可直接操作；
- cloud Provider 只通过正式 SDK/API 和身份访问；
- 所有查询强制 tenant/workspace/agent/user scope；
- 不允许前端传任意 scope 绕过当前 Principal；
- Secret/PII 检测结果可显示分类和拒绝原因，不回显已脱敏原文；
- 生产删除、审批和跨用户查询属于控制面权限，应跳转或委托云端 API。

### 6.5 Working State 与 Checkpoint API

Working State 必须成为结构化合同：

```json
{
  "goal": "完成预发部署验证",
  "phase": "rollback",
  "completed": ["basic", "same-session", "memory-recall"],
  "nextAction": "关闭 V2 后重复 Case A/B",
  "constraints": ["不得操作生产环境"],
  "pendingApprovals": [],
  "errorsAndCorrections": [],
  "updatedAt": "..."
}
```

Studio 后端负责从 Checkpoint/RuntimeEvent 读取和展示；更新必须经过 Session/Checkpoint 服务，不能让 UI 直接改模型摘要文本。

### 6.6 Operation 和错误合同修复

当前 Studio 测试暴露出 Build Operation 返回不一致。需要统一所有异步动作：

```json
{
  "id": "op_xxx",
  "kind": "BUILD",
  "status": "QUEUED",
  "resourceId": "agent-id"
}
```

Build、Run、Evaluation、Deployment、Rollback 都必须：

- 支持 Idempotency-Key；
- 返回同一 Operation schema；
- 可查询事件；
- 失败保留稳定 error code；
- 不把后台异常吞成 200 或空对象；
- UI 只依据 Operation terminal 状态判断成功。

---

## 7. Studio 前端建设方案

### 7.1 前端信息架构

建议在 Agent Detail 中增加四个一级区域：

```text
Agent Detail
├── Overview
├── Prompt & Policy
├── Context & Memory
├── Builds & Deployments
└── Runs & Evaluation
```

### 7.2 Agent 创建/编辑页

增加以下表单：

#### Runner 与 ownership

- Runtime 类型；
- 当前 integration mode；
- Context ownership，按 capability 限制；
- 对 native runtime 显示说明：“平台投影与观测，不接管最终上下文”。

#### Prompt

- System Prompt；
- Task Contract；
- Project Instructions 来源；
- Prompt version 和变更说明；
- “编译预览”按钮；
- section 排序、来源和覆盖警告。

首期不允许在 UI 任意调整 platform safety 顺序；平台安全段只读。

#### Context Policy

- 最大输入 token；
- 输出预留；
- soft/hard 阈值；
- Tokenizer；
- Contributor 开关；
- Tool Result 单项预算；
- rollout：off/shadow/enabled。

表单旁实时展示预算条：

```text
Prompt 8% | History 35% | Working State 5% | Memory 8% | Tool 24% | Reserve 20%
```

预算条是 preview，不应标注为模型实际消耗。

#### Memory Policy

- Recall 开关、topK、maxTokens；
- Write 模式；
- 压缩前 Flush；
- Provider 选择/绑定；
- scope 说明；
- local/cloud 差异提示。

### 7.3 Context Inspector

Run Detail 增加 Context Inspector：

```text
┌ Summary ─────────────────────────────────────────────┐
│ accuracy=reported  planned=12.2k actual=11.9k        │
│ prompt=1.8k history=5.4k memory=0.9k tool=3.8k       │
└──────────────────────────────────────────────────────┘

[Prompt] [Context Items] [Memory] [Working State] [Compaction] [Raw Evidence]
```

各页签内容：

- **Prompt**：section、来源、hash、版本、投影角色、覆盖关系；
- **Context Items**：selected/dropped、kind、priority、token、reason、trust；
- **Memory**：query、命中、采用/未采用、scope、候选写入结果；
- **Working State**：目标、阶段、下一步、关键约束；
- **Compaction**：触发阈值、压缩前后 token、保存/删除内容、PTL retry；
- **Raw Evidence**：脱敏 RuntimeEvent/Trace，不默认展示 Prompt 正文。

颜色口径必须固定：

- 绿色：实际使用或明确 reported；
- 蓝色：平台 planned/projected；
- 黄色：estimated；
- 灰色：opaque；
- 红色：mismatch、丢失 required、重复注入等异常。

### 7.4 Memory 调试页

首期只面向开发者：

- 按当前 Agent/User/Workspace 查看 Memory；
- 查看 Candidate 的提取来源和 Policy 决策；
- 手动批准显式记忆；
- 删除本地测试 Memory；
- 展示 supersede 链和版本；
- 一键新建 Session 验证跨 Session Recall。

云端 Memory 应显示 Provider 类型和控制面链接，不在本地 Studio 保存生产凭证。

### 7.5 A/B 评测页

同一 Build 支持生成两个运行配置：

- A：旧路径或 shadow；
- B：Context V2 enabled。

固定展示：

- Case 成功率；
- Prompt/Context/Memory 正确性；
- input/output/cache token；
- P50/P95 latency；
- PTL、retry、compaction 次数；
- Working State 关键字段保留率；
- Memory precision/recall 和错误召回；
- capability mismatch。

首期评测 Case 直接复用现有 `prompt-context-memory-evaluation-plan.md`，不在 UI 中发明另一套指标。

### 7.6 前端代码组织

建议在 `ksadk/studio/web/src/app/` 增加：

```text
context-policy.js
prompt-compiler-preview.js
context-inspector.js
memory-debug.js
working-state.js
pcm-evaluation.js
```

公共 API client 和状态模型集中管理，不把字段解析散落在页面事件中。完成源码修改后，通过现有前端构建流程同步到 `ksadk/studio/static/`，禁止只改构建产物。

---

## 8. Runtime 与后端底座仍需完成的工作

### 8.1 Working State 修复（本期已完成）

当前实现已经补齐 `constraints`、严格四字段验收、摘要后 schema 校验，以及关键字段缺失时与
压缩前 Working State 合并。2026-08-10 真实云端 Compaction 后，目标、关键约束、已完成项和
下一步全部恢复，首轮 Bad Case 已关闭。

后续生产增强只剩 Checkpoint 并发 version/stale guard、多副本恢复，以及更多 tool/approval
pending state 的云端压力测试，不再把基本 Working State 连续性列为未完成。

### 8.2 真实 PTL/emergency retry

- 构造可控的 Provider PTL fixture；
- 验证最多 retry 一次；
- 验证紧急降载不破坏 tool/approval 配对；
- RuntimeEvent 记录触发原因和裁剪决策；
- Studio Inspector 展示 PTL 恢复链。

### 8.3 生产 Memory Provider

当前 SQLite 适合本地和单副本功能验证，但云端滚动更新后记录已归零。生产需要：

- 接入 `LongTermMemoryService`/HTTP/SDK Provider；
- 使用可信 tenant/workspace/agent/user Principal；
- 支持幂等、版本冲突、TTL、删除、supersede、审计；
- 多副本一致性和故障降级；
- Provider capability negotiation；
- local SQLite 到云端 Provider 的迁移/导入工具。

### 8.4 Prompt 与 Context actual evidence

- LangGraph hosted：可记录 exact/projected；
- ADK assisted：尽量从框架 usage 和 state 取得 reported；
- Codex/Claude native：只记录平台 projection、thread/checkpoint reference 和 runtime reported usage；
- 禁止把估算 token 写成 actual；
- Prompt 正文默认不进入日志和基线。

### 8.5 回退合同

从环境变量回退升级为 AgentVersion rollout：

```text
contextEngine: off | shadow | enabled
memoryWrite: off | shadow | enabled
```

2026-08-10 已用环境开关完成一次真实预发回退与恢复：Agent ID/Endpoint 不变，基本调用和
同 Session 均成功。下一步仍需把该能力从环境变量升级为正式 AgentVersion rollout，并保证
回退不清空 Session/Memory、产生独立 Deployment 审计记录。

---

## 9. 本地 Studio 与云端控制面的职责边界

| 能力 | 本地 Studio / ksadk-python | agentengine-server / 平台服务 |
|---|---|---|
| Agent 草稿、Revision、Build | 本地实现 | 云端 Registry/Version 持久化 |
| Prompt 编译与 Context preview | 复用 SDK 核心实现 | 云端可复用相同 contract/service |
| RuntimeAdapter 执行 | SDK 负责 | Runtime 托管、扩缩容和生命周期 |
| 本地 Session/Trace | 文件/SQLite | 云端 Session/Trace 服务 |
| 本地 Memory 调试 | SQLite Provider | 生产 Memory Service |
| Provider 凭证 | env/ref，不写 Build | Secret/Identity/Policy 管理 |
| Deployment | 生成 payload、调用 Gateway | 创建 Runtime、Route、AgentVersion |
| 灰度/回退 | 提交策略和目标版本 | 状态机、审计、流量和回滚 |
| 多租户治理 | 只消费 Principal | 服务端强制隔离与权限 |

跨仓合同至少要明确：

- AgentVersion 中 ContextPolicy/MemoryPolicy 字段；
- DeploymentMode 与 Context ownership 两个独立字段；
- Memory Provider Ref 和运行时身份；
- RuntimeEvent/Trace 中 PCM evidence schema；
- Create/Update/Deploy/Rollback 的失败语义；
- 服务端不能支持某项能力时返回 capability mismatch，不静默忽略。

---

## 10. 分阶段建设路径

### 阶段 0：修复 Studio 基线（P0）

目标：Studio 对现有多 Runner 项目可用，测试基线可信。

- 修复 LangGraph/ADK 根 Manifest 被当作 Codex 解析；
- 统一 `OPENAI_BASE_URL` / `OPENAI_API_BASE`；
- 修复 3 个现有 Studio 测试失败；
- 增加标准 Code 项目导入 fixture；
- bootstrap 返回 PCM feature flags 和 runtime ownership capability；
- 保持运行行为不变。

验收：Studio 全量测试通过；现有 canary 可导入或被明确识别为标准项目；首页、Agent 列表、Build、Run smoke 通过。

### 阶段 1：配置合同与只读预览（P0/P1）

目标：用户能在 Studio 配置并理解策略，但暂不默认接管。

- 扩展 AgentSpec/ContextSpec/MemorySpec/RolloutSpec；
- ownership capability 校验；
- Prompt compile preview；
- Context preview；
- 前端配置页和预算条；
- Build 固化 policy/capability hash；
- 默认 rollout=`shadow`。

验收：同一 Revision 两次 Build hash 一致；preview 与 runtime shadow plan 在固定 fixture 上一致。

### 阶段 2：Run Context Inspector（P1）

目标：本地每次运行都能解释 Context。

- Runtime evidence API；
- Prompt/Context/Memory/Working State/Compaction 页签；
- planned/projected/actual 精度显示；
- 脱敏和权限；
- mismatch 告警。

验收：PCM-RUNNER、PCM-CTX、PCM-MEM 固定 Case 可在 UI 中定位原因。

### 阶段 3：Memory 与 Working State 产品化（P1/P2）

目标：跨 Session 能力可控、可验证。

- 修复 Working State Bad Case；
- Candidate 审批/拒绝；
- 本地 Memory 调试页；
- 生产 Provider Adapter；
- scope/Principal 验证；
- 跨滚动更新验证。

验收：关键约束保留率 100%；跨 Session 正确召回；滚动更新后 Memory 不丢失；跨用户无污染。

### 阶段 4：评测、云端灰度和回退（P2）

目标：从“能运行”进入“可发布”。

- Studio A/B 实验；
- AgentVersion rollout；
- 云端 Deployment contract；
- 逐 Runner 灰度；
- 回退演练；
- 清理旧路径。

验收：A/B 指标齐全；回退不改变 Agent ID/Session；生产 Provider、多副本、故障降级通过。

---

## 11. 建议 PR 拆分

| PR | 主题 | 是否改变运行行为 |
|---|---|---|
| PR-S0 | ManifestResolver、多 Runner 导入、env alias、Studio 失败测试修复 | 否 |
| PR-S1 | AgentSpec PCM 合同、ownership capability、Build lock | 否 |
| PR-S2 | Prompt/Context preview API | 否，只读 |
| PR-S3 | Studio PCM 配置前端与预算预览 | 否，默认 shadow |
| PR-S4 | Runtime Context Evidence API 与 Inspector | 否，观测为主 |
| PR-S5 | Working State 修复、PTL evidence | 是，可独立回退 |
| PR-S6 | Memory Debug、本地 Provider、生产 Provider Adapter | 是，默认 shadow/off |
| PR-S7 | Evaluation A/B、AgentVersion 灰度与云端部署合同 | 是，逐 Agent 灰度 |
| PR-S8 | 稳定后清理旧 Prompt/Context 分支 | 是，最后执行 |

不建议把 Studio 前端、所有 Runner 接管、Compaction 和生产 Memory Provider 放在同一个 PR 中。原因不是为了形式拆分，而是这些模块的失败面和回退方式不同：导入/预览可以零行为上线，Runtime 接管需要灰度，Memory 写入涉及数据副作用，云端合同还涉及跨仓协调。

---

## 12. 测试与验收方案

### 12.1 后端单测

- Manifest kind detection；
- AgentSpec migration；
- ownership capability matrix；
- preview 无副作用；
- Prompt section 覆盖和安全顺序；
- Context budget/group 原子性；
- Memory scope、PII、冲突和删除；
- Working State merge；
- Operation schema 和错误码。

### 12.2 Studio API 集成测试

至少覆盖：

1. 导入标准 LangGraph 项目；
2. 创建 Studio 原生 LangGraph Agent；
3. 编辑 Prompt/Context/Memory policy；
4. Build 固化 resolved spec；
5. Preview 与 Build 合同一致；
6. Run 产生 Context evidence；
7. Inspector API 不泄露 Secret；
8. rollout=off/shadow/enabled 行为；
9. Codex native 不被 hosted 接管；
10. Session/Memory 跨 Agent/User 隔离。

### 12.3 前端测试

- capability 驱动的表单禁用/提示；
- 预算校验；
- Operation 轮询与失败展示；
- Inspector 精度标签；
- plaintext/secret 默认隐藏；
- A/B 对比；
- Runner 切换时不静默保留不兼容配置。

### 12.4 本地浏览器 E2E

固定流程：

```text
启动 Studio
→ 导入 LangGraph canary
→ 配置 ksadk ownership + shadow
→ 编译预览
→ Build
→ 本地 Run
→ 打开 Context Inspector
→ 开启 V2
→ 重复 Run
→ A/B 对比
```

### 12.5 云端预发 E2E

复用现有 Case A-E，并增加：

- AgentVersion policy 与本地 Build digest 一致；
- Runtime capability 与 Studio 预览一致；
- 生产 Memory Provider 跨滚动更新；
- 多副本并发；
- PTL/emergency retry；
- Working State 关键字段；
- rollback；
- 控制面和数据面分别判定，不因 GetAgent 失败误报 Agent 宕机。

### 12.6 发布门槛

- required Prompt/安全规则丢失：0；
- tool/approval 孤儿事件：0；
- 跨 tenant/user Memory 污染：0；
- Working State 关键约束保留率：100%；
- actual 不可见时准确标记 opaque/estimated：100%；
- V2 成功率不低于旧路径；
- PTL 不高于旧路径；
- 回退演练通过；
- Studio 全量测试和浏览器 E2E 通过。

---

## 13. 兼容与迁移

### 13.1 旧 Agent

- 缺少 `context.ownership` 时按 Runtime 当前默认值解析；
- 缺少 `memory` 时视为关闭，不自动写长期记忆；
- 旧 `compaction.thresholdRatio` 映射到 hard threshold，soft 使用安全默认；
- Build 时写入 migration warning，但不原地改用户文件；
- 用户确认保存新 Revision 后才写新 schema。

### 13.2 环境变量

短期保留环境变量作为部署 override，但优先级明确为：

```text
平台强制安全策略
> AgentVersion policy
> Deployment override
> 兼容环境变量
> SDK 默认值
```

环境变量与 AgentVersion 冲突时必须记录来源和最终值。长期逐步移除依赖环境变量控制正式灰度。

### 13.3 Provider 迁移

- local SQLite 仅标记 `development`；
- 云部署如果仍使用 SQLite，Studio 必须显示醒目警告；
- 提供 export/import 的脱敏格式；
- 迁移期间双读不双写，或使用明确的迁移作业；
- 不在 Agent Build 中携带 Memory 数据。

---

## 14. 风险与应对

| 风险 | 表现 | 应对 |
|---|---|---|
| 双重 Prompt/History 注入 | 回答重复、token 异常 | ownership conformance + mismatch 熔断 |
| Studio preview 与真实运行漂移 | UI 看起来正确，Runner 实际不同 | preview 复用真实核心模块；展示 planned/projected/actual |
| native runtime 被过度接管 | Codex/Claude 行为破坏 | native ownership 只读；禁止 hosted assembler |
| Memory 污染 | 跨用户召回 | 服务端 Principal、scope 强校验、审计 |
| SQLite 被误当生产存储 | 滚动更新丢记忆 | 云端警告 + 生产 Provider 发布门槛 |
| Prompt 正文泄露 | Trace/截图包含敏感信息 | 默认 hash/manifest；正文仅本地显式调试 |
| 前后端 schema 漂移 | 表单保存后字段丢失 | OpenAPI/contract 生成类型 + schema fixture |
| 多 Runner 一次性接管 | 难定位回归 | 逐 Runner、逐 AgentVersion 灰度 |
| UI 只改静态产物 | 下次构建覆盖 | 只改 `web/src`，构建生成 `static` |

---

## 15. 本期建议范围

如果本期资源有限，建议把正式交付边界控制在：

### 必须完成

1. ManifestResolver 和 LangGraph/ADK 标准项目导入；
2. Studio 全量测试恢复；
3. Prompt ownership、Context rollout、Memory rollout 的 AgentSpec 合同；
4. Studio 配置页；
5. Prompt compile + Context preview；
6. Run Context Inspector 只读版；
7. Working State Bad Case 修复；
8. LangGraph 本地与云端回退演练。

### 可后续完成

1. Memory Candidate 人工审批；
2. 完整 Memory 管理 UI；
3. ADK 深度 assisted 接管；
4. Codex/Claude 更细粒度 native evidence；
5. Studio 内完整 A/B 实验编排；
6. 多 Provider Memory 迁移工具。

### 本期不做

1. Skill 自动进化；
2. 完整 Skill 评测；
3. 自建生产 Memory Server；
4. 强制统一所有 Runner 内部 Context；
5. 用 Studio 本地状态替代 AgentEngine 云端控制面。

---

## 16. 最终交付效果

完成本方案的首期范围后，用户在 Studio 中应获得如下体验：

1. 导入现有 LangGraph/ADK 项目，不再被误判为 Codex；
2. 创建 Agent 时能看到 Runner 对 Prompt/History/Memory/Compaction 的 ownership；
3. 编辑 Prompt 后能预览来源、版本、顺序和覆盖关系；
4. 调整 Context 预算时能立即看到预计占用和裁剪结果；
5. 运行一次后能区分平台计划、Runner 投影和实际回报；
6. 能看到 Memory 为什么被召回、为什么没有进入本轮、是否产生写入候选；
7. 长会话压缩后能检查当前目标、下一步和关键约束是否保留；
8. 本地 Build 部署到云端后，AgentVersion 和策略 hash 可对齐；
9. 新路径出现异常时能按 AgentVersion 回退，而不是临时进 Pod 改变量；
10. 产品、研发和测试可以使用同一套 Case 和指标讨论效果。

这才是 Prompt、Context、Memory 从“代码底座存在”升级为“用户能够配置、理解、验证和放心发布”的完成标准。
