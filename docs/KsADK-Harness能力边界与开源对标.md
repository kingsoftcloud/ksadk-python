# KsADK Harness 能力边界与开源对标

> 文档状态：实测盘点（2026-09-01）
> 适用仓库：`ksadk-python`
> 基线分支：`feat-ksadk-harness`
> 测试基线：638 passed / 6 skipped / 644 测试用例

---

## 1. 定位

KsADK Harness 是企业 Agent 的统一运行时——把 LLM 编译为可审计、可构建、可部署、可治理的 Agent Revision，运行在 KsADK Managed Runtime 中。

**不是个人编码工具**，不是单租户 CLI Agent，不是图编排可视化平台。它是面向企业 Agent 平台（财务、客服、内部工具）的数据面运行时，强调受控、合规、可观测、可替换引擎。

**默认执行链路**：

```
Studio 创建 → AgentDraft → AgentRevision → HarnessCompiler → HarnessSpec
  → KsADK Harness → ManagedLangGraphEngine → Model / MCP / Skill / Memory / Sandbox
```

普通用户不选 Runner、不选引擎、不接触 LangGraph 类型。

---

## 2. 能力清单

### 2.1 执行引擎

| 能力 | 现状 | 说明 |
|---|---|---|
| 默认引擎 | ✅ ManagedLangGraphEngine | 状态图 + Checkpoint + Interrupt/Resume，LangGraph 类型不暴露到平台协议 |
| 兼容引擎 | ✅ Codex / ADK / 外部 LangGraph | 各自声明 Capability Matrix，不伪造一致性 |
| 引擎无关 Loop | ✅ loop/ 纯逻辑 | reason/tool 逻辑不依赖 LangGraph，可在无引擎环境单测 |
| 流式 Token | ✅ TEXT_DELTA 逐 chunk | reasoner.stream_complete + loop 层发 TEXT_DELTA，首 token 延迟降到首 chunk |
| 多 Agent 策略 | ✅ single/plan-execute/plan-execute-review | ExecutionStrategyRegistry 按 spec 编译图拓扑，策略后置不固化进模板 |
| 子 Agent | ✅ 独立 Run/Checkpoint/预算/取消域 | 并行子 Agent、依赖图拓扑、fail_fast/partial 模式、父取消传播 |
| 跨进程恢复 | ✅ SQLite/Postgres Checkpointer | 审批中断/进程重启后从 Checkpoint 恢复，Tool Receipt 防重复副作用 |

### 2.2 上下文与记忆

| 能力 | 现状 | 说明 |
|---|---|---|
| Stable/Dynamic 分层 | ✅ | Stable Prompt（身份/角色/策略）与 Dynamic Context（输入/Working Context/Memory/历史）分开构建、分别算 Hash |
| 动态 Token 预算 | ✅ | 窗口来自 Model Profile → Provider 目录 → 静态元数据 → 安全默认值链，无固定常数 |
| 主动/紧急压缩 | ✅ | 主动阈值前压缩；紧急压缩 overflow 后更强压缩仅重试一次 |
| 压缩后校验 | ✅ | 正则抽取关键 ID/金额/日期/版本/审批号，比对压缩前后存在性 |
| 压缩后重注入 | ✅ | 丢失的关键事实作为"必须保留"重新注入 |
| Working Context | ✅ | 当前目标/已确认约束/未解决问题/计划/已验证事实/最近工具失败 |
| Core Memory Block | ✅ | 常驻块数量与总预算有限，超预算进按需检索 |
| 受控写入 | ✅ | 禁止整段聊天写入/无来源写入 org/敏感标签未脱敏写入 |
| Memory 审计 | ✅ | 每次写入留来源/决策/冲突/操作记录 |
| Memory 后端 | ✅ SQLite + Postgres | SQLite 默认本地，Postgres 多副本共享；http/sdk/longterm-service 钩子已有 |

### 2.3 能力运行时（Tool/MCP/Skill）

| 能力 | 现状 | 说明 |
|---|---|---|
| MCP 真实调用 | ✅ | stdio/http/sse 三种 transport，真实 tools/list + tool 调用 |
| MCP 降级治理 | ✅ | health 缓存/熔断/required vs optional 分类/Schema drift 检测 |
| Tool Policy | ✅ | 风险分级 → allow/deny/require_approval，按 Revision 级策略投影 |
| Tool Receipt | ✅ | 幂等键防重复副作用，审批通过后仅执行一次 |
| 审批 Interrupt/Resume | ✅ | approval interrupt 挂起 → 审批决定 → resume 重放 Receipt |
| 并行 Tool | ✅ | 无审批/无副作用的批次并行执行，有审批的串行 |
| Skill 四级渐进披露 | ✅ | L0 名称→L1 Manifest→L2 完整正文→L3 引用资源，越级拒绝 |
| Skill 按需加载 | ✅ | 正文和资源仅执行时加载，不常驻 Prompt |
| Sandbox Backend 协议 | ✅ | 不绑定 E2B 对象，统一 SandboxBackend Protocol |
| Sandbox 后端 | ✅ 本地只读 + E2B 适配 | 本地只读白名单最低安全模式；E2B 远程后端适配 |

