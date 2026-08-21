# KsADK Prompt、Context 与 Memory 实现说明

> 状态：随当前实现维护的工程合同
>
> 范围：`ksadk-python` 数据面 Runtime、Runner Adapter、Studio 本地构建与运行
>
> 不包含：平台控制面、完整 Memory 管理服务、Skill 注册治理和生产安全策略管理

## 1. 文档目的

本文说明 KsADK 当前 Prompt、Context 与 Memory（下称 PCM）的实际实现边界，供代码评审、
Runner 接入、故障排查和兼容性验证使用。

PCM 的目标不是把所有 Runner 改造成同一个内部实现，而是在保留 Runner 原生 Agent Loop 的
前提下，提供统一的配置合同、可审计语义、能力声明和按能力投影。

本实现重点解决以下问题：

1. Prompt 来源、顺序、覆盖关系和版本不可解释；
2. Prompt、历史、记忆、Tool Result 和当前输入缺少请求级预算；
3. KsADK 与 Runner 可能重复注入历史或重复执行压缩；
4. 长会话压缩后缺少可恢复的当前工作状态；
5. 长期记忆缺少统一 scope、候选、冲突、敏感信息和失败语义；
6. 平台计划、交付 Runner 的内容和模型实际用量容易混为一谈；
7. 本地构建与云端部署缺少同一份 AgentVersion 运行合同。

## 2. 范围与非目标

### 2.1 当前范围

- 结构化 Prompt 来源与确定性编译；
- Runner Context capability 与 ownership 合同；
- 请求级 ContextItem、ContextBudget、ContextPlan 和 ContextDecision；
- KsADK Hosted 模式下的候选收集、预算规划、组装和有序降载；
- Working State、Checkpoint 和压缩恢复接线；
- 长期记忆的召回、候选、策略解析、Provider 和结构化事件；
- Agent Revision、Build、RunSpec 和 Trace 中的 PCM 配置与证据；
- LangGraph、ADK、Codex 等 Runner 的差异化投影与保守降级。

### 2.2 非目标

- 不统一或替换所有 Runner 的 Agent Loop；
- 不伪装成能够观察 Native Runtime 未暴露的最终模型输入；
- 不把 Session Transcript 整体当作长期记忆；
- 不把 Runner 原生 thread、checkpoint 或缓存自动提升为平台事实；
- 不在 SDK 仓库实现完整多租户 Memory 控制面；
- 不把 Prompt 当作 approval、sandbox、鉴权和 Secret 防护的替代品；
- 不在本阶段实现 Skill 自进化或自动发布。

## 3. 统一术语

| 概念 | 当前定义 | 生命周期 |
| --- | --- | --- |
| Prompt | Agent 长期遵守的规则、身份、策略和资源说明 | AgentVersion / Build |
| Context | 本次模型调用实际需要的工作集 | Turn / Request |
| Transcript | User、Assistant、Tool、Approval 等追加式事件事实 | Session |
| Working State | 当前目标、阶段、约束、下一步和 pending state | Session / Checkpoint |
| Memory | 对未来会话仍有价值的精选事实或持续状态 | 跨 Session |
| Checkpoint | 可恢复边界上的摘要、Working State 和 Runner 引用 | Session / Run |
| Runner | 执行 Agent Loop 的框架或原生 Runtime | Build / Deployment |

Prompt 不等于全部模型输入；Context 不等于完整 Session；Memory 不等于聊天归档；Working
State 不用于保存跨会话偏好。

## 4. 总体责任边界

PCM 使用两个相互独立的维度描述运行方式：

- `deployment_mode`：`local`、`ksadk_managed_cloud`、`external_managed`；
- `integration_mode`：`ksadk_hosted`、`framework_assisted`、`native_runtime`。

部署在哪里不决定谁拥有最终 Context。一个云端部署的 Codex Runtime 仍可能由 Codex 持有
history 和 compaction；一个本地 LangGraph Agent 也可以由 KsADK 接管 Context 规划。

### 4.1 Integration Mode

| 模式 | KsADK 责任 | Runner 责任 |
| --- | --- | --- |
| `ksadk_hosted` | 编译 Prompt、选择候选、预算、组装、压缩和投影 | 执行 Agent Loop 与模型调用 |
| `framework_assisted` | 提供统一配置、Prompt/Memory 语义和证据 | 映射到 framework instruction/state/session/store |
| `native_runtime` | 传递版本化配置、外部 Memory hook 和可得证据 | 持有 thread、history、compaction 和最终输入 |

### 4.2 Runner Capability

`ContextCapabilities` 显式声明：

- `prompt_owner`；
- `history_owner`；
- `compaction_owner`；
- `memory_owner`；
- `skill_owner`；
- 支持的 Prompt 投影位置；
- Memory 读写能力；
- token accounting 精度；
- 是否支持 Context snapshot。

已知 Runner 使用显式默认值，不依赖类名或 `hasattr` 猜测。未知 Runner 使用
`framework_assisted + opaque` 的保守合同，不自动开启行为型接管。

### 4.3 当前 Runner 边界

| Runner | 默认模式 | 关键边界 |
| --- | --- | --- |
| LangGraph | `framework_assisted`，可升级为 `ksadk_hosted` | KsADK 可拥有 Prompt、history 和 compaction；投影为 SystemMessage/state |
| ADK | `framework_assisted` | ADK 拥有 instruction、SessionService 和 memory_service 的原生语义 |
| LangChain / DeepAgents | `framework_assisted` | KsADK 提供 Prompt 投影，history 与 compaction 默认交框架 |
| Codex | `native_runtime` | Codex 持有 base_instructions、thread、history 和 compaction；KsADK 不重复注入完整 Transcript |
| 未知自定义 Runner | 保守 assisted | 无可靠能力声明时只记录 opaque 证据，不接管输入 |

如果运行证据与 capability 声明不一致，平台记录 capability mismatch，并应停止对该 Runner
启用行为型 Context Engine，避免错误接管。

## 5. Prompt 实现

### 5.1 Prompt 来源

`ResolvedPromptSources` 聚合当前可用来源：

- `agent_system`：Agent 身份和系统规则；
- `agent_task`：任务执行契约；
- `request_instructions`：本次请求附加指令；
- `platform_policy_source`：可信平台策略来源，可为空。

生产平台安全规则必须来自可信 `PlatformPolicySource`。当前默认实现
`EnvPlatformPolicySource` 只用于本地开发和测试；未配置时不会虚构 platform safety 内容。

### 5.2 PromptSection

Prompt 被拆分为以下稳定语义区段：

1. `platform_safety`；
2. `agent_identity`；
3. `agent_policy`；
4. `runtime_capabilities`；
5. `resource_manifest`；
6. `request_instructions`。

每个 `PromptSection` 带有 `section_id`、`kind`、`source`、`priority`、`trust_level`、
`stability`、`merge_policy` 和 `overridable`，因此平台可以解释规则来源和覆盖决策。

动态历史、长期记忆正文、Tool Result 和附件不属于 PromptSection，它们进入 ContextPlan。

### 5.3 编译规则

`PromptCompiler` 对相同输入产生确定结果：

1. 按 priority、kind 和 section_id 排序；
2. 统一换行与尾部空白；
3. 按 `replace`、`append`、`merge_unique`、`protected` 显式合并；
4. 拒绝 user/untrusted 来源声明 `platform_safety`；
5. 空 section 不输出占位内容；
6. 生成 content hash、section hash、section token 和 stable prefix hash。

`CompiledPrompt.content` 是 canonical 内容，不自动代表模型最终看到的物理输入。

### 5.4 Runner 投影

投影只改变合法承载形式，不改变 section 顺序、信任级别和 hash：

- Hosted / LangGraph：`system_message`；
- ADK：`instruction`；
- Codex：`base_instructions`；
- Assisted Runner：按 capability 选择 state、session 或 framework 原生位置。

正式 Prompt 接管需要同时满足：