### 2.4 生命周期

| 能力 | 现状 | 说明 |
|---|---|---|
| Revision → Build → Deploy → Activate → Invoke | ✅ | 每步对应真实产物，非只改数据库状态 |
| Build Manifest | ✅ | 记录 revisionRef/harnessVersion/engine/modelProfileRef/mcpRefs/skillRefs/contentHash |
| Rollback | ✅ | 回滚后 Route 指向历史版本，继续 Invoke 命中旧版本 |
| Draft vs 正式等价 | ✅ | Draft 和正式编译使用同一 HarnessCompiler，Draft 不创建 Build/Route |

### 2.5 可观测性

| 能力 | 现状 | 说明 |
|---|---|---|
| RuntimeEvent 事件体系 | ✅ v1 冻结 | Run/Turn/Model/Tool/Context/Memory/Approval/Usage/Artifact/Checkpoint 事件族，additive only |
| 事件成对 | ✅ | Tool Call begin/end、Model started/completed 成对，Conformance 校验 |
| OTel/OTLP 导出 | ✅ | OTelSpanAdapter 映射 RuntimeEvent→OTel Span，按 KSADK_OTEL_ENDPOINT 开关 |
| Context 检查 | ✅ | context_inspection 单页可视化投影（预算/压缩/缓存/Memory） |
| Capability Health | ✅ | 从事件流投影 MCP/Skill/Sandbox 健康状态 |

### 2.6 Conformance

| 能力 | 现状 | 说明 |
|---|---|---|
| 事件契约校验 | ✅ 14 个 verify 函数 | start/terminal、ordering、tool pairing、secret redaction、cancel honesty、model pairing、tool failure、approval、checkpoint、compaction、usage、capability transitions |
| 单引擎 Conformance | ✅ | ManagedLangGraphEngine + HarnessRuntimeAdapter 事件流过完整 suite |
| 跨引擎 Conformance | ✅ | managed/codex/adk/external-langgraph 各自验证配置 + capability 声明诚实 |
| 能力诚实声明 | ✅ | unavailable 字段不可被规避，强制安全用例不能绕过 |
| not_configured 诚实 | ✅ | 缺真实环境凭证时如实标注，不伪造通过 |

---

## 3. 边界（不做什么）

| 不做 | 原因 |
|---|---|
| 不向 Studio 暴露 LangGraph 类型 | 平台协议不含 StateGraph/RunnableConfig/Command/ToolNode |
| 不把本地 MEMORY.md 当企业事实源 | 企业 Memory 必须受控写入、有来源、有审计 |
| 不做 Skill 自进化 | 有意延后，非 MVP 范围 |
| 不采用实验性 Completion Cache | claw-code 实验性能力，有意不采 |
| 不要求普通用户选 Runner | 普通创建流程不展示 Codex/ADK/LangGraph 选择 |
| 不魔改用户已编译 LangGraph Graph | 兼容模式保留原框架 Loop 所有权 |
| 不把整个 Agent 跑在 Sandbox 里 | Sandbox 是 Tool（按需 provision），不是 Agent 容器 |
| 不负责完整 registry/gateway/控制面 | 归 agentengine-server、Skill Service、Sandbox Service |

---

## 4. 与开源 Harness 的区别

### 4.1 对标项目

| 项目 | 类型 | 定位 |
|---|---|---|
| **TrueForge** | 开源 harness | 通用 agent harness，MCP/skill/sandbox/approval/subagent，TS SDK + Chat UI |
| **OpenHands** | 开源 agent | 通用自主 Agent，含 Skill manifest |
| **Letta (MemGPT)** | 开源 agent | 有状态 Memory Agent + server，Core Memory Block |
| **Codex** | 开源 coding agent | Coding/Computer-use 强引擎，Rollout/长任务连续性 |
| **Claude Code** | 闭源(公开分析) | 个人编码 Agent，Prompt Section/缓存边界/分层压缩 |
| **OpenClaw** | 开源 | Context 压缩/分层，Tool Pair 边界 |
| **claw-code** | 开源 | Claude Code parity，cache-break 统计/Parity Test |
| **AutoGen/CrewAI** | 开源框架 | 多 Agent 对话/角色编排 |
| **Bee Agent** | 开源 harness | IBM 企业向，OTel trace |
| **Mem0** | 开源 Memory | Memory Extract/Dedup/服务化 |