1. 全局 Prompt Compiler 开关允许；
2. 当前 Build 标记 `prompt_ownership=ksadk`；
3. Runner capability 允许 KsADK 投影；
4. 不是 resume 等禁止重写输入的路径。

不满足时回退旧输入路径，不进行双重注入。

## 6. Context 实现

### 6.1 ContextItem

当前支持的主要 Context 类型包括：

- `compiled_prompt`；
- `core_memory`、`recalled_memory`；
- `resource_manifest`、`skill_content`；
- `checkpoint_summary`、`working_state`；
- `history_round`；
- `tool_result`、`attachment_context`；
- `current_input`。

每个 ContextItem 包含来源、信任级别、优先级、token 估算、required/droppable/truncatable
属性、group_id、序列范围、hash 和 provenance。

外部 Memory、Knowledge、Tool、Hook 和文件内容即使来自受信基础设施，也作为不可信上下文
处理，不能覆盖 Prompt 规则。

### 6.2 原子分组

`group_id` 用于保护协议完整性。以下内容必须原子保留或原子丢弃：

- 一轮 User/Assistant 对话；
- Tool Call 与对应 Tool Result；
- Approval Request 与 Approval Response；
- Responses API function call 与 function output。

Planner 不允许产生孤立 Tool Result 或缺失配对的协议历史。

### 6.3 请求级预算

`ContextBudget` 统一描述：

- 模型上下文窗口；
- 输出预留；
- reasoning 预留；
- safety buffer；
- 最大输入；
- soft/hard 阈值；
- 分区建议上限。

AgentVersion 的 `maxInputTokens` 和 `reserveOutputTokens` 优先于模型 metadata。分区预算不是
静态预占，未使用预算可以回流，但 required 内容先锁定。

默认策略包含 Prompt、资源清单、核心记忆、召回记忆、Checkpoint、Working State、近期
历史以及 Tool/附件分区。具体数值可由 AgentVersion 或部署策略覆盖。

### 6.4 Planner 决策

`ContextPlanner` 输出 `ContextPlan` 和逐项 `ContextDecision`。决策动作包括：

- `included`：原样保留；
- `summarized`：使用摘要替代；
- `truncated`：按允许策略截断；
- `dropped`：因预算或低价值丢弃。

required 的 Prompt 和当前输入优先保留；Working State、协议配对和高价值记忆优先于低价值
旧历史。单个大型 Tool Result 可按策略降载，避免提前占满窗口。

### 6.5 Context Contributor

Contributor 用于按需贡献 Workspace Rules、Memory Recall 等候选项。每个 Contributor 有独立
超时和失败模式；默认失败语义为 skip/warn，不因非关键召回故障阻断主回答。

Contributor 只能提供候选项，不能直接改写受保护 Prompt，也不能绕过 Planner 预算。

### 6.6 Hosted Pipeline 门控

当前完整 Context 规划只在以下条件同时成立时进入正式路径：

1. `KSADK_CONTEXT_ENGINE_V2_ENABLED` 未关闭；
2. AgentVersion rollout 为 `enabled`；
3. `prompt_integration_mode=ksadk_hosted`；
4. 已生成有效 CompiledPrompt；
5. Runner capability 的 `prompt_owner=ksadk`。

当前主要正式接管目标是 LangGraph。Codex 等 Native Runtime 不进入 Hosted Pipeline，避免
KsADK 与原生 Runtime 同时管理 history 和 compaction。

### 6.7 Working State 与压缩

Working State 保存当前目标、约束、进度、下一步、错误修正和 pending state。它随
ContextCheckpoint 持久化，在压缩后重新进入候选集。

达到主动阈值时，系统优先降载 Tool Result、执行小范围压缩并更新 Checkpoint；达到紧急
阈值或 Prompt Too Long 时，可在 best effort Memory Flush 后执行语义压缩、重建
ContextPlan，并最多进行一次受控重试。

Native Runtime 内部压缩不可见时，KsADK 只报告自己计划和投影的内容，不宣称掌握原生摘要。

## 7. Planned、Projected 与 Actual

平台必须区分三类事实：