### 4.2 横向对比矩阵

| 维度 | KsADK | TrueForge | OpenHands | Letta | Codex | Claude Code | OpenClaw |
|---|---|---|---|---|---|---|---|
| **执行** | | | | | | | |
| 图执行/Checkpoint | ✅ | ✅ | ✗ | ✗ | ✅ | ✗ | ✗ |
| Interrupt/Resume | ✅ | ✅? | ✗ | ✗ | ✅ | ✗ | ✗ |
| 流式 Token | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 多 Agent 策略 | ✅ | ✅ | ✗ | ✗ | ✗ | ✗ | ✗ |
| 长任务 Rollout | ✗(可配 32 轮) | ? | ✅ | ✗ | ✅ | ✗ | ✗ |
| **Context** | | | | | | | |
| Stable/Dynamic 分层 | ✅ | ? | ✗ | ✅ | ✗ | 部分 | ✅ |
| 压缩+校验+重注入 | ✅强 | ✅ | ? | ✗ | ✗ | ✅ | ✅ |
| **Memory** | | | | | | | |
| 受控写入/审计 | ✅强 | ? | ✗ | ✅ | ✗ | ✗ | ✗ |
| Core Block 限量 | ✅ | ✗ | ✗ | ✅ | ✗ | ✗ | ✗ |
| 图记忆/向量 | ✗ | ? | ✗ | ✅ | ✗ | ✗ | ✗ |
| 多后端(SQLite/PG) | ✅ | ✅ | ✗ | ✅ | ✗ | ✗ | ✗ |
| **Tool/MCP** | | | | | | | |
| MCP 真实调用 | ✅ | ✅ | ✅ | ✗ | ✅ | ✅ | ✗ |
| Tool Receipt 幂等 | ✅ | ? | ✗ | ✗ | ✅ | ✗ | ✗ |
| 熔断降级 | ✅ | ? | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Skill** | | | | | | | |
| 四级渐进披露 | ✅独有 | ✅ | ✅ | ✗ | ✗ | ✗ | ✗ |
| **Sandbox** | | | | | | | |
| Backend 协议 | ✅ | ✅ | ✅ | ✗ | ✅ | ✗ | ✗ |
| Sandbox-as-tool | ✅ | ✅ | ✅ | ✗ | ✅ | ✗ | ✗ |
| **治理** | | | | | | | |
| ToolPolicy | ✅ | ? | ✗ | ✗ | 部分 | ✗ | ✗ |
| 多租户编码 | ✅独有 | ? | ✗ | ✗ | ✗ | ✗ | ✗ |
| **可观测** | | | | | | | |
| OTel/OTLP | ✅ | ? | ? | ? | ✅ | ✗ | ✗ |
| Conformance 契约 | ✅ | ? | ✗ | ✗ | ✗ | ✗ | ✗ |
| **部署** | | | | | | | |
| 多副本分布式 | ✅(PG) | ✅(PG+Redis) | ✗ | ✅ | ✗ | ✗ | ✗ |
| 登录认证 | ✗(agentengine-server) | ✅(OIDC) | ✗ | ? | ✗ | ✗ | ✗ |

### 4.3 关键区别

**KsADK 与 TrueForge（最接近的竞品）**：
- 都是企业向 agent harness，都有 MCP/Skill/Sandbox/Approval/Subagent
- TrueForge 强在：流式每步（我们已补）、TS SDK、独立 UI SDK、OIDC 登录、PG+Redis 多副本
- KsADK 强在：Memory 受控写入+审计、ToolPolicy+Receipt、多租户编码、Conformance 契约、Skill 四级披露、压缩后校验+重注入
- 定位差异：TrueForge 偏"通用易用"，KsADK 偏"企业合规治理"

**KsADK 与 Codex**：
- Codex 是 Coding/Computer-use 强引擎，长任务 Rollout 是其独有
- KsADK 借鉴其 Agent Loop/Tool Pair/Sandbox Policy/Cancel/Receipt/Conformance，但不把 Coding Workspace 当通用 Memory
- Codex 作为兼容 Engine 保留，不是默认

**KsADK 与 Letta/Mem0**：
- Letta 强在 Core Memory Block + 图记忆 + server 化
- Mem0 强在 Extract/Dedup/服务化
- KsADK 借鉴 Core Block 限量 + 写入管线，但首阶段不做图记忆/向量（有意延后）

---

## 5. 吸取的开源优点