| 口径 | 含义 |
| --- | --- |
| `planned` | KsADK 计划选择、裁剪和预算的内容 |
| `projected` | KsADK 实际交给 Runner 的内容 |
| `actual` | Runner 或模型 Provider 报告的实际使用内容 |

每项证据同时标注精度：

| 精度 | 含义 |
| --- | --- |
| `exact` | KsADK 拥有最终请求并用匹配 tokenizer 计算 |
| `runtime_reported` | Runner 或 Provider 直接上报 |
| `estimated` | KsADK 使用 tokenizer 或启发式估算 |
| `opaque` | Runtime 未暴露足够信息 |

`ContextPlan` 在 assisted/native 模式下不等于模型实际所见。缺失 actual 时必须展示为未上报
或不可见，不能用 projected 数值冒充实际输入。

Trace 只保存 hash、token、来源、决策和状态，不保存完整 Prompt、Memory 或敏感正文。

## 8. Memory 实现

### 8.1 生命周期

长期记忆按以下流程处理：

1. Session 事件、用户明确保存或压缩前 flush 产生输入；
2. 提取 `MemoryCandidate`；
3. 分类 scope/type，检测 Secret、PII、重复和冲突；
4. 执行 add、update、delete 或 ignore；
5. Provider 持久化版本化 `MemoryRecord`；
6. 后续请求按 scope、类型、分数和 token 预算召回；
7. 召回结果作为 `ContextItem(recalled_memory)` 进入 Planner。

长期 Memory 适合保存用户明确偏好、稳定项目事实和经确认业务状态。当前下一步、临时错误、
待审批事项和模型猜测不直接写入长期 Memory。

### 8.2 Scope 与隔离

Memory 支持 tenant、workspace、agent、user 等 scope。Provider 的查询和写入必须携带明确
scope_id，不能仅依赖自然语言或 Session 名称隔离。

Agent scope 防止不同 Agent 之间互相召回偏好；user/tenant/workspace scope 由 AgentVersion
策略显式选择。Runner 原生 Memory 不会自动成为平台 Memory 的事实源。

### 8.3 写入策略

`ResolvedMemoryPolicy` 是 recall、candidate extraction 和 flush 的唯一决策入口：

| rollout / mode | 行为 |
| --- | --- |
| `off` | 不提取、不写入；recall 是否启用由 MemorySpec 决定 |
| `shadow` | 生成 Candidate 和审计事件，不提交 Provider |
| `enabled + explicit_only` | 只提交用户明确要求保存的候选 |
| `enabled + candidate` | 按 Candidate、冲突和安全策略提交 |

`memory.enabled=false` 会关闭 recall 和 write。环境变量只用于兼容缺少新字段的旧 AgentVersion，
不能覆盖已锁定 Build 的明确策略。

### 8.4 Provider

当前 Provider 解析支持：

- `local-default` / `local-sqlite`：本地持久 SQLite；
- `local-inmemory`：测试用内存实现；
- `http`：HTTP LTM Backend；
- `sdk`：SDK LTM Backend；
- `longterm-service`：`LongTermMemoryService` 环境配置。

Build 只保存 `providerRef`，不保存 AK/SK、Token 或连接凭证。生产凭证由部署环境或控制面
注入。本地 SQLite 用于开发验证，不自动满足多副本、跨滚动更新和生产高可用要求。

### 8.5 冲突与删除

MemoryRecord 带 status、valid_from/valid_to、expires_at、content_hash 和 version。新偏好与旧
偏好冲突时，应更新或 supersede 同一 slot，而不是把两个互斥事实同时作为 active 结果返回。

Candidate 命中 Secret、API Key、Cookie、Authorization、签名 URL、DSN、PII 等敏感标签时
硬拒绝，不写入 Provider。

### 8.6 Memory 事件

运行时发出以下不含正文的结构化事件：

- `memory.recall.completed/projected/empty/failed`；
- `memory.candidate.created/rejected`；
- `memory.flush.completed/failed`。

`recall.completed` 表示 Provider 返回候选；`recall.projected` 表示已交付 Runner；二者都不
证明模型最终采纳。Provider 失败不伪装为空结果，partial flush 不误报为完整成功。