| 来源 | 吸取的能力 | 在 KsADK 的落点 |
|---|---|---|
| LangGraph | 状态图/Checkpoint/Interrupt/Resume/Subgraph/Durable Execution | ManagedLangGraphEngine（默认内核） |
| OpenClaw | Stable/Dynamic 分离、Tool Pair 边界、压缩前 flush、保留尾部、overflow 重试一次 | ContextEngine 压缩 |
| Hermes | Working Context、主动/紧急压缩、可替换 Context Engine | ContextEngine + WorkingContext |
| Letta/MemGPT | Core Memory Block 限量/写权限 | MemoryRuntime |
| OpenHands | Skill Manifest、先描述后正文、按需资源 | SkillRuntime 四级披露 |
| Codex | Agent Loop、Tool Pair、Sandbox Policy、Receipt、Cancel、Conformance | loop/ + tool_policy + tool_receipts + conformance |
| Claude Code | Prompt Section、缓存边界、Working Notes、分层压缩 | prompt_cache + ContextTrace |
| Mem0 | Memory Extract/Dedup/Conflict | MemoryCoordinator |
| Bee | OTel trace 标准观测 | OTelSpanAdapter |
| TrueForge | sandbox-as-tool、流式每步、多副本 | SandboxBackend + stream_complete + Postgres 后端 |
| claw-code | Parity Test、cache-break 统计 | Conformance suite + prompt_cache 诊断 |

---

## 6. KsADK 独有的能力（开源生态没有）

| 能力 | 说明 | 开源有无 |
|---|---|---|
| **Memory 受控写入 + 审计** | 禁止整段聊天写入、无来源写入 org、敏感标签未脱敏写入；每次写入留来源/决策/冲突/操作记录 | 全无 |
| **ToolPolicy + Receipt 幂等** | 风险分级→allow/deny/require_approval；审批通过后仅执行一次，Receipt 防重复副作用 | 仅 Codex 部分有 |
| **多租户 thread_id 编码** | tenant/user/agent/session/run 复合编码 + Checkpointer 读写校验 | 全无 |
| **Conformance 事件契约校验** | 14 个 verify 函数校验事件成对/顺序/cancel 诚实/secret 脱敏；跨引擎 matrix | 仅 claw-code 有 parity |
| **Skill 四级渐进披露** | L0→L1→L2→L3 越级拒绝 + 按需资源加载 | OpenHands 有 manifest 但非四级 |
| **压缩后关键事实校验 + 重注入** | 正则抽取 ID/金额/日期/版本/审批号比对，丢失的重新注入 | OpenClaw 有压缩但无后校验 |
| **HarnessSpec 引擎无关契约** | Revision 编译为不可变 Spec，不含 LangGraph 类型，架构守卫 | 全无 |

---

## 7. 已知不足（诚实记录）

| 不足 | 影响 | 状态 | 性质 |
|---|---|---|---|
| 长任务 Rollout | 超过 max_turns（默认 4，可配 32）后终止，需显式 resume | 有意设计（Checkpoint 可续） | 与 Codex 差距 |
| Memory 图记忆/向量检索 | 仅 keyword search，无 embedding/图 | 有意延后（方案 §9） | 与 Letta/Mem0 差距 |
| Memory server 化 | 有 LongTermMemoryService 钩子，本地仅 SQLite/Postgres | 归 agentengine-server | 与 Mem0 差距 |
| 登录认证 | 多租户编码有，登录层归 agentengine-server | 跨仓对接 | 与 TrueForge 差距 |
| TS SDK | 仅 Python SDK | 看需求 | 与 TrueForge 差距 |
| 财务编排迁移 | ksadk.orchestration 样板已删，未接入 ExecutionStrategy | 后续独立 change | 方案 §14.3 未完成 |
| Codex/ADK 事件流 Conformance | 需 app-server/真实 runtime，无环境时 not_configured | 诚实跳过 | 环境限制 |

---

## 8. 环境开关速查

```bash
# 流式 Token（首 chunk 实时输出）
KSADK_MODEL_STREAMING=1

# OTel/OTLP 标准观测导出
KSADK_OTEL_ENDPOINT=http://otel-collector:4318/v1/traces

# Postgres 分布式（Checkpoint + Memory 多副本）
KSADK_CHECKPOINT_DSN=postgresql://user:pass@host:5432/db
KSADK_MEMORY_PROVIDER=local-postgres
```

无配置 = SQLite 本地 + 无 OTel + 无流式（零破坏默认）。

---

## 9. 测试与质量基线

| 指标 | 值 |
|---|---|
| harness 测试用例 | 644 |
| 通过 | 638 passed / 6 skipped |
| harness 模块数 | 86 |
| harness 代码行数 | ~23,800 |
| Conformance verify 函数 | 14 |
| RuntimeEvent 事件类型 | 30+ |
| 架构守卫 | loop/ 无 langgraph import、契约层无引擎框架 |
| ruff | 干净 |