## 9. AgentVersion 与 Build 合同

PCM 配置属于 AgentVersion，并随不可变 Build 固化。运行时必须从 resolved spec、manifest
或 RunSpec 读取，不能依赖可变 Draft，也不能按 Runner 类名临时猜测。

核心配置示例：

```yaml
context:
  maxInputTokens: 32000
  reserveOutputTokens: 4096
  ownership: auto
  promptOwnership: framework
  policyVersion: context-v2
  rollout:
    contextEngine: shadow
    memoryWrite: shadow
  contributors:
    workspaceRules: null
    skillManifest: null
    memoryRecall: null
memory:
  enabled: false
  providerRef: local-default
  recall:
    enabled: true
    maxTokens: 1600
    topK: 8
    minScore: 0.45
  write:
    mode: candidate
    flushBeforeCompaction: true
  scopes: [workspace, agent, user]
```

默认 rollout 为 shadow，便于先采集证据、比较计划，再逐 Agent 启用正式行为。修改 Draft 后
不会改变已存在 Build；新策略必须生成新的 Revision/Build。

Codex Manifest 必须持久化 resolved context/memory 配置，使 Codex RunSpec 能获得相同的
Memory 策略；不能从只保存 framework Agent 的 Draft Store 反向读取。

## 10. 降级与失败语义

PCM 遵循“非关键增强失败不阻断主回答，安全和协议错误不静默”的原则：

- Prompt protected section 冲突：编译失败并记录原因；
- Context Contributor 超时：按 failure mode skip、warn 或 fail；
- Context Engine 异常：回退旧路径，不双重组装；
- Memory recall 失败：发 `memory.recall.failed`，主回答可继续；
- Memory flush 失败：发 `memory.flush.failed`，不能报告保存成功；
- Tokenizer 不可用：降级 heuristic，并标注 `estimated`；
- Native Runtime 不可见：标注 `opaque` 或 `runtime_reported`；
- Prompt Too Long：最多一次受控重试，避免无限循环。

全局紧急开关只能收紧或关闭行为，不应绕过 AgentVersion 的安全配置。

## 11. 安全与审计

必须遵守以下不变量：

1. Prompt/Memory 正文默认不进入 Trace；
2. 平台安全 section 不接受 user/untrusted 来源；
3. 外部 Context 不能覆盖 Prompt；
4. Build 不保存凭证；
5. Memory 写入前执行敏感信息检测；
6. Tool 与 Approval 协议配对不可被预算拆散；
7. Provider unauthorized、timeout、failed 与 empty 必须可区分；
8. estimated/opaque 数据不得展示为 exact；
9. 运行行为必须可由 Revision、Build、policy version 和 hash 追踪；
10. Prompt 规则不能替代代码级鉴权、审批和沙箱。

## 12. 可观测合同

每次 Run 可提供以下证据，具体字段取决于 Runner capability：

- Prompt compiler version、section count、section hash、stable prefix hash；
- Runtime type、integration mode 和各类 owner；
- planned/projected/runtime-reported input tokens；
- tokens by kind 和 ContextDecision；
- contributor 状态和 compaction 状态；
- Memory recall/candidate/flush 事件；
- Agent Revision、Build、模型和 policy version；
- accounting accuracy 与 capability mismatch。

Studio 普通用户界面只展示“规则已应用、是否使用记忆、是否发生上下文整理、运行是否正常”
等可理解结论；token 分区、hash、planned/projected/actual 和失败码放在开发与排障详情。

## 13. 兼容与迁移原则

1. 先 shadow 采集，再逐 Runner 启用行为；
2. 每次只接管一个明确 ownership，不同时维护两套 history/compaction；
3. 新 AgentVersion 使用结构化 context/memory 字段；旧版本保留环境变量 fallback；
4. 未知 Runner 默认 opaque，不因“看起来像 LangGraph”而自动接管；
5. Native Runtime 只做合法投影，不承诺跨 Runner 原生恢复；
6. Transcript 可回放不等于 thread/checkpoint 可迁移；
7. Provider 迁移保持 scope、version、delete 和冲突语义；
8. 每个行为开关必须可回退，回退后主链路仍可运行。

## 14. 验收标准

### 14.1 Prompt

- 相同来源编译得到稳定 hash；
- protected section 覆盖被拒绝；
- section 来源、顺序和 token 可审计；
- LangGraph 接管后只有一个 system message；
- Codex system/task 合并进入 base_instructions，分段 hash 仍保留；
- 未开启接管时旧 Runner 输入保持兼容。

### 14.2 Context

- required Prompt 和当前输入不会被预算丢弃；
- Tool Call/Result 与 Approval 配对保持完整；
- 大型 Tool Result 可降载；
- ContextPlan 记录 included/summarized/truncated/dropped 原因；
- Working State 在压缩后恢复当前目标和最新修正；
- planned/projected/actual 缺失时不伪造数值；
- Native Runtime 不进入 Hosted Pipeline。

### 14.3 Memory

- disabled/off 不产生候选和写入；
- shadow 产生候选事件但不落库；
- explicit_only 过滤非明确保存请求；
- candidate 模式执行冲突、去重和敏感信息检查；
- Recall 真实触发 completed/projected/empty/failed；
- Provider partial/failure 不误报成功；
- agent scope 防止不同 Agent 之间的信息污染；
- 新事实可 supersede 同一 slot 的旧事实。

### 14.4 Build 与 Runner

- 修改 Draft 后旧 Build hash 和配置不变；
- LangGraph Build → Run 可产生 Prompt/Context evidence；
- Codex RunSpec 保留 Context/Memory 字段；
- ADK、LangGraph、Codex 均按 capability 投影而非强制统一；
- 本地和云端读取同一 resolved spec，差异只来自部署和 Provider 能力。

## 15. 当前限制

- 生产级 PlatformPolicySource 仍需由控制面或部署配置提供；
- LangGraph 是当前完整 KsADK Hosted Context 链路的主要目标；
- ADK/Codex 的最终模型输入受其原生 Runtime 可见性限制；
- 本地 SQLite 不等于生产长期记忆服务；
- Token 统计可能因模型 tokenizer 不可用而降级为 estimated；
- Native thread、ADK invocation 和 LangGraph checkpoint 不能互相等价迁移；
- Studio 展示的是运行解释和证据，不应暴露完整 Prompt 或 Memory 正文。

## 16. 代码索引

| 模块 | 责任 |
| --- | --- |
| `ksadk/prompts/` | Prompt 来源、模型、编译、resolved source 和投影 |
| `ksadk/context_engine/capabilities.py` | Runner ownership/capability 合同 |
| `ksadk/context_engine/models.py` | ContextItem、Budget、Plan、Decision |
| `ksadk/context_engine/planner.py` | 请求级选择、预算和降载决策 |
| `ksadk/context_engine/hosted_pipeline.py` | Contributor → Planner → Assembler 主链路 |
| `ksadk/conversations/runtime_preparation.py` | PCM 门控、Working State 和运行准备 |
| `ksadk/conversations/runtime_input.py` | Prompt/Memory 投影和运行输入 |
| `ksadk/conversations/runtime_compaction.py` | 压缩、Checkpoint 与恢复 |
| `ksadk/memory/` | Memory 数据模型、策略、Provider、候选和事件 |
| `ksadk/studio/contracts.py` | AgentVersion ContextSpec/MemorySpec |
| `ksadk/studio/manifest_resolver.py` | Build resolved spec 固化 |
| `ksadk/studio/framework_run.py` | Framework Runner RunSpec 接线 |
| `ksadk/studio/codex_manifest.py` | Codex Manifest PCM 持久化 |
| `ksadk/studio/codex_run.py` | Codex RunSpec PCM 投影 |
| `ksadk/conversations/runtime_observability.py` | Prompt/Context Trace 证据 |

本文描述的是当前代码合同。新增 Runner 或调整 ownership 时，必须同步 capability、投影、
Conformance 测试和本文档，不能只在 Studio 增加一个展示标签。
