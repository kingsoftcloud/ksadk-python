# KsADK Prompt、Context 与 Memory 技术实现方案

> 状态：实施设计稿
> 适用分支：`feature-prompt-context-optimize`
> 代码基线：`agentkit-studio-phase1` 的 RuntimeAdapter-first 架构（当前分支 `HEAD 0102b30`）
> 范围：仅覆盖 Prompt、运行时上下文（Context）和长期记忆（Memory）
> 不包含：完整 Skill 管理面、Knowledge/RAG 重构、控制面服务实现和发布流程；仅定义与 Studio Agent Revision/Build/Runtime 的必要接缝

## 1. 文档目的

本文用于指导 `ksadk-python` 后续代码修改，目标是在不破坏现有 Studio Agent Revision、`RuntimeExecutor`、`RuntimeAdapter`、Session、RuntimeEvent、Checkpoint、Approval、Tool Call 和不同 Runner 适配边界的前提下，建立一套本地与云端运行一致、可预算、可压缩、可审计、可扩展的 Prompt/Context/Memory 机制。

本文把“部署在哪里”和“谁拥有 Context”作为两个独立维度。本地可使用进程内或 SQLite Provider，云端可使用 Memory Service、模型目录和远端 Session Store；但云端托管不自动意味着 KsADK 接管最终 Prompt/History/Compaction。对于 Codex、未来的 Claude Agent SDK 等原生 Agent Runtime，即使由 KsADK 云端部署，最终上下文组装和压缩算法仍由原生 Runtime 持有，KsADK 不重复实现。

## 2. 核心结论

本次改造采用以下主线：

1. **Prompt 是版本化的稳定指令，不等于一次模型请求的全部输入。**
2. **Context 是 Runtime 每次调用前，根据预算动态组装的工作集。**
3. **Session Transcript 是 append-only 事实记录，不等于长期记忆。**
4. **Working State 是当前 Session 的结构化工作面，属于 ContextCheckpoint，不属于长期记忆。**
5. **Memory 是跨 Session 的精选事实和持续状态，必须检索后才进入 Context。**
6. **Skill 属于程序性知识，不并入 Memory；这里只规定它如何占用 Context。**
7. **压缩只改变投影，不删除原始 Transcript。**
8. **本地与云端使用同一 Context 协调层、语义模型和策略合同；云端只替换 Provider 和策略来源。**
9. **多 Runner 统一语义、合同、治理和观测，不强行统一内部 Agent loop、Session、compaction 和最终消息格式。**
10. **每个 RuntimeAdapter 必须声明 Prompt、History、Compaction、Memory 和 Skill 的 ownership；平台只接管声明属于 KsADK 的部分。**
11. **ContextPlan 必须标明 exact、runtime_reported、estimated 或 opaque，不能把平台计划错误表述为模型实际所见。**
12. **动态 Context 扩展必须通过有来源、信任级别、预算和超时的 Contributor 进入，不能任意拼接到高信任 Prompt。**
13. **DeploymentMode 与 Context ownership 正交：前者描述运行位置和生命周期责任，后者描述最终输入由谁管理。**
14. **通过 Codex SDK/CLI 部署的 Codex 是 Managed Codex Runtime，不是 KsADK Harness；只有 KsADK 拥有 Agent Loop 和最终模型输入时才是 Harness。**

目标调用链以当前 RuntimeAdapter-first 主链路为基础。核心不变量是：**同一个 Turn 的 canonical Prompt 和 ContextPlan 只生成一次；RuntimeAdapter 只做目标 Runtime 的合法投影，不重复规划或拼接第二套上下文。**

```text
Studio Agent Revision / Build / ContextSpec
              │
              ▼
Agent instructions / platform policy
              │
              ▼
        Prompt Compiler
              │ CompiledPrompt
              ▼
Current input ──► Context Engine ◄── Session transcript/checkpoint
                      ▲    ▲                  └── Working State
                      │    └──────── Memory retrieval
                      └───────────── Contributors/Skill/tool/resource manifests
                              │
                              ▼
                 ContextPlan + semantic envelope
                              │
                              ▼
                     RuntimeExecutor
                              │
                              ▼
                    RuntimeAdapter Projection
                    hosted / assisted / native
                              │
                              ▼
                     Runtime / Runner
                              │
                events / usage / tool results
                              │
             ┌────────────────┴────────────────┐
             ▼                                 ▼
       Session Store                    Memory candidates
       append-only                      policy/commit
```

### 2.1 首要实施顺序

本方案不是要求同时重构 Prompt、Context、Memory 和 Skill。第一阶段的首要目标是先让 Runtime 对“平台计划和投影了什么、能够证明模型实际使用了什么、占用了多少 token、为什么发生裁剪”给出带精度等级的答案，然后再改变运行行为。

建议严格按以下顺序推进：

| 优先级 | 工作 | 首先解决的问题 | 本阶段是否改变线上行为 |
|---|---|---|---|
| P0 | 建立 Context 可观测基线 | 当前无法准确解释 Prompt、History、Memory、Tool 各占多少预算，也无法量化 PTL 原因 | 否 |
| P0 | 引入 PromptSection/CompiledPrompt | instructions、安全规则、资源说明在不同 Runner 中可能重复、覆盖或顺序漂移 | 先 shadow，后切换 |
| P0 | 引入 ContextItem/ContextPlan | 当前只有历史压缩计划，没有完整请求级上下文计划 | 先 shadow，后切换 |
| P0 | 建立 RuntimeAdapter Context Conformance | 当前部分逻辑仍依赖框架类型和类名推断 ownership，无法证明各 RuntimeAdapter 的实际接入边界 | 否 |
| P1 | 接入精确 TokenCounter 与分区预算 | 启发式 token 估算无法稳定支持不同模型和中英文内容 | 是，可回退 |
| P1 | Tool Result 预算与有序降载 | 单个大工具结果可能在整体 compaction 前直接挤爆请求 | 是，可回退 |
| P1 | 在 KsADK-owned compaction 上实现双阈值 | 仅接近模型上限才处理，容易增加同步延迟和 PTL 风险 | 是，可回退 |
| P1 | 修正 Memory Recall 失败语义 | 当前 Provider 异常可能作为普通文本进入模型上下文 | 是，低风险 |
| P1 | 引入 Session Working State | 压缩摘要描述“发生过什么”，但不能稳定表达“现在做到哪、下一步是什么” | 是，可回退 |
| P2 | 实现 Memory Flush 与 Candidate 管线 | 旧历史被压缩前，重要跨 Session 事实没有受控沉淀路径 | 是，默认可关闭 |
| P2 | 实现 Memory Provider v2 | local/http/sdk 的 scope、版本、删除和冲突语义不统一 | 是，兼容迁移 |
| P2 | 引入受控 ContextContributor | Git、规则文件、Hook、外部资源缺少统一预算、信任和失败语义 | 是，逐个启用 |
| 后续 | Skill 评测、漂移观测、自进化 | Skill 是否有价值、是否退化以及如何产生新版本 | 否，本期不做 |

第一批代码不应直接修改所有 Runner，也不应先建设复杂长期记忆。首个 PR 只新增模型、capability/ownership、shadow plan、统计和 invariant 测试；证明计划结果正确后，只让 `ksadk_hosted` 路径的新 assembler 接管实际请求，assisted/native 路径使用对应 Projection。

### 2.2 参考架构、借鉴内容与解决的问题

本方案的主要依据仍是 KsADK 当前实现。外部项目用于验证设计取舍，而不是作为需要逐项复制的功能清单。

| 参考 | 借鉴的架构或思想 | 解决 KsADK 的问题 | 明确不照搬 |
|---|---|---|---|
| KsADK 当前实现 | append-only SessionEvent、API round、pinned state、Context Checkpoint、分层 compaction、PTL retry | 保留现有审计、恢复、审批、工具配对和多 Runner 兼容能力 | 不另建一套平行 Session/Context 系统 |
| KsADK AgentKit Studio Phase 1 | Agent Revision/Build、RuntimeRef、RuntimeCatalog、RuntimeExecutor、RuntimeAdapter Registry、统一 RuntimeEvent | 给 Prompt/Context/Memory 找到稳定的产品配置归属和运行时接入点；避免继续按 Runner 文件分散建设 | 不把控制面生命周期或完整 Registry Server 搬入 SDK |
| OpenClaw | Skill 渐进披露、稳定 Prompt 与动态上下文分离、核心记忆与按需检索、压缩前记忆整理 | 降低 Skill 正文常驻 token；避免 Prompt 频繁变化；防止重要事实随旧历史压缩而消失 | 不把本地 `MEMORY.md` 作为云端事实源，不把单用户目录权限模型搬到多租户平台 |
| Hermes | Working Context、Session、Memory、Skill 分层；主动压缩与紧急兜底；可替换 Context Engine | 解决概念混用、接近窗口末端才压缩、各 Runner 自行拼接上下文的问题 | 不直接采用固定 token 常数，不在本期实现 Agent 自主修改正式 Skill |
| Letta/MemGPT | 有名称、描述、大小限制和写权限的 Core Memory Block；大内容按需检索 | 给“始终进入上下文的少量记忆”建立显式数据模型和硬预算 | 不把所有 Memory Block 无限制常驻 Prompt |
| OpenHands | 初始只暴露 Skill 描述，真正需要时显式加载正文 | 验证 KsADK 当前 Skill manifest + on-demand 路径是合理方向 | 不将 OpenHands 的 Agent 运行模型引入 KsADK |
| LangGraph | thread-scoped short-term state、namespace-scoped long-term store、可插拔 Store | 统一本地和云端 Memory Provider 边界，明确 Session 与长期记忆作用域 | 不要求 KsADK 用户采用 LangGraph，也不魔改已编译 graph |
| Mem0 | 记忆写入分成提取、去重、冲突判断和 add/update/delete | 避免把整段聊天或模型猜测直接写入长期记忆 | 不在第一阶段引入其完整服务和图记忆依赖 |
| RULER 类长上下文方法 | 按长度、位置、干扰、多跳和聚合逐级测试 | 验证 Context Engine 在 4K～长窗口下不是“能塞进去就算可用” | 不把通用模型榜单分数当成 KsADK 业务验收结果 |
| Claude Code 第三方源码分析 | Prompt section 缓存边界、分层 compaction、Session Working Notes、压缩后状态重注入、Context 分类统计 | 补齐稳定 Prompt、缓存失效诊断、长 Session 连续性和有序降载闭环 | 不复制泄露/重建源码和 Prompt 原文，不把第三方结论当官方合同 |
| claw-code Rust Runtime | Prompt Builder、指令发现上限、tool pair 边界保护、cache-break 统计、契约/Parity 测试 | 强化 Prompt 输入约束、缓存诊断和 Runner Conformance | 不采用其 completion cache，不以 museum exhibit 的实现替换 KsADK 现有 runtime |
| 知乎汇总文章 | 用于建立 Claude Code Agent Loop、Prompt、Context、Session、Hook 的全局阅读索引 | 帮助核对概念关系和继续定位源码证据 | 仅作二手导航，不作为实现事实的唯一依据 |
| AWS Bedrock AgentCore | Harness 与 Runtime 分离；Harness 拥有 Agent Loop，Runtime 托管第三方框架；Memory/Identity/Gateway/Evaluation 模块化 | 明确“云端托管”不等于“平台拥有 Context”，支撑 KsADK 双产品模式 | 不把 AgentCore Runtime 支持框架误写成统一了框架内部 Prompt/Context |
| Microsoft Foundry Hosted Agents | Hosted Runtime 与 Session/Conversation 边界分离；不同 API 的 conversation owner 不同 | 指导 KsADK 分开建模运行沙箱、语义会话和消息历史 | 不假定容器托管后平台天然拥有框架内部会话 |
| Google Gemini Enterprise Agent Platform | 对象、源码、Dockerfile、镜像和 Git 多种构建方式汇聚到托管 Runtime；Session、Memory、Evaluation、Sandbox 服务化 | 对齐 KsADK 双构建统一云部署，并保持平台服务与 Runner 内部状态的边界 | 不再把旧版框架支持等级表当成当前稳定合同 |
| Codex SDK / MCP 官方合同 | SDK 创建、恢复本地 Codex thread；Codex 可作为 MCP server 被外层 Agent 编排 | 明确 Managed Codex 和 Codex-as-subagent 的嵌套 ownership | 不把 `base_instructions`、thread 或 compact 能力解释为 KsADK 拥有最终 Context |

主要参考资料：

- [OpenClaw Memory 文档](https://docs.openclaw.ai/concepts/memory)与[OpenClaw Skills 文档](https://docs.openclaw.ai/tools/skills)：用于参考记忆分层、渐进披露和运行时加载边界。
- [Hermes Agent](https://github.com/NousResearch/hermes-agent)：用于参考 Session、Memory、Skills 和上下文压缩的职责划分。
- [Letta Context Hierarchy](https://docs.letta.com/guides/core-concepts/memory/context-hierarchy)与[Memory Blocks](https://docs.letta.com/guides/core-concepts/memory/memory-blocks)：用于参考 Core Memory 的结构化和预算约束。
- [OpenHands AgentSkills](https://docs.openhands.dev/sdk/guides/skill)：用于参考 Skill 描述索引和按需加载。
- [LangGraph Memory](https://docs.langchain.com/oss/python/concepts/memory)：用于参考短期/长期记忆和 namespace 边界。
- [Mem0](https://github.com/mem0ai/mem0)：用于参考 Memory Candidate 的写入、更新和检索流程。
- [NVIDIA RULER](https://github.com/NVIDIA/RULER)：用于参考长上下文测试的长度、位置、多目标和多跳设计。
- [liuup/claude-code-analysis](https://github.com/liuup/claude-code-analysis)：第三方静态分析材料，用于研究 Prompt section、Context 管理、Session Memory 和 compaction；不是 Anthropic 官方仓库，引用时必须回到具体代码证据并遵守合规边界。
- [ultraworkers/claw-code](https://github.com/ultraworkers/claw-code)：主要参考其 Rust runtime 的 Prompt Builder、compaction invariant、prompt cache-break 统计和 parity test 思想；仓库自身明确不是严肃生产项目。
- [《claude code 源码优秀解读整理》](https://zhuanlan.zhihu.com/p/2022605516262614921)：二次汇总索引，只用于交叉阅读，不作为单独技术事实源。
- [Amazon Bedrock AgentCore Developer Guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/)：用于区分 Harness 深度接管与 Runtime 开放托管。
- [Microsoft Foundry Agent Service](https://learn.microsoft.com/en-us/azure/foundry/agents/overview) 与 [Hosted Agent Sessions](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)：用于参考 Runtime、Session 和 Conversation ownership。
- [Google Gemini Enterprise Agent Platform](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale) 与 [Deploy agents](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/deploy-an-agent)：用于参考多交付形态、统一托管和平台 Session/Memory 服务。
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) 与 [Codex as MCP server](https://learn.chatgpt.com/docs/mcp-server)：用于确认 Codex thread 和外层编排边界。

这些参考最终收敛成 KsADK 自己的四个核心抽象：

```text
CompiledPrompt   解决稳定指令的确定性和版本问题
ContextPlan      解决一次模型调用的预算、裁剪和可解释问题
ContextCheckpoint 解决长 Session 的连续性和 append-only 审计问题
MemoryProvider   解决本地/云端长期记忆后端的一致合同问题
```

`WorkingState` 不新增第五套存储，它是 `ContextCheckpoint` 的结构化字段；`ContextContributor` 也不是新的事实源，它只是把 Git、规则文件、Memory Recall、Skill manifest、附件等现有来源安全地转换成 `ContextItem` 的扩展合同。

### 2.3 外部材料使用原则

外部项目只用于验证架构取舍，实施时遵循以下证据顺序：

1. KsADK 当前代码、测试和真实协议。
2. 目标 Runtime 的官方 SDK/文档和实际 capability report。
3. 可运行的开源实现及其测试。
4. 第三方源码分析、文章和架构图。

`claude-code-analysis` 涉及疑似 Source Map 还原内容，团队只借鉴通用工程模式，不复制实现代码、内部 Prompt 文本、Feature Flag、私有接口或未公开产品细节。任何需要对外公开的 KsADK 文档只能表述为“参考通用模式”，不能宣称与 Claude Code 内部实现等价。

## 3. 当前代码基线

当前仓库已有较完整基础，本次不应推倒重写。

### 3.0 已有 Studio 与 Runtime 基础

- Studio 已通过 `AgentSpec`、`RuntimeRef` 和 `ContextSpec` 表达 Agent 的运行时选择及部分上下文策略；创建 Agent 时可选择 Codex、ADK 或 LangGraph，普通编辑不会静默改变 Runtime。
- `ksadk/runtime/adapter.py`、`registry.py`、`factory.py`、`executor.py` 已形成 RuntimeAdapter-first 执行骨架；HTTP 路由和 Studio 执行入口最终汇聚到该运行时层。
- `ksadk/runtime/conversation_execution.py` 是 Session/Transcript 与 Runtime 执行之间的 canonical 单 Turn 协调入口。
- `ksadk/runtime/preprocessing.py` 支持复用已准备的 Turn，已经具备避免重复预处理的基础。
- Codex 由 `CodexRuntimeAdapter` 直接接入；ADK、LangGraph 等通过 `RunnerRuntimeAdapter` 兼容现有 Runner。
- `RuntimeEvent` 已包含 context compaction start/completed 等统一事件，`RuntimeLaunchContext` 已承载运行时启动所需的结构化环境。

因此，本方案不再把“统一 Context 层”接到每个 Runner 或旧 conversation invocation 文件中，而是在 canonical conversation execution 处生成一次语义计划，再由 RuntimeExecutor 选择 RuntimeAdapter 完成投影和执行。

当前 Runtime/Studio 层仍有以下缺口：

- `ContextSpec` 尚未覆盖 Prompt 编译版本、分区预算、Memory policy、投影版本和 ownership capability hash。
- RuntimeAdapter 尚无统一的 Prompt/History/Compaction/Memory/Skill ownership 合同。
- Prepared Turn 的复用避免了部分重复工作，但尚未建立“CompiledPrompt/ContextPlan 每 Turn 只生成一次”的强制 invariant。
- `runtime_input.py` 仍存在依据 Runner 类型或类名推断 ambient context 行为的逻辑。
- Studio Build/Agent Revision 尚未锁定 prompt hash、policy hash、compiler/projection version，回放时难以解释语义变化。

### 3.1 已有 Prompt 基础

- `PreparedConversationTurn.instructions` 已能携带调用级 instructions。
- `ksadk/conversations/compaction_prompt.py` 已将压缩 Prompt 独立成固定 builder。
- Skill manifest 已支持只向主 Agent 暴露名称、描述和版本，完整 Skill 按需执行。
- Studio Agent instructions、Codex `base_instructions`、ADK instruction 与 LangGraph state/input 已存在各自投影入口。

主要缺口：

- 缺少统一 Prompt 分区模型和编译结果。
- Agent 指令、平台安全规则、资源说明和动态上下文仍可能在不同 Runner 中重复拼接。
- 缺少 Prompt 哈希、分区 token、来源和覆盖规则。
- 缺少稳定前缀与动态后缀的显式边界，不利于模型 Context Cache。
- 缺少 expected/unexpected cache break 诊断，无法区分正常动态变化和 Prompt 顺序漂移。
- 仓库级/目录级指令文件缺少统一发现顺序、去重、单文件预算和总预算合同。

### 3.2 已有 Context 基础

- Session Transcript 为 append-only 事件流。
- 已按 API round 分组，而不是逐条粗暴删除消息。
- 支持保护未完成 approval、tool call、附件引用和当前用户目标。
- 已有自动压缩阈值、Prompt-too-long 强制压缩和重试。
- 已有 L2 Snip、L3 Microcompact、L4 semantic summary、L5 working-set metadata。
- Checkpoint 保存摘要策略、模型、usage、pipeline 统计和覆盖 seq 范围。

主要缺口：

- 当前 token 统计仍是启发式估算。
- 压缩触发主要看 Transcript，没有统一计算 Prompt、Memory、Skill、Tool schema 和输出保留预算。
- 缺少统一 `ContextPlan`，无法解释每部分为何保留、裁剪或丢弃。
- Memory/Knowledge 以预格式化文本注入，缺少条目级预算和溯源。
- `runtime_input.py` 仍按 Runner 类型/类名判断是否注入平台 ambient context，缺少显式 capability/ownership 合同。
- 缺少单个 Tool Result 的硬预算、替换记录和 artifact reference；整体未超限前也可能被单个结果挤爆。
- checkpoint 摘要和 working-set metadata 尚未形成强类型 `WorkingState`，压缩后“当前阶段/下一步/错误修正”可能漂移。
- Git、规则文件、Hook、附件等动态来源尚未通过统一 Contributor 合同进入 Context。
- 尚未实现压缩前 Memory Flush。

### 3.3 已有 Memory 基础

- `LongTermMemoryService` 支持 local/http/sdk backend。
- 已有 `search_entries`、`save_text`、`build_context`。
- ADK 路径已有短期和长期 Memory 适配。
- LangGraph 路径可将平台 Memory 格式化后投影到 graph 输入；Codex 原生适配器保留原生 Session/Context ownership。
- runtime-common 已存在 Memory Provider manifest/registry 基础。

主要缺口：

- 当前保存粒度接近原始消息，缺少候选提取、分类、去重、冲突和敏感信息策略。
- 缺少统一 scope：`user/agent/workspace/org`。
- 缺少 stable ID、来源事件、版本、有效期、置信度和状态。
- `search_text()` 在异常时把错误字符串混入模型上下文，容易污染回答。
- 当前自动保存粒度接近整轮 user/assistant 文本，Session Working State 与跨 Session Memory 的边界仍需落实到代码。
- 本地 KV `MemoryManager`、ADK LTM 和平台 `LongTermMemoryService` 概念重叠。

## 4. 范围与非目标

### 4.1 本期范围

- Prompt 分区、编译和可观测元数据。
- Prompt stable/dynamic boundary、section hash 与 cache-break 诊断。
- ContextItem、ContextPlan、预算和组装顺序。
- RuntimeAdapter ContextCapabilities 与 Conformance Test。
- 受控 ContextContributor 合同及首批内置 Contributor。
- token counter 可插拔与估算降级。
- Tool Result 单项预算、内容替换记录与 artifact reference。
- 在 KsADK 持有 compaction ownership 的路径上，基于现有 append-only checkpoint 实现双阈值压缩。
- ContextCheckpoint 内的结构化 WorkingState 与压缩后状态重注入。
- 压缩前 Memory Flush。
- 长期记忆统一数据模型、检索接口和写入候选管线。
- 本地 Provider 与云端 Provider 一致的合同。
- Trace/Checkpoint 中的 Context 审计字段。
- 相关单测、集成测试、回归测试和特性开关。

### 4.2 非目标

- 不建设 Memory Registry/控制台 CRUD。
- 不在 SDK 内实现多租户权限事实源。
- 不实现知识图谱和自动实体关系抽取。
- 不将 Knowledge Base 合并进 Memory。
- 不允许 Agent 自动修改正式 Prompt 或正式 Skill。
- 不复制或发布第三方还原的 Claude Code Prompt、源码、私有 Flag 和未公开接口。
- 不实现通用 Completion Cache；本期只观测 Provider Prompt Cache 的命中与失效。
- 不让任意 Hook 直接覆盖 `platform_safety`、Agent 身份或其他高信任 PromptSection。
- 不建设 Skill 必要性评测、漂移观测和长上下文 Skill Benchmark；仅保留未来接入 Context Trace 和 Evaluation 的数据边界。
- 不改变不同框架的 checkpoint/resume 真实能力。
- 不在本期删除已有 Memory API；先通过兼容层迁移。

## 5. 术语与边界

| 名称 | 定义 | 生命周期 | 事实源 |
|---|---|---|---|
| Prompt Source | 用户或平台维护的指令源文件/字符串 | Agent 版本级 | Agent 工程/控制面 |
| CompiledPrompt | 按确定规则编译后的稳定 Prompt | Agent 版本级 | Runtime 输入 |
| ContextItem | 可进入一次模型调用的最小上下文单元 | Run/Turn 级 | Context Engine |
| ContextPlan | 本次调用所有候选项、预算和决策 | Turn 级 | Runtime |
| Session Transcript | 用户、模型、工具、审批等完整事件流 | Session 级 | Session Store |
| Context Checkpoint | 对旧 Transcript 前缀的摘要投影 | Session 级 | Session Store |
| Working State | 当前目标、阶段、进展、下一步、活跃资源和未完成状态的结构化快照，是 Checkpoint 的一部分 | Session/Checkpoint 级 | Session Store |
| Core Memory | 少量、重要、经策略允许的稳定事实 | 跨 Session | Memory Provider |
| Recall Memory | 按查询召回的长期事实/经验 | 跨 Session | Memory Provider |
| Memory Candidate | 尚未提交的记忆写入候选 | Turn/Run 级 | Runtime/Memory Service |
| Skill | 可执行或可加载的程序性知识 | 版本级 | 本地包/Skill Service |
| Context Contributor | 把既有外部来源转换成带预算、信任和溯源的 ContextItem 的扩展点 | Turn 级 | Runtime |

必须保持以下边界：

```text
Prompt != Context
Context != Transcript
Transcript != Memory
Working State != Long-term Memory
Memory != Knowledge Base
Memory != Skill
```

## 6. 总体模块设计

建议新增目录：

```text
ksadk/
├── prompts/
│   ├── __init__.py
│   ├── models.py          # PromptSection/CompiledPrompt
│   ├── compiler.py        # 确定性编译、覆盖、hash
│   └── sources.py         # 文件/字符串/控制面投影
├── context_engine/
│   ├── __init__.py
│   ├── models.py          # ContextItem/ContextBudget/ContextPlan
│   ├── tokenizer.py       # 精确 tokenizer + heuristic fallback
│   ├── planner.py         # 预算与保留/裁剪决策
│   ├── assembler.py       # 投影为 messages/responses items
│   ├── capabilities.py    # RuntimeAdapter ownership/接入模式/观测精度
│   ├── projection.py      # semantic envelope 与 ProjectionResult
│   ├── contributors.py    # 受控动态 Context 扩展点
│   ├── policies.py        # 默认策略与解析
│   ├── cache_observability.py # cache usage 与失效诊断
│   └── tracing.py         # context.plan/context.compaction 观测
└── memory/
    ├── models.py          # MemoryRecord/Candidate/SearchRequest
    ├── provider.py        # Provider Protocol
    ├── policy.py          # 写入、敏感信息、冲突策略
    ├── extraction.py      # 候选抽取接口
    ├── coordinator.py     # recall/flush/commit 编排
    └── providers/
        ├── local_sqlite.py
        ├── http.py
        └── sdk.py
```

模块职责按当前代码边界划分：

- `ksadk/conversations` 继续负责 Session/Transcript、Checkpoint、API round、tool/approval 配对和 compaction 算法，不整体搬迁。
- `ksadk/prompts` 与 `ksadk/context_engine` 是纯语义编译/规划模块，不直接选择 Runner，也不直接维护 Session。
- `ksadk/runtime/conversation_execution.py` 是每个 Turn 的 canonical coordinator：解析 Agent/策略、调用 Prompt Compiler 与 Context Engine 各一次，并把结果放入准备好的运行时请求。
- `ksadk/runtime/executor.py` 通过 Registry 选择 RuntimeAdapter；`ksadk/runtime/preprocessing.py` 复用 prepared turn，不允许再次编译 Prompt 或重建 ContextPlan。
- RuntimeAdapter 只依据 capability 将 semantic envelope 投影到 Codex、ADK、LangGraph 或自定义 Runtime 的合法输入，并返回 `ProjectionResult` 和可观测证据。

旧 `ksadk/conversations/runtime_preparation.py`、`runtime_invocation.py` 等模块在迁移期只保留兼容职责；新的行为不能同时写入新旧两条调用链。

### 6.1 RuntimeAdapter Context Ownership

KsADK 不能假设所有 Runtime 都允许平台直接组装最终 messages。能力声明的归属点是 `RuntimeAdapter`；仍通过 `RunnerRuntimeAdapter` 接入的框架，可由 `BaseRunner` 提供细化声明，但由 Adapter 汇总成统一合同。建议通过 `describe_context_capabilities()` 声明能力和所有权：

```python
from dataclasses import dataclass
from typing import Literal

DeploymentMode = Literal[
    "local",
    "ksadk_managed_cloud",
    "external_managed",
]
ContextIntegrationMode = Literal[
    "ksadk_hosted",
    "framework_assisted",
    "native_runtime",
]
ContextOwner = Literal["ksadk", "framework", "native"]
ContextAccuracy = Literal["exact", "runtime_reported", "estimated", "opaque"]

@dataclass(frozen=True)
class ContextCapabilities:
    integration_mode: ContextIntegrationMode
    prompt_owner: ContextOwner
    history_owner: ContextOwner
    compaction_owner: ContextOwner
    memory_owner: ContextOwner
    skill_owner: ContextOwner
    prompt_projection: frozenset[str]
    memory_read: bool
    memory_write: bool
    core_memory: bool
    native_skills: bool
    token_accounting: ContextAccuracy
    supports_context_snapshot: bool
```

`DeploymentMode` 不放入 `ContextCapabilities`：它属于 `RuntimeRef/RuntimeLaunchContext/Deployment`，描述实例在哪里运行以及谁负责构建、扩缩容和运维；`ContextIntegrationMode` 属于 Adapter，描述 Prompt、History、Memory、Compaction 和最终输入的 ownership。`ContextPlan`、Run 和 Trace 同时记录二者，才能区分下列场景：

| 场景 | DeploymentMode | ContextIntegrationMode | 产品称谓 |
|---|---|---|---|
| KsADK 自有 Agent Loop | `ksadk_managed_cloud` | `ksadk_hosted` | KsADK Harness |
| KsADK 云端运行 Codex SDK/CLI | `ksadk_managed_cloud` | `native_runtime` | Managed Codex Runtime |
| 本地 ADK/LangGraph 协作接入 | `local` | `framework_assisted` | Local BYO Runner |
| 外部 Claude/OpenClaw/Hermes | `external_managed` | `native_runtime` | External Managed Runtime |

当前枚举名为了兼容已经落地的 shadow 模型和测试暂不重命名；其中 `ksadk_hosted` 的语义是 `ksadk_owned`，`native_runtime` 的语义是 `native_owned`。不得依据字符串中的 `hosted` 判断部署位置。

能力声明必须由 RuntimeAdapter 实现，或由 `RunnerRuntimeAdapter` 汇总已知 Runner 的显式声明，不能仅通过 `hasattr`、类名或框架名称猜测。未知自定义 Runtime/Runner 默认采用最保守的 `framework_assisted + opaque`，在未完成适配前不得启用 KsADK assembler 改写真实输入。

Capability 是可执行合同，不是展示标签。Runtime 必须按它决定是否编译/投影 Prompt、是否注入 History/Memory、是否执行 compaction。若实际 usage 或事件证明声明不一致，应记录 `context.capability_mismatch` 并停止对该 Runner 启用行为型 Context Engine，而不是静默重复注入。

### 6.2 三种接入模式

| 模式 | KsADK 负责 | Runtime/框架负责 | 当前/典型路径 |
|---|---|---|---|
| `ksadk_hosted` | Prompt 编译、候选选择、预算、compaction、最终输入组装 | 执行模型/图并返回事件 | 明确把 messages/state 交由 KsADK 管理的 Runtime |
| `framework_assisted` | 统一 CompiledPrompt、ContextPolicy、Memory Provider 和观测；提供 semantic envelope | Adapter 将内容投影到原生 Session、State、Store、Instruction | 当前 ADK、LangGraph 的 `RunnerRuntimeAdapter` 路径 |
| `native_runtime` | 传递版本化 instructions、平台边界和外部 Memory hook；统一事件、治理和 Trace | Agent loop、thread/history、compaction、原生 Skill 和最终模型输入 | 当前 `CodexRuntimeAdapter`；未来 Claude Agent SDK、OpenClaw/Hermes |

首批 ownership 建议如下，最终以能力探测和契约测试为准：

| Runtime/Adapter | 模式 | History owner | Compaction owner | 长期 Memory 接入 |
|---|---|---|---|---|
| LangGraph / RunnerRuntimeAdapter | assisted；显式托管时可 hosted | framework 或 ksadk | framework 或 ksadk | Provider/Store adapter |
| Google ADK / RunnerRuntimeAdapter | assisted | framework | framework/ksadk | ADK MemoryService adapter |
| LangChain、DeepAgents / RunnerRuntimeAdapter | assisted | framework | framework/native | Provider + 原生能力 |
| CodexRuntimeAdapter | native | native | native | 外部 Memory hook，不重复注入完整历史 |
| Claude Agent SDK（未来） | native | native | native | 外部 Memory hook，不重复实现 SDK 会话 |
| OpenClaw/Hermes | native | native | native | 文件/Provider/Gateway 投影 |

同一个框架可以因 Adapter 接入方式不同声明不同模式。例如用户显式把 LangGraph state/messages 交给 KsADK 时可使用 `ksadk_hosted`；已经编译且自行维护 checkpointer/store 的 graph 应使用 `framework_assisted`，不得魔改用户 graph。模式是 Adapter 实例/版本的运行合同，不是框架名称的永久标签。

Codex 还有一种嵌套形态：KsADK Harness 可通过 MCP 调用 Codex 子 Agent。此时外层 Turn 是 `ksadk_hosted`，KsADK 对外层 Prompt/Context 提供 `exact` 证据；内层 Codex thread 是 `native_runtime`，只接受经批准的任务包和资源引用，并使用 `runtime_reported/opaque` 证据。两层必须使用不同 `run_id/thread_id` 和 ownership span，不能把外层 Transcript 整体复制进 Codex，也不能把 Codex 内部 history 伪装成平台 ContextItem。

### 6.3 Context 观测精度

统一观测不等于假定平台掌握底层全部输入。每个 Plan/Trace 必须声明：

| 精度 | 含义 |
|---|---|
| `exact` | KsADK 生成了最终模型输入，并使用匹配 tokenizer 计算 |
| `runtime_reported` | 原生 Runtime/模型返回了实际 usage 或 context 统计 |
| `estimated` | KsADK 只能对提交给 Runner 的内容进行估算 |
| `opaque` | Runner 不暴露最终输入或可靠 usage，只记录来源、hash 和能力缺口 |

Trace 中应同时区分 `planned_*`、`projected_*` 和 `actual_*`。对于 native Runtime，`ContextPlan.selected` 表示“KsADK 请求投影的内容”，只有收到运行时确认后才能标记为模型实际使用。控制台不能用 estimated 数据展示为精确 token，也不能据此承诺平台 compaction 已生效。

### 6.4 RuntimeAdapter Context Conformance

每个内置 RuntimeAdapter 共用一套合同测试；`RunnerRuntimeAdapter` 再对其承载的 Runner 做参数化验证。至少回答：

| 合同项 | 验证内容 |
|---|---|
| Prompt projection | section 是否按声明进入目标 SDK，安全优先级是否保持 |
| History ownership | 是否发生重复历史注入，恢复后是否仍由同一 owner 管理 |
| Compaction ownership | PTL 时只运行一套压缩链路，native/assisted 路径不被平台越权压缩 |
| Memory projection | Recall 是否条目化、可追溯，native memory 与 platform memory 是否重复 |
| Skill projection | 只暴露 manifest 还是加载全文，与 capability 声明是否一致 |
| Token accounting | exact/runtime_reported/estimated/opaque 是否有真实证据 |
| Resume invariant | tool pair、approval、receipt、framework_ref 是否完整 |

Conformance 结果应包含 `runtime_type`、Adapter/Runner/SDK 版本、capability hash、projection version 和证据等级。版本升级后必须重新执行，不能沿用旧版本结论。

## 7. Prompt 技术设计

### 7.1 Prompt 分区

Prompt 使用统一语义分区和信任优先级。`PromptCompiler` 产生 canonical 顺序，Runner 不得改变安全优先级、信任边界和覆盖规则；最终物理格式、消息 role 和顺序由 Runner Projection 按目标 SDK 的合法输入方式映射，并输出可审计的 `PromptProjectionResult`。

| 顺序 | 区段 | 内容 | 稳定性 | 默认可覆盖 |
|---:|---|---|---|---|
| 10 | `platform_safety` | 平台安全和不可绕过规则 | 稳定 | 否 |
| 20 | `agent_identity` | Agent 身份、职责、目标 | 稳定 | 是 |
| 30 | `agent_policy` | 行为、输出和工具使用策略 | 稳定 | 是 |
| 40 | `runtime_capabilities` | Runner、Sandbox、恢复能力声明 | 部署级 | 否 |
| 50 | `resource_manifest` | Skill/Tool/Memory/Knowledge 索引 | 部署级 | 否 |
| 60 | `request_instructions` | API 本次请求的 instructions | Turn 级 | 是 |

动态历史、记忆和工具结果不属于 PromptSection，它们是 ContextItem。

各类内容的最低信任边界如下：

| 内容 | 信任级别 | 能否覆盖平台规则 |
|---|---|---|
| `platform_safety` | platform | 否 |
| Deployment/Environment policy | platform | 否 |
| Agent identity/policy | developer | 否，只能在平台允许范围内配置 |
| Skill/resource manifest | resource | 否 |
| Memory Recall | untrusted | 否 |
| Tool/Knowledge 输出 | untrusted | 否 |
| 用户输入 | user | 否 |

典型投影方式：Codex 使用 `base_instructions` 和 thread input；ADK 使用 Agent instruction、Session/MemoryService；LangGraph 使用 system message、state/store。Projection 可以改变物理承载形式，但必须保留 section source、hash、信任级别和覆盖决策。

#### 7.1.1 `platform_safety` 的真实来源与首批接管边界

当前 Runtime 没有统一的 agent-level system prompt 来源，主链路只有 request-level `instructions`；当前 shadow 实现只编译真实存在的 `request_instructions`，`stable_prefix_hash` 为空是符合事实的结果。`platform_safety` 目前首先是 schema 预留槽位，不能在首个行为 PR 中凭空硬编码一段“平台安全 Prompt”，也不能把 request instructions 改名后冒充平台规则。

正式启用时按以下来源优先级解析：

| 来源 | 用途 | 发布语义 |
|---|---|---|
| 控制面版本化 Platform Policy Bundle | 平台级不可绕过规则及租户允许范围 | 与 Deployment 绑定，包含 version/hash，可独立回滚；本仓只消费投影合同 |
| AgentVersion 的 identity/policy | Agent 角色、任务边界和开发者配置 | 随 Agent Build 锁定、发布和回滚 |
| SDK 内最小安全不变量 | approval、sandbox、secret redaction 等无法只依赖 Prompt 的强制检查 | 通过代码和 Policy Enforcement 执行，不把全部安全能力寄托于一段文字 |
| 本地显式配置文件 | 离线开发时的 Platform Policy Bundle 替代来源 | 必须有来源/version/hash；不得自动读取不透明环境字符串 |

首个接管 PR 如果控制面 Bundle 尚未就绪，应继续保持 `platform_safety` 缺席并诚实观测，只接管已有 `request_instructions` 的编译清单；或者使用仓库内公开、版本化、经过评审的最小默认 Bundle。生产环境变量只允许指向版本化文件/资源 ID，不建议直接承载整段安全正文。任何外部 Memory、Tool、MCP、附件和用户内容都不能产生 platform-trust Section。

### 7.2 数据模型

```python
from dataclasses import dataclass, field
from typing import Literal

PromptSectionKind = Literal[
    "platform_safety",
    "agent_identity",
    "agent_policy",
    "runtime_capabilities",
    "resource_manifest",
    "request_instructions",
]

@dataclass(frozen=True)
class PromptSection:
    section_id: str
    kind: PromptSectionKind
    content: str
    source: str
    priority: int
    trust_level: Literal["platform", "developer", "resource", "untrusted", "user"]
    stability: Literal["stable", "deployment", "volatile"] = "stable"
    merge_policy: Literal["replace", "append", "merge_unique", "protected"] = "append"
    overridable: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

@dataclass(frozen=True)
class CompiledPrompt:
    sections: tuple[PromptSection, ...]
    content: str  # canonical content，不保证等于 Runner 最终物理输入
    content_hash: str
    estimated_tokens: int
    stable_prefix_hash: str
    section_hashes: dict[str, str]
    tokens_by_section: dict[str, int]
    compiler_version: str

@dataclass(frozen=True)
class PromptProjectionResult:
    projection_id: str
    runner_type: str
    integration_mode: ContextIntegrationMode
    projection_version: str
    section_hashes: tuple[str, ...]
    projected_roles: tuple[str, ...]
    accounting_accuracy: ContextAccuracy
    estimated_tokens: int | None
    warnings: tuple[str, ...] = ()
```

### 7.3 编译规则

`PromptCompiler.compile()` 必须是确定性的：

1. 对 section 按固定 priority 和 kind 排序。
2. 标准化换行和尾部空白，但不改变正文语义。
3. 同 kind 多来源时执行显式 merge policy，禁止“最后写入悄悄覆盖”。
4. `platform_safety` 不允许请求级 instructions 覆盖。
5. 空 section 不输出占位文本。
6. 对最终 UTF-8 字节计算 SHA-256。
7. 输出 section 级 token 统计，供 Context Planner 使用。
8. 同一个 `section_id` 的来源、稳定性和 merge policy 必须固定，不能因 Runner 遍历顺序变化。
9. `protected` section 发生覆盖尝试时编译失败并产生审计事件，不能静默忽略。

推荐格式：

```text
<platform_safety>
...
</platform_safety>

<agent_identity>
...
</agent_identity>
```

标签只用于消除区段歧义，不应把所有动态上下文拼成 XML。

### 7.4 Stable/Volatile 边界

为提高模型缓存命中率，canonical Prompt 以及 `ksadk_hosted` 的默认发送顺序如下；assisted/native Runner 可改变物理承载形式，但不得改变信任优先级和覆盖规则：

```text
[稳定前缀]
platform_safety
agent_identity
agent_policy

[部署级低频变化]
runtime_capabilities
resource_manifest

[动态后缀]
request_instructions
checkpoint summary
memory recall
recent transcript
current input
```

禁止把时间戳、session_id、run_id、随机 nonce 等字段写入稳定 Prompt 正文；这些信息进入 request metadata 或 Context Trace。

### 7.5 Prompt Cache 失效诊断

KsADK 不实现通用 Completion Cache，但应利用 Provider/Runtime 返回的 prompt cache usage 诊断稳定前缀是否意外失效：

```text
prompt.content_hash
prompt.stable_prefix_hash
prompt.section_hashes
prompt.cache.read_input_tokens
prompt.cache.creation_input_tokens
prompt.cache.expected_invalidation
prompt.cache.unexpected_break
prompt.cache.break_reason
```

判定原则：

- Agent Revision/Build、模型、稳定 section 或 projection version 变化属于 expected invalidation。
- 只有 current input、session metadata、Memory Recall 等动态后缀变化时，稳定前缀 cache read 大幅下降属于疑似 unexpected break。
- assisted/native Runner 只有在 Runtime 返回 usage 时才能做 runtime-reported 诊断；否则标记 opaque，不推断命中率。
- cache-break 只用于发现 Prompt 顺序漂移、动态字段误入稳定前缀和 Adapter projection 变化，不能作为回答质量的唯一指标。

### 7.6 指令文件发现与预算

如果 Agent 工程允许从 `AGENTS.md`、`CLAUDE.md` 或规则目录加载指令，必须统一：

1. 以 workspace/repository boundary 为上限，从父目录到当前目录确定性发现。
2. 使用真实路径去重，记录 `source/path/content_hash`。
3. 父级通用规则先进入，子级规则只按显式 merge policy 收窄或追加。
4. 设置单文件和总 token 预算；超限返回明确 warning/config error，不静默截断平台安全规则。
5. 本地路径只进入 Trace 的脱敏 source id，不进入公开日志。

首期只把用户显式配置的 Prompt Source 纳入编译；自动目录发现应使用独立 feature flag，避免改变现有 Agent 行为。

### 7.7 Skill 在 Prompt 中的处理

延续当前 manifest + on-demand 设计：

```text
- skill name
- description
- version/content hash
- activation hint
```

完整 `SKILL.md` 不默认进入 Prompt。只有以下情况可进入 Context：

- Runner 明确调用加载工具。
- 确定性 trigger 命中且策略允许自动注入。
- Skill 被标记为极少数 `always` 基础能力。

本期不实现 Skill 自进化，只在 ContextItem 中预留 `skill_content` 类型。

## 8. Context Engine 技术设计

### 8.1 ContextItem

所有可能进入模型的内容统一表示为 ContextItem：

```python
ContextKind = Literal[
    "compiled_prompt",
    "core_memory",
    "resource_manifest",
    "checkpoint_summary",
    "working_state",
    "recalled_memory",
    "history_round",
    "skill_content",
    "tool_result",
    "attachment_context",
    "current_input",
]

@dataclass
class ContextItem:
    item_id: str
    kind: ContextKind
    content: object
    source: str
    trust_level: Literal["platform", "developer", "resource", "user", "untrusted"]
    priority: int
    estimated_tokens: int
    required: bool = False
    droppable: bool = True
    truncatable: bool = False
    stable: bool = False
    group_id: str | None = None
    seq_start: int | None = None
    seq_end: int | None = None
    score: float | None = None
    content_hash: str | None = None
    provenance: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
```

`group_id` 用于确保以下内容原子保留或原子丢弃：

- 一轮 user/assistant 对话。
- tool call 与对应 tool result。
- approval request 与 approval response。
- Responses API function call/output item。

`provenance` 至少能表示 provider/contributor、source record/event、版本和生成时间，但不得默认包含完整敏感正文。所有 Memory、Knowledge、Tool、Hook 和外部文件内容即使来自受信基础设施，也按 `untrusted` 内容处理，不能覆盖 PromptSection。

### 8.2 ContextBudget

```python
@dataclass(frozen=True)
class ContextBudget:
    context_window_tokens: int
    reserved_output_tokens: int
    reserved_reasoning_tokens: int
    safety_buffer_tokens: int
    max_input_tokens: int
    soft_limit_tokens: int
    hard_limit_tokens: int
    section_limits: dict[str, int]
```

计算规则：

```text
max_input = min(
    model.max_input_tokens,
    context_window
      - reserved_output
      - reserved_reasoning
      - safety_buffer
)

soft_limit = floor(max_input * 0.50)   # 主动整理
hard_limit = floor(max_input * 0.85)   # 强制压缩/重试
```

50% 和 85% 是初始默认值，应可由模型 metadata 或 Deployment policy 覆盖。不要继续只使用“接近窗口末端才压缩”的单阈值策略。

### 8.3 默认分区预算

分区预算以 `max_input_tokens` 的比例计算，并设置绝对上限：

| 分区 | 默认比例 | 默认绝对上限 | 策略 |
|---|---:|---:|---|
| Prompt | 15% | 24K | 超限视为配置错误，不静默裁剪安全规则 |
| Resource manifest | 5% | 8K | 先限制条数和描述长度 |
| Core memory | 5% | 8K | 按 block priority 保留 |
| Recalled memory | 10% | 16K | 按相关度、时效和多样性裁剪 |
| Checkpoint summary | 10% | 16K | 必须保留，必要时再次摘要 |
| Working state | 5% | 8K | 必须保留核心字段，列表按优先级裁剪 |
| Recent history | 35% | 64K | 按完整 round 保留 |
| Tool/attachment | 10% | 16K | 原始大结果转 artifact/摘要 |
| Safety buffer | 5% | 8K | 不分配给内容 |

比例不是配额预占。某分区未使用的预算可回流，但 required item 的预算必须先锁定。

### 8.4 Planner 决策顺序

`ContextPlanner.plan()` 建议按以下算法实现：

```python
def plan(candidates, budget):
    required = select_required(candidates)
    assert fits(required, budget.hard_limit_tokens)

    selected = list(required)
    selected += choose_prompt_and_core_memory(candidates, budget)
    selected += choose_checkpoint(candidates, budget)
    selected += choose_recent_complete_rounds(candidates, budget)
    selected += choose_recalled_memory(candidates, budget)
    selected += choose_optional_resources(candidates, budget)

    if tokens(selected) > budget.soft_limit_tokens:
        selected = deterministic_reduce(selected)

    if tokens(selected) > budget.hard_limit_tokens:
        selected = emergency_reduce(selected)

    return ContextPlan(...)
```

强制优先级：

1. `platform_safety`。
2. 当前用户输入。
3. 未完成 approval/tool 状态。
4. 当前 Run 恢复所需 receipt/framework reference。
5. Agent 核心身份与策略。
6. 最新 checkpoint summary。
7. Session Working State。
8. 最近完整轮次。
9. Core Memory。
10. Recall Memory。
11. 可选 Skill、旧工具输出和附件摘要。

### 8.5 确定性缩减策略

在调用摘要模型前先执行零 LLM 成本处理：

1. 删除重复 resource manifest 项。
2. 将已持久化的大型 tool result 替换成摘要 + artifact reference。
3. 移除被后续同参数调用覆盖的旧 tool call/result 对。
4. 去除二进制、base64、重复日志和不可见 reasoning。
5. 对旧冷轮次执行现有 Microcompact。
6. 降低 Recall Memory top_k。
7. 最后才触发 semantic compaction。

任何缩减只能作用于 Context 投影或 summarizer candidate，不能删除 Session Event。

### 8.6 Tool Result 单项预算与内容替换

整体 Context 尚未达到 soft limit 时，单个 Tool Result 也可能直接造成 PTL。每个 Tool 应允许声明：

```python
ToolContextPolicy(
    max_result_tokens=8000,
    replacement_strategy="artifact_summary",
    preserve_error_tail=True,
    never_truncate=False,
)
```

超限时按以下顺序处理：

1. 去除二进制/base64 和重复日志。
2. 保留结构、错误码、首尾关键片段和统计摘要。
3. 将完整结果持久化到具备权限和生命周期的 Artifact Store。
4. 在 Context 中放入摘要、artifact reference、content hash 和截断原因。
5. 将 replacement record 追加到 Session/Checkpoint metadata，保证 resume/replay 能解释模型看到了哪个投影。

Tool call/result 的协议配对必须保持完整；如果 Tool 声明 `never_truncate`，Planner 应将超预算作为结构化失败或触发上层降载，不能私自裁剪语义载荷。

### 8.7 ContextContributor

动态上下文不能由 Runner、Hook 或业务代码直接拼接到 Prompt。统一扩展协议：

```python
@dataclass(frozen=True)
class ContributorCapabilities:
    contributor_id: str
    trust_level: Literal["platform", "developer", "resource", "user", "untrusted"]
    max_tokens: int
    timeout_ms: int
    cacheability: Literal["stable", "turn", "none"]
    failure_mode: Literal["skip", "warn", "fail"]

class ContextContributor(Protocol):
    def capabilities(self) -> ContributorCapabilities: ...
    async def contribute(self, request: ContextContributionRequest) -> list[ContextItem]: ...
```

首批内置 Contributor：

- `WorkspaceRulesContributor`
- `GitContextContributor`
- `MemoryRecallContributor`
- `SkillManifestContributor`
- `AttachmentContributor`
- `ControlPlanePolicyContributor`

Contributor 必须并发执行但分别受超时和预算约束；结果先进入候选集，再由 Planner 决策。外部 Hook/MCP 的返回一律为 `untrusted`，不能生成 `platform_safety`、不能改变 ownership，也不能绕过 approval。P0 只建立接口和 shadow trace，P2 再逐个接管现有来源。

### 8.8 ContextPlan 与审计

```python
@dataclass
class ContextDecision:
    item_id: str
    action: Literal["included", "summarized", "truncated", "dropped"]
    reason: str
    tokens_before: int
    tokens_after: int

@dataclass
class ContextPlan:
    plan_id: str
    policy_version: str
    tokenizer: str
    deployment_mode: DeploymentMode
    integration_mode: ContextIntegrationMode
    accounting_accuracy: ContextAccuracy
    budget: ContextBudget
    selected: list[ContextItem]
    decisions: list[ContextDecision]
    tokens_by_kind: dict[str, int]
    planned_input_tokens: int
    projected_input_tokens: int | None
    runtime_reported_input_tokens: int | None
    stable_prefix_hash: str
    projection_id: str | None = None
    contributor_status: dict[str, str] = field(default_factory=dict)
```

`ContextPlan` 是平台的选择和投影计划。`deployment_mode` 说明执行位置，`integration_mode` 说明 Context owner，两者不得相互推导。仅在 `accounting_accuracy=exact` 且 Projection 成功时，它才可以代表最终模型输入；native/assisted 模式下必须结合 Runner 回报生成实际使用记录。Trace/日志只记录统计、哈希和脱敏摘要，默认不记录完整 Prompt、Memory 和 Tool 内容。

建议新增事件或 Span 属性：

```text
context.plan_id
context.policy_version
context.tokenizer
context.deployment_mode
context.integration_mode
context.planned_input_tokens
context.projected_input_tokens
context.runtime_reported_input_tokens
context.integration_mode
context.history_owner
context.compaction_owner
context.accounting_accuracy
context.projection_id
context.tokens_by_kind
context.dropped_items
context.compaction_trigger
context.checkpoint_id
context.stable_prefix_hash
context.cache.unexpected_break
context.contributor_status
memory.recall_count
memory.flush_candidate_count
```

### 8.9 Token Counter

新增协议：

```python
class TokenCounter(Protocol):
    name: str

    def count_text(self, text: str, *, model: str | None = None) -> int: ...
    def count_messages(self, messages: Sequence[object], *, model: str | None = None) -> int: ...
```

实现顺序：

1. 模型 Provider 官方 tokenizer。
2. 已知兼容 tokenizer（例如 tiktoken-compatible）。
3. 当前 CJK + ASCII heuristic。

必须记录所用 tokenizer。只有 heuristic 可用时给预算增加 10% 安全系数。模型请求返回实际 usage 后，记录估算偏差：

```text
error_ratio = abs(actual_input - estimated_input) / max(actual_input, 1)
```

以模型为维度监控 P50/P95，偏差持续过大时调整 tokenizer mapping，而不是散落修改阈值。

## 9. Compaction 设计

Compaction 是有序降载的最后几级，不是单一摘要调用。KsADK-owned 路径固定执行：

```text
per-tool result budget
  → dedupe/snip
  → microcompact cold rounds
  → checkpoint/context collapse
  → semantic compact
  → PTL emergency compact/retry once
```

每一级都必须报告触发原因、输入/输出 token、覆盖范围、replacement record 和失败类别。低成本确定性步骤已经把请求降到 soft limit 以下时，不再调用 semantic summarizer。

### 9.1 双阈值

| 阶段 | 触发 | 行为 |
|---|---|---|
| Proactive | 达到 soft limit（默认 50%） | 后台/当前 turn 前整理旧历史，执行 Memory Flush，生成 checkpoint |
| Emergency | 达到 hard limit（默认 85%）或模型返回 PTL | 同步强制压缩，减少保留尾部轮次，最多重试一次 |

禁止无限 PTL 重试。一次请求最多：

```text
normal attempt → emergency compact → retry once
```

仍失败则返回结构化错误，带上 estimated tokens、模型窗口和 checkpoint 信息，不返回完整敏感上下文。

### 9.2 Memory Flush

在旧轮次被 checkpoint summary 替代前，应提取可能需要跨 Session 保存的 Memory Candidate。

流程：

```text
groups_to_compact
      │
      ▼
MemoryExtractor.propose()
      │ candidates
      ▼
MemoryPolicy.evaluate()
      ├── reject
      ├── pending
      └── commit
      │
      ▼
MemoryProvider.upsert()
      │
      ▼
semantic/extractive compaction
      │
      ▼
append context_checkpoint
```

失败语义：

- Memory Flush 失败不能阻止紧急 compaction，否则会导致请求永久 PTL。
- checkpoint metadata 必须记录 `memory_flush_status` 和失败类别。
- Proactive compaction 可根据策略重试 flush。
- 不得把异常文本注入给模型。

### 9.3 Session Working State

`WorkingState` 是压缩前后保持任务连续性的结构化工作面，随 `ContextCheckpoint` 持久化：

```python
@dataclass
class WorkingState:
    current_goal: str
    current_phase: str | None
    completed_steps: list[str]
    pending_steps: list[str]
    next_action: str | None
    active_files: list[dict[str, object]]
    decisions: list[dict[str, object]]
    errors_and_corrections: list[dict[str, object]]
    pending_tools: list[dict[str, object]]
    pending_approvals: list[dict[str, object]]
    artifact_refs: list[dict[str, object]]
    source_seq_range: tuple[int, int]
    schema_version: str = "v1"
```

生成原则：

- 优先从 SessionEvent、pinned state、receipt、workspace metadata 做确定性提取；只有难以结构化的目标/决策/错误修正才允许模型辅助。
- `pending_tools`、`pending_approvals` 和 receipt 必须来自事实事件，不能由摘要模型猜测。
- 使用 token growth + 自然 turn 边界触发更新；不按固定每 N 轮无条件调用模型。
- proactive 更新可以异步执行，但需使用 per-session lock、超时和 stale guard；emergency compact 只等待有限时间，超时后使用最近成功版本。
- 设置总预算和字段预算，优先保留 current goal、next action、error correction 和 pending state。
- Working State 不跨 Session 自动召回，也不直接写入 MemoryProvider。

压缩后默认重注入：summary + WorkingState + 未完成协议状态 + 必要 artifact/reference。近期文件只注入路径、范围、mtime/content hash 等元数据；除非 Planner 明确选择，不自动重读完整文件正文。

### 9.4 摘要结构

延续当前固定摘要结构，但升级为版本 `v2`：

```text
当前用户目标
关键约束与偏好
已完成进展
重要决策/代码上下文
未完成事项
下一步工作位置
未完成工具与审批
重要引用与 Artifact
```

规则：

- 明确区分“事实”“模型推断”“未确认”。
- 保留文件路径、符号名、错误码、版本和执行结果。
- 不复制 token、cookie、Secret 和临时签名 URL。
- 工具成功与失败状态不可只保留自然语言结论，应保留 receipt/reference。
- previous summary 与新事件发生冲突时，保留最新事实并标记来源范围。

### 9.5 Checkpoint 元数据

在现有字段基础上新增：

```json
{
  "context_policy_version": "v1",
  "prompt_hash": "sha256:...",
  "stable_prefix_hash": "sha256:...",
  "tokenizer": "heuristic-cjk-v1",
  "working_state": {
    "schema_version": "v1",
    "content_hash": "sha256:...",
    "source_seq_range": [1, 120],
    "status": "succeeded"
  },
  "memory_flush": {
    "status": "succeeded",
    "proposed": 4,
    "committed": 2,
    "rejected": 2
  },
  "tokens_by_kind_before": {},
  "tokens_by_kind_after": {},
  "retained_group_ids": [],
  "dropped_context_item_ids": []
}
```

### 9.6 Compaction 熔断与并发

- semantic compact 的熔断键至少包含 tenant/workspace、session、model 和 summarizer version，不能使用进程级全局失败计数影响无关会话。
- 同一 Session 同时只允许一个 checkpoint/WorkingState 更新提交；并发 Turn 使用乐观版本或 session lock，失败方重新读取最新 checkpoint 后规划。
- 摘要失败优先回退到现有 extractive summary；连续失败达到阈值后跳过 semantic 层，但 deterministic reduction 和 emergency 结构化错误仍可用。
- 每次 PTL 链路只有一个 emergency retry budget，Hook/Contributor/Runner 都不能自行再形成嵌套无限重试。

## 10. Memory 技术设计

### 10.1 记忆类型

本期支持三类长期记忆：

| 类型 | 内容 | 示例 | 是否默认始终注入 |
|---|---|---|---|
| `profile` | 稳定用户偏好与身份信息 | 偏好中文、默认 Python 3.12 | 少量 Core Block 可注入 |
| `fact` | 项目或业务事实 | 仓库主分支、接口约束 | 否，按查询召回 |
| `episode` | 可复用的任务经历与结果 | 某类部署失败的处理结论 | 否，按查询召回 |

Skill/流程规则不作为 Memory Record 保存；如果某一经验需要稳定成为操作方法，应进入 Skill 的受审版本流程。

当前目标、当前计划、近期文件、未完成工具、待审批操作和“下一步做什么”属于 `WorkingState`，即使看起来很重要也不直接写入长期记忆。只有其中经过验证、跨 Session 仍有效的事实或偏好，才能独立生成 MemoryCandidate。

必须区分两类实现：

- **Platform portable memory**：由 KsADK `MemoryProvider`/云端 Memory Service 管理，是跨 Runtime、跨 Agent Revision/Build 和跨部署的长期记忆事实源，必须支持 scope、权限、版本、TTL、删除和审计。
- **Runtime-native memory**：Codex、Claude、OpenClaw/Hermes 等原生 Runtime 自行维护的 thread/file/internal state，属于 Runtime 局部能力，可作为缓存、会话状态或 Provider 投影，但不能替代平台长期记忆事实源。

native Runner 可以使用自己的短期/工作记忆，同时通过外部 hook 检索和提交平台 MemoryCandidate。KsADK 不抓取整个原生 thread 写成长记忆，也不把平台完整 Transcript 再注入原生 Runtime。

### 10.2 MemoryRecord

```python
MemoryScope = Literal["user", "agent", "workspace", "org"]
MemoryType = Literal["profile", "fact", "episode"]
MemoryStatus = Literal["active", "superseded", "deleted", "expired"]

@dataclass
class MemoryRecord:
    memory_id: str
    tenant_id: str
    workspace_id: str
    scope: MemoryScope
    scope_id: str
    memory_type: MemoryType
    content: str
    summary: str
    status: MemoryStatus
    confidence: float
    importance: float
    valid_from: datetime
    valid_to: datetime | None
    expires_at: datetime | None
    source_session_id: str
    source_event_ids: list[str]
    source_seq_range: tuple[int, int] | None
    content_hash: str
    version: int
    metadata: dict[str, object]
    created_at: datetime
    updated_at: datetime
```

SDK 本地模式可将 tenant/workspace 设为明确的本地值，但不能省略隔离字段，以免迁移云端时改变合同。

### 10.3 MemoryCandidate

```python
@dataclass
class MemoryCandidate:
    candidate_id: str
    operation: Literal["add", "update", "delete", "ignore"]
    memory_type: MemoryType
    scope: MemoryScope
    scope_id: str
    content: str
    confidence: float
    importance: float
    source_event_ids: list[str]
    conflicts_with: list[str]
    sensitive_labels: list[str]
    reason: str
```

### 10.4 写入策略

默认策略：

| 场景 | 行为 |
|---|---|
| 用户明确说“记住……” | 同步 propose；安全检查通过后 commit |
| 用户明确说“忘掉……” | 解析目标并请求删除；歧义时不猜测 |
| 普通对话中的稳定偏好 | 后台 propose，达到阈值才 commit |
| 一次性当前任务状态 | 留在 Session/Checkpoint，不写长期记忆 |
| 模型自己的猜测 | 不写入 |
| 工具返回的可验证事实 | 可提议 fact，必须保留工具来源 |
| 密钥、cookie、token、AK/SK | 永久拒绝 |
| 完整附件或二进制内容 | 永久拒绝，只允许安全摘要/引用 |
| 与旧事实冲突 | update/supersede，不覆盖历史来源 |

初始阈值建议：

```text
explicit user request: confidence >= 0.60
verified tool fact:    confidence >= 0.80
implicit preference:   confidence >= 0.85 and observed >= 2
episode:               importance >= 0.70
```

阈值必须属于 `MemoryPolicy`，不能硬编码在 Runner。

### 10.5 Provider 合同

```python
class MemoryProvider(Protocol):
    async def capabilities(self) -> MemoryCapabilities: ...
    async def search(self, request: MemorySearchRequest) -> MemorySearchResult: ...
    async def get(self, memory_id: str) -> MemoryRecord | None: ...
    async def upsert(self, record: MemoryRecord, *, expected_version: int | None) -> MemoryRecord: ...
    async def delete(self, request: MemoryDeleteRequest) -> MemoryDeleteResult: ...
    async def list_core(self, request: CoreMemoryRequest) -> list[MemoryRecord]: ...
```

`expected_version` 用于避免多个 Run 并发更新同一偏好时静默覆盖。

Provider 能力声明至少包括：

```python
@dataclass(frozen=True)
class MemoryCapabilities:
    semantic_search: bool
    keyword_search: bool
    metadata_filter: bool
    versioned_update: bool
    hard_delete: bool
    ttl: bool
    max_record_chars: int
```

### 10.6 检索

```python
@dataclass
class MemorySearchRequest:
    query: str
    scopes: list[tuple[MemoryScope, str]]
    memory_types: list[MemoryType]
    top_k: int = 8
    max_tokens: int = 4000
    min_score: float = 0.45
    as_of: datetime | None = None
    filters: dict[str, object] = field(default_factory=dict)

@dataclass
class MemorySearchResult:
    status: Literal["ok", "not_configured", "timeout", "unauthorized", "failed"]
    records: list[MemoryRecord]
    error_code: str | None
    provider: str
    latency_ms: int
    accounting_accuracy: ContextAccuracy
```

排序建议：

```text
final_score =
    0.55 * relevance
  + 0.20 * importance
  + 0.15 * freshness
  + 0.10 * source_quality
```

随后做：

1. `content_hash` 去重。
2. 同一事实多版本只返回当前 active 版本。
3. 限制单一类型或单一来源垄断结果。
4. 按 `max_tokens` 装箱，而不是只按 top_k。
5. 每条结果保留 memory_id、scope、score 和来源，模型正文中使用安全短引用。

### 10.7 Core Memory

Core Memory 是少量始终可用的 profile/fact block，不是无限增长的 MEMORY.md。

建议约束：

- 默认最多 8 个 block。
- 总预算默认 4K tokens。
- 每个 block 有 `name/description/content/max_tokens/writable`。
- block 更新也走候选、版本和安全检查。
- 云端事实源为 Memory Provider；本地可投影成文件方便调试，但文件不是云端事实源。

```python
@dataclass
class CoreMemoryBlock:
    name: str
    description: str
    content: str
    max_tokens: int
    writable: bool
    source_memory_ids: list[str]
```

### 10.8 失败和降级

- Memory 未配置：返回空结果，不记录为错误。
- Provider 超时：跳过 Recall，记录 warning/Span，继续执行 Agent。
- Provider 鉴权失败：记录配置错误，不把异常文本注入 Context。
- 搜索无结果：不注入“未找到相关长期记忆”等噪声文本。
- 写入失败：不影响当前回答；记录待重试候选或审计事件。
- 删除失败：用户明确要求遗忘时必须向上层返回失败，不能假装成功。
- Memory Record 正文按不可信内容投影，使用显式引用边界，不能把其中的指令提升成 PromptSection。

这要求修改当前 `LongTermMemoryService.search_text()` 的行为：Provider 异常不能转换为普通模型文本。

## 11. Runtime 集成时序

### 11.1 普通 Turn

```text
1. resolve Studio Agent Revision/Build、RuntimeRef、ContextSpec and RuntimeLaunchContext
2. ensure session and append user event
3. Runtime Registry resolves RuntimeAdapter and its ContextCapabilities
4. canonical conversation coordinator compiles CompiledPrompt exactly once
5. load checkpoint summary、WorkingState and transcript projection
6. run bounded ContextContributors (core/recall memory, rules, Git, Skill manifest, attachments)
7. project transcript/checkpoint/tool results into ContextItem candidates
8. enforce per-item Tool Result budget and persist replacement records
9. create ContextBudget and shadow/real ContextPlan exactly once
10. create prepared turn / semantic envelope with prompt、plan、policy and source hashes
11. RuntimeExecutor dispatches to RuntimeAdapter; preprocessing reuses the prepared turn
12. RuntimeAdapter validates ownership and projects by integration mode

   ksadk_hosted:
     proactive compact if soft limit reached
     request the coordinator to re-plan after compaction, then assemble final input

   framework_assisted:
     project Prompt/Memory/ContextPolicy into native instruction/state/store
     do not rewrite a compiled graph without explicit integration

   native_runtime:
     pass base instructions/current input/resource hooks
     do not inject a second complete transcript or run duplicate compaction

13. invoke runtime/runner and emit unified RuntimeEvent
14. append assistant/tool events and checkpoint updates
15. update WorkingState when threshold/natural boundary is met
16. optionally extract platform MemoryCandidates
17. merge runtime-reported usage/context/cache facts
18. record projection and context trace with accuracy
```

`CompiledPrompt` 与 `ContextPlan` 必须带 `turn_id`/`plan_id` 并存入 prepared turn。Adapter 若没有收到计划，应按兼容模式运行并记录 capability gap；不得在 Adapter 内静默启动第二次 Planner。只有以下情况允许生成新的 `plan_revision`：首次计划处于 shadow、KsADK-owned proactive/emergency compaction 后重规划、或 PTL 一次性恢复。每次 revision 都必须关联前一版本和原因。

`ksadk_hosted` 的细化时序如下：

```text
1. compile prompt
2. load checkpoint summary and WorkingState
3. run bounded Contributors, including core/recall memory
4. project transcript/tool results into ContextItem
5. enforce per-item budget and persist replacement records
6. create ContextBudget
7. plan ContextPlan
8. proactive compact if soft limit reached
9. re-plan after compact
10. assemble runner input
11. invoke runner
12. append assistant/tool events
13. update WorkingState when required
14. optionally extract memory candidates
15. record runtime usage, cache facts and context trace
```

### 11.2 Prompt-too-long

```text
runner returns PTL
  → verify no previous emergency retry
  → persist PTL reason and largest ContextItem categories
  → if ksadk owns compaction:
       enforce/verify per-item tool-result budget
       memory flush (best effort)
       force compact
       return coordinator and create a new ContextPlan revision
       retry once
  → if framework/native owns compaction:
       invoke documented Adapter/native compaction capability when available and record runtime evidence
       otherwise return structured failure; do not silently rebuild full history
  → structured failure if still PTL
```

### 11.3 Resume/Approval

Resume 路径优先恢复以下 ContextItem：

- checkpoint summary 与 WorkingState。
- pending approval request/response。
- tool call/result pair。
- receipt/idempotency key。
- framework_ref/checkpoint_ref。
- 原 Run 的关键 prompt hash 和策略版本。

不能因为 Memory Recall 返回相关内容而覆盖真实 pending state。

Resume 时若 checkpoint 的 Prompt/Policy/Projection 版本与当前 Agent Revision/Build 不同，必须记录版本迁移事件。安全策略使用当前部署版本，历史工作状态仍保留原来源版本；不得伪装成完全相同的可重放输入。RuntimeRef 不允许在普通 Agent 编辑中隐式改变；需要跨 Runtime 恢复时必须作为显式迁移处理。

## 12. 本地与云端一致性

本节验证的是部署可移植性，不负责改变 Context ownership。同一个 Build 从 `local` 晋级到 `ksadk_managed_cloud` 时应保持 Prompt/Policy/Projection 版本和 Adapter ownership；若确需从 native/assisted 改为 KsADK-owned，必须创建新的 Runtime/Projection 版本、明确迁移 Session，并重新执行 Conformance 与 A/B，不能作为透明部署参数处理。

| 能力 | 本地默认 | 云端默认 | 一致性要求 |
|---|---|---|---|
| Prompt compiler | 本地 AgentSpec/文件/参数 | Agent Revision/Build 投影 | 相同 canonical 编译器版本、hash 和 Adapter projection 版本 |
| Token counter | provider/heuristic | provider tokenizer | ContextPlan 记录实现名称 |
| Session | SQLite/InMemory | PostgreSQL/Session Service | 相同 SessionEvent schema |
| Memory | SQLite Provider | HTTP/SDK Provider | 相同 Provider Protocol |
| Compaction | KsADK 或原生 Runtime | KsADK 或原生 Runtime | ownership 相同；KsADK-owned 时 prompt/schema/version 相同 |
| Context policy | 本地 AgentSpec/结构化配置 | Agent Revision/ContextSpec + Deployment 约束 | 相同 canonical policy shape |

本地运行不得直接读取仅云端存在的隐式环境变量决定核心算法。所有云端策略必须先规范化为：

```python
ContextPolicy
PromptPolicy
MemoryPolicy
```

然后交给相同 Runtime 代码消费。

### 12.1 Agent Revision、Build 与 Environment 边界

为了与“双构建、版本化 Agent、统一云部署”产品主线一致，Prompt/Context/Memory 在制品和环境中的归属必须固定：

| 归属 | 应包含 | 不应包含 |
|---|---|---|
| Agent Revision/Build/AgentPackage | Prompt source/bundle、compiler/projection 版本、默认 ContextPolicy、RuntimeRef、Memory 能力需求、内容 hash | 实际用户记忆、完整 Session、生产凭证、生产 Provider 地址 |
| Environment/Deployment | Runtime/模型选择、平台安全 policy、MemoryCollection/Knowledge/MCP 绑定、SecretRef、策略允许范围内的 override | 修改后的 Agent Prompt 正文、未版本化代码 |
| Run/Trace | 最终解析后的 Agent Revision/Build、policy/projection/runtime 版本和 hash、Context 精度 | 默认不保存明文 Prompt、Memory、Tool 敏感内容 |

平台安全 policy 可以在部署时收紧 Agent 配置，但不能降低安全约束；必须把最终解析版本/hash 写入 Run，以便回放、评测和审计。Memory 数据永远不打进 AgentPackage；同一个 Build 晋级到不同 Environment 时只重新绑定 Provider 和资源，不重新编译 Agent Prompt。若环境 override 改变了 Prompt 语义，必须形成新的有效配置版本并可追溯，不能作为无版本字符串覆盖。

## 13. 配置设计

Studio 当前已有 `ksadk/studio/contracts.py::ContextSpec`。第一阶段应采用**兼容扩展**：保留现有字段和默认值，在其上增加 prompt policy、分区预算、memory policy、ownership override 限制及可观测开关；内部统一归一化成 `ContextPolicy`，不要再创建一套平行的 Studio 配置对象。

配置的事实来源和生效过程为：

```text
Studio Agent Revision.ContextSpec
        + Build 锁定的 compiler/policy/projection 版本与 hash
        + Environment 允许的安全收紧和 Provider 绑定
        + 本地显式 API/配置文件（本地开发）
        ↓
ResolvedContextPolicy
        ↓
RuntimeLaunchContext / prepared turn
```

环境变量只保留为本地开发和迁移兼容入口，不作为云端 Agent 的主要事实源。第一阶段内部必须归一化成结构化配置。

```text
KSADK_CONTEXT_POLICY_VERSION=v1
KSADK_CONTEXT_SOFT_LIMIT_PERCENT=50
KSADK_CONTEXT_HARD_LIMIT_PERCENT=85
KSADK_CONTEXT_SAFETY_BUFFER_TOKENS=8000
KSADK_CONTEXT_MEMORY_MAX_TOKENS=8000
KSADK_CONTEXT_TOOL_RESULT_MAX_TOKENS=16000
KSADK_CONTEXT_KEEP_TAIL_GROUPS=8
KSADK_CONTEXT_WORKING_STATE_MAX_TOKENS=8000
KSADK_CONTEXT_CONTRIBUTOR_TIMEOUT_MS=3000
KSADK_CONTEXT_RULE_FILE_MAX_TOKENS=4000
KSADK_CONTEXT_RULE_FILES_MAX_TOKENS=12000
KSADK_CONTEXT_CACHE_BREAK_OBSERVABILITY=true

KSADK_TOKENIZER_PROVIDER=auto

KSADK_MEMORY_ENABLED=true
KSADK_MEMORY_PROVIDER=local_sqlite|http|sdk
KSADK_MEMORY_CORE_MAX_TOKENS=4000
KSADK_MEMORY_RECALL_TOP_K=8
KSADK_MEMORY_RECALL_MAX_TOKENS=4000
KSADK_MEMORY_FLUSH_BEFORE_COMPACTION=true
KSADK_MEMORY_WRITE_MODE=explicit|propose|off
```

旧变量继续映射：

```text
KSADK_LTM_BACKEND      → KSADK_MEMORY_PROVIDER
KSADK_LTM_TOP_K        → KSADK_MEMORY_RECALL_TOP_K
KSADK_LTM_INDEX        → provider-specific collection/index
COMPACTION_*           → ContextPolicy.compaction
```

优先级：

```text
Agent Revision/Build 锁定配置
  > Environment 安全收紧与 Provider 绑定
  > 本地显式 API 参数（仅本地模式）
  > 结构化配置文件
  > 环境变量
  > SDK 默认值
```

## 14. API 兼容与迁移

### 14.1 兼容原则

- 保留 `LongTermMemoryService`，内部委托给新 `MemoryCoordinator`。
- 保留 `build_context()` 一段时间，但返回结构化结果，不再以错误字符串作为正文。
- 保留现有 compaction entrypoint，内部逐步接入 ContextPlan。
- 不一次修改所有 Runner；先在 `ksadk/runtime/conversation_execution.py` canonical coordinator 和 RuntimeAdapter 边界集成。
- 为 RuntimeAdapter 增加 capability/ownership 声明；`RunnerRuntimeAdapter` 再汇总 BaseRunner 的细化能力。默认不改变未知自定义 Runtime/Runner 的真实输入。
- `ksadk/runtime/preprocessing.py` 必须优先复用 coordinator 产生的 prepared turn；旧 preparation/invocation 模块只作为兼容 shim，不新增第二套行为。
- `native_runtime` 路径不启用 KsADK 完整历史 assembler 和重复 compaction。
- 旧行为通过 feature flag 回退至少一个发布周期。

### 14.2 建议 Feature Flags

```text
KSADK_CONTEXT_ENGINE_V2_ENABLED
KSADK_PROMPT_COMPILER_ENABLED
KSADK_MEMORY_COORDINATOR_ENABLED
KSADK_MEMORY_FLUSH_ENABLED
KSADK_CONTEXT_DUAL_THRESHOLD_ENABLED
KSADK_CONTEXT_WORKING_STATE_ENABLED
KSADK_CONTEXT_CONTRIBUTORS_ENABLED
KSADK_CONTEXT_TOOL_RESULT_BUDGET_ENABLED
KSADK_CONTEXT_CACHE_OBSERVABILITY_ENABLED
```

默认发布策略：

1. 单测和 CI 开启。
2. 本地 Studio opt-in。
3. dev 环境按 Agent 开启。
4. 灰度观测 token、失败率和回答质量。
5. 默认开启，但保留快速回退。
6. 最后删除旧路径。

## 15. 代码改造清单

### 15.1 新增文件

| 文件 | 责任 |
|---|---|
| `ksadk/prompts/models.py` | PromptSection/CompiledPrompt |
| `ksadk/prompts/compiler.py` | 编译、merge、hash、分区 token |
| `ksadk/context_engine/models.py` | ContextItem/Budget/Plan/Decision |
| `ksadk/context_engine/tokenizer.py` | TokenCounter registry 与 fallback |
| `ksadk/context_engine/planner.py` | 预算与选择算法 |
| `ksadk/context_engine/assembler.py` | Chat/Responses/Runner 投影 |
| `ksadk/context_engine/capabilities.py` | Context ownership、接入模式和观测精度 |
| `ksadk/context_engine/projection.py` | Prompt/Context semantic envelope 与 ProjectionResult |
| `ksadk/context_engine/contributors.py` | Contributor Protocol、并发、超时、预算和来源审计 |
| `ksadk/context_engine/cache_observability.py` | cache usage、expected/unexpected break 诊断 |
| `ksadk/context_engine/policies.py` | 策略归一化和旧变量映射 |
| `ksadk/memory/models.py` | Record/Candidate/Search 类型 |
| `ksadk/memory/provider.py` | Provider Protocol |
| `ksadk/memory/coordinator.py` | core/recall/flush/commit 编排 |
| `ksadk/memory/policy.py` | 写入和敏感信息策略 |
| `ksadk/memory/providers/local_sqlite.py` | 本地可持久化实现 |

### 15.2 修改文件

| 文件 | 修改点 |
|---|---|
| `ksadk/runtime/adapter.py` | 增加 ContextCapabilities、ownership 和 ProjectionResult 合同 |
| `ksadk/runtime/conversation_execution.py` | canonical Turn 协调；每 Turn 编译 Prompt/生成 ContextPlan 一次；处理 plan revision |
| `ksadk/runtime/preprocessing.py` | 复用 prepared turn/semantic envelope，禁止 Adapter 侧重复规划 |
| `ksadk/runtime/executor.py` | 按 capability 分发、记录 planned/projected/runtime-reported 证据和 mismatch |
| `ksadk/runtime/factory.py`、`ksadk/runtime/adapter.py`（`RuntimeRegistry`） | 注册 Adapter capability 与版本，未知实现使用保守默认值 |
| `ksadk/runtime/launch.py` | 承载解析后的 policy、版本/hash 和 Provider 绑定，不承载 Memory 正文 |
| `RuntimeRef` / Deployment 投影相关合同 | 增加独立 `deployment_mode`，与 Adapter `integration_mode` 同时写入 Run/Trace；不在 SDK 内实现控制面生命周期 |
| `ksadk/conversations/runtime_input.py` | 移除 Memory 错误字符串注入；接入结构化 recall |
| `ksadk/conversations/model_context.py` | 委托 TokenCounter/ContextBudget，保留兼容函数 |
| `ksadk/conversations/runtime_compaction.py` | 双阈值、flush hook、ContextPlan metadata |
| `ksadk/conversations/compaction_prompt.py` | 摘要 schema v2 与敏感信息约束 |
| `ksadk/conversations/semantic_summary.py` | per-session/model 熔断键、v2 result |
| `ksadk/conversations/context.py` | SessionEvent → ContextItem 投影、WorkingState 恢复 |
| `ksadk/conversations/compaction_pipeline.py` | Tool Result replacement record、WorkingState 重注入和逐层 stats |
| `ksadk/memory/service.py` | 兼容 facade，异常结构化 |
| `ksadk/memory/ltm_backend_factory.py` | 适配新 Provider registry |
| `ksadk/skills/tool_defs.py` | manifest 转为 resource_manifest section/item |
| `ksadk/codex/runtime.py` | Codex native ownership、base_instructions、外部 Memory hook 和 runtime evidence 投影 |
| `ksadk/runners/base_runner.py` | 为 RunnerRuntimeAdapter 提供可选的细化 capability；默认保守合同 |
| `ksadk/runners/adk_runner.py` | assisted ownership、ADK instruction/session/memory 投影 |
| `ksadk/runners/langgraph_runner.py` | hosted/assisted 能力声明和显式 helper 接入 |
| `ksadk/studio/contracts.py` | 兼容扩展 ContextSpec；增加 policy/compiler/projection version 与配置校验 |
| `ksadk/studio/builder.py`、`compiler.py`、`service.py` | Build/Revision 锁定 Prompt、Policy、Runtime capability hash；编辑时保持 RuntimeRef 不变 |

首批实现不要求一次创建所有文件。建议按 PR 边界新增最小模块，避免只有空抽象没有调用方；但公开类型和事件字段必须从第一批开始稳定版本化。

### 15.3 暂不修改

- `Skill Service` 注册和版本治理接口。
- Sandbox backend。
- Tool Gateway approval contract。
- Knowledge Base Provider。
- `agentengine-server` 的 Agent/Deployment 控制面 Schema（本仓只定义所需投影字段和失败语义，跨仓另行实施）。

## 16. 分阶段实施计划

### Phase 0：基线与可观测（建议 2～3 天）

当前分支状态：shadow 数据模型、capability/ownership、Prompt 来源 trace、请求级基线采集及 Runtime 主链路旁路挂载已经落地；采集由 `KSADK_BASELINE_COLLECT` 控制，默认关闭，只记录 hash、计数、分类和精度，不记录 Prompt 正文。Phase 0 后续重点是补齐真实本地/云端样本、二维运行合同字段和版本升级 Conformance，而不是重复实现另一套 collector。

- 为现有 context token、compaction、PTL retry 增加基线指标。
- 为 CodexRuntimeAdapter、RunnerRuntimeAdapter（ADK/LangGraph 等）和自定义 Adapter 建立 ownership/capability matrix。
- 在 Run/Trace 中同时记录 `deployment_mode` 与 `integration_mode`，验证云端托管不会被误判为 KsADK ownership。
- 建立 RuntimeAdapter Context Conformance 测试骨架，固化当前实际输入与恢复行为。
- 在 `runtime/conversation_execution.py` 生成 shadow turn identity，验证同一 Turn 不发生重复 preparation、Prompt 编译或 Context 规划。
- Trace 区分 planned/projected/actual，并标注 exact/runtime_reported/estimated/opaque。
- 记录 Provider 返回的 cache read/creation usage；仅观察，不改变 Prompt。
- 建立中英文、工具密集、附件、超长 Session 测试集。
- 固化当前 checkpoint snapshot，避免重构造成恢复回退。

完成标准：能回答当前每次调用 Prompt/History/Memory/Tool 大约各占多少 token、由谁管理 Prompt/History/Compaction/Memory/Skill、统计精度是什么，以及 PTL/compaction 成功率和 expected/unexpected cache-break 率是多少；无法观测的 Adapter/Runner 明确标为 opaque。

### Phase 1：数据模型与 Prompt Compiler（建议 3～5 天）

- 新增 PromptSection/CompiledPrompt。
- 统一 instructions、平台规则和 resource manifest。
- 新增 ContextCapabilities 和三种 integration mode。
- 兼容扩展 Studio `ContextSpec`，Build/Revision 记录 compiler、policy、projection 和 capability hash。
- 实现指令来源、merge policy、单文件/总预算和 stable/dynamic boundary。
- 为各 RuntimeAdapter 输出可审计的 PromptProjectionResult，不要求最终物理消息格式一致。
- 增加 prompt/section/stable-prefix hash 和 expected/unexpected cache-break 诊断。
- 保持对 Runner 输出完全兼容。

完成标准：相同 Build 和策略输入在本地和云端得到相同 canonical 编译结果；同一 RuntimeAdapter projection 结果一致；安全规则不能被 request instructions、Memory 或 Tool 输出覆盖。

### Phase 2：ContextPlan 与预算（建议 5～8 天）

- 新增 ContextItem/Budget/Plan。
- 将 Session round、checkpoint、tool pair、memory result 投影成 item。
- 新增 ContextContributor Protocol，首期仅 shadow 收集来源和状态。
- 引入分区预算、required/group 原子性和决策审计。
- 引入单个 Tool Result 预算和 content replacement record。
- 在 canonical conversation coordinator 先以 shadow mode 每 Turn 生成一次 Plan，不改变实际 messages；Adapter 只上报 ProjectionResult。
- 对比通过后启用 assembler。

完成标准：hosted 路径的 ContextPlan token 与实际 usage 偏差可量化；assisted/native 路径能区分 projected 和 actual；不会产生孤立 tool result、重复历史、无来源动态上下文或丢失 pending approval。

### Phase 3：有序降载与 Working State（建议 5～8 天）

- 仅在 KsADK-owned 路径启用主动 50% 整理、85% 紧急压缩。
- framework/native-owned 路径只调用已声明的原生 compaction 能力并记录结果，不运行第二套摘要链路。
- 完成 Tool Budget → Snip → Microcompact → Semantic Compact → PTL retry 的逐层编排。
- 在 ContextCheckpoint 中实现结构化 WorkingState，优先确定性提取，模型辅助可关闭。
- 实现压缩后 WorkingState、pending state 和 artifact reference 重注入。
- 升级 summary v2。
- PTL 最多一次重试。

完成标准：超长会话可持续运行；压缩后能恢复当前目标、下一步、错误修正和 pending state；Transcript 不删除；native Runtime 不出现双重 compaction。

### Phase 4：Memory Candidate 与 Provider v2（建议 7～12 天）

- Record/Candidate/Search 合同。
- 压缩前 best-effort Memory Flush；WorkingState 不直接写入长期记忆。
- SQLite Provider。
- 旧 local/http/sdk backend adapter。
- scope、TTL、版本冲突和删除语义。
- 敏感信息拒绝和来源追踪。

完成标准：本地和云端 Provider 契约测试一致；用户删除记忆后不能再次召回；异常不污染模型上下文。

### Phase 5：Contributor 接管、灰度与清理（建议 4～7 天）

- 逐个让 Rules/Git/Memory/Skill/Attachment Contributor 从 shadow 切换为真实候选来源。
- Studio 按 Agent Revision/dev 环境灰度，确保同一 Build 的本地运行与云端运行使用相同 policy/compiler/projection 版本。
- 比较 token、延迟、缓存命中/意外失效、PTL、任务完成率和 WorkingState 连续性。
- 回放失败 Case。
- 默认启用新路径，保留旧路径回退。

## 17. 测试方案

### 17.1 Prompt 单测

- section 排序确定性。
- 合并和覆盖规则。
- safety section 不可覆盖。
- 换行标准化和 SHA-256 稳定。
- stable prefix 不受 session/run/time 影响。
- 单一动态 section 变化不改变 stable prefix hash。
- section 顺序漂移和动态字段误入稳定前缀能触发 unexpected cache-break 诊断。
- 指令文件发现顺序、真实路径去重、单文件和总预算。
- Skill manifest 不包含完整正文。
- Prompt 超预算返回配置错误。

### 17.2 Context Planner 单测

- required item 永远优先。
- tool call/result 原子保留。
- approval request/response 原子保留。
- Responses API call/output ID 保持一致。
- 最近 round 优先于旧 round。
- recalled memory 按 score/token budget 装箱。
- hard limit 下执行正确降级顺序。
- 单个 Tool Result 超限后保留协议配对、replacement record 和 artifact reference。
- Contributor 超时、超预算、异常和 untrusted 内容不能改变高信任 Prompt。
- 无候选项时产生最小合法请求。

建议采用 property-based test：随机生成 round/tool/approval 序列，验证 Planner 后不存在孤儿事件、超预算和顺序逆转。

### 17.3 Compaction 单测

- proactive/emergency 阈值。
- append-only Transcript 不变。
- checkpoint 覆盖 seq 连续且不越过 pinned group。
- previous summary 合并。
- WorkingState 的 current goal、next action、错误修正和 pending state 在压缩后保留。
- WorkingState 并发更新遵守 per-session lock/version，stale extraction 不覆盖新版本。
- semantic 失败回退 extractive。
- Memory Flush 成功、失败、超时。
- PTL 只重试一次。
- 摘要不包含 fixture secret。

### 17.4 Memory 契约测试

所有 Provider 共用同一测试套：

- add/get/search/update/delete。
- scope 隔离。
- expected_version 冲突。
- TTL/expired 不召回。
- superseded 版本不作为 active 返回。
- top_k 与 max_tokens 同时生效。
- content_hash 去重。
- Provider timeout/401/500 的标准错误。
- 删除后缓存失效。

### 17.5 集成与 E2E

至少覆盖：

1. 20 轮普通中文对话。
2. 50 轮中英混合代码任务。
3. 多个并行 tool call/result。
4. approval 等待后 resume。
5. 大型日志/附件结果。
6. 主动 compaction 后继续对话。
7. 模拟 PTL 后强制 compaction/retry。
8. 跨 Session 召回用户明确保存的偏好。
9. 用户删除记忆后新 Session 不再召回。
10. Memory Service 不可用时 Agent 降级运行。
11. 单个超大 Tool Result 产生 artifact replacement 后继续完成任务。
12. Prompt 动态后缀变化不导致稳定前缀异常失效。
13. Contributor 超时或返回 prompt injection 时不污染安全 Prompt。
14. 压缩/Pod 恢复后从 WorkingState 直接继续下一步，不要求用户重述。

对 ADK、LangGraph、OpenAI Responses 和 Codex native 路径分别做 smoke，不能只验证一个 RuntimeAdapter。Codex 用例必须断言未重复注入完整 Transcript、未同时运行 KsADK 与 Codex 两套 compaction；自定义 Adapter/Runner 必须验证默认保守/opaque 行为。

此外至少覆盖两个容易混淆的云端路径：

1. `ksadk_managed_cloud + native_runtime`：Codex thread 可恢复，KsADK 不注入第二份完整历史，观测不得标为 `exact`；
2. `ksadk_managed_cloud + ksadk_hosted`：自有 Harness 的最终输入确由 KsADK assembler 产生，Prompt/Context/Compaction 可提供 `exact` 证据。

若支持 Codex MCP 子 Agent，还需验证外层 Harness 与内层 Codex 分别建 span、预算和 ID，审批后的任务包是唯一跨边界输入，不传播完整 Transcript 或跨租户 Memory。

### 17.6 RuntimeAdapter Context Conformance

所有内置 RuntimeAdapter 复用参数化测试套；RunnerRuntimeAdapter 还需对其内置 Runner 做二级参数化。至少断言：

- capability 声明与实际 Prompt/History/Memory/Skill 投影一致。
- SDK/Adapter/Runner 版本变化后 capability hash 和测试证据更新。
- native/assisted 路径未被 KsADK assembler 或 compaction 越权接管。
- `exact/runtime_reported/estimated/opaque` 与可获取证据一致。
- Resume、Approval 和 PTL 路径不改变 ownership。
- 同一 `turn_id` 只有一个 canonical `CompiledPrompt` 和初始 `ContextPlan`；合法 re-plan 必须产生有关联原因的 plan revision。
- Studio 锁定的 RuntimeRef、policy/compiler/projection version 与执行时解析结果一致。

## 18. 指标与验收标准

### 18.1 运行指标

| 指标 | 目标 |
|---|---:|
| Context 估算 token P50 偏差 | ≤ 8% |
| Context 估算 token P95 偏差 | ≤ 20% |
| Prompt stable-prefix 非预期 cache break | 灰度基线后持续下降；默认开启前无系统性回归 |
| hosted 路径 ownership 错配 | 0 |
| Runner capability 与 Conformance 不一致 | 0 |
| native 路径重复历史/重复 compaction | 0 |
| PTL 后恢复成功率 | ≥ 99% |
| 单次请求 PTL 重试次数 | ≤ 1 |
| compaction 导致孤儿 tool event | 0 |
| pending approval 丢失 | 0 |
| 压缩后 WorkingState 核心字段丢失 | 0 |
| Contributor 越权覆盖高信任 Prompt | 0 |
| Memory Provider 故障导致 Agent 请求失败 | 0（显式删除除外） |
| Secret 写入长期记忆 | 0 |
| 删除后再次召回 | 0 |

### 18.2 质量验收

- 压缩后 Agent 能正确说明当前用户目标、已完成工作和下一步。
- 关键文件路径、错误码、工具结果和审批状态不丢失。
- 不相关长期记忆不会因为高 importance 强行注入。
- 旧偏好被新明确偏好替代后，默认只召回新版本。
- Prompt、Context、Memory 的来源和 token 可在 Trace 中解释。
- Prompt Cache 失效能区分预期版本变化和疑似动态字段/顺序漂移。
- Tool Result 被替换后能从 Session 审计模型实际看到的摘要和原 Artifact 引用。
- WorkingState 与长期 Memory 边界明确，临时计划不会跨 Session 被错误召回。
- Trace 能区分平台计划、Runner 投影和 Runtime 实际报告，且不把 estimated/opaque 展示为精确值。
- 相同策略下本地与云端 ContextPlan 结构一致。

### 18.3 回归门禁

以下任一发生则不得默认开启：

- Session/Run/Checkpoint 现有测试失败。
- Resume/Approval 语义回退。
- Tool call/result 投影不合法。
- P95 延迟显著上升且无明确收益。
- 平均输入 token 上升超过 10%。
- 任务成功率或回放评测显著下降。
- Trace/日志出现未脱敏 Prompt、Memory 或 Secret。
- Runner Conformance 与 capability 声明不一致。
- Contributor 可绕过 Planner、预算、信任或 approval 直接拼接输入。

## 19. 安全要求

- Prompt、Memory、Context Trace 默认只记录 hash、长度、类型和脱敏摘要。
- Memory Candidate 进入 Provider 前执行 Secret/PII 检查。
- 禁止保存 API Key、AK/SK、cookie、Authorization header、临时签名 URL。
- Memory scope 必须由可信 Principal/Runtime Projection 决定，不能信任用户自行提交 scope_id。
- Tool 输出中的敏感字段在进入摘要模型前脱敏。
- 用户明确删除记忆时需支持 hard delete 能力声明；不支持时明确返回。
- Memory Recall 视为不可信外部内容，不能覆盖 platform safety。
- Prompt injection 文本只能作为引用内容进入低优先级 ContextItem。
- Contributor 不能声明高于其注册配置的 trust level，不能自行扩大 token/timeout/failure 权限。
- Artifact reference 必须使用受控 ID 和授权解析，不能把临时签名 URL、宿主绝对路径或凭证写入长期 Context/Memory。
- cache observability 只记录 hash、usage 和 break reason，不落完整 Prompt 或跨租户 request fingerprint。

## 20. 关键设计决策记录

### ADR-001：Context Engine 位于 KsADK Runtime

原因：RuntimeAdapter 投影、Session 事件和 PTL 恢复协调都发生在 Runtime；本地和云端必须复用相同语义模型、策略合同和投影版本。控制面只下发结构化策略，不负责每 Turn 拼接 messages。Context Engine 是纯计划模块，`ksadk/runtime/conversation_execution.py` 是 canonical coordinator；在 `framework_assisted/native_runtime` 模式下，两者都不拥有框架内部的最终 messages、Session 或 compaction。

### ADR-002：Transcript 保持 append-only

原因：审计、恢复、回放和评测依赖原始事件。Compaction 只生成 checkpoint 和投影，不删除历史。

### ADR-003：Memory 数据通过 Provider 管理

原因：本地需要轻量实现，云端需要多租户、TTL、删除和治理。SDK 定义运行时合同，不承载完整管理面。

### ADR-004：Memory 写入采用 Candidate 管线

原因：把普通模型输出直接写入长期记忆会放大幻觉、隐私和冲突问题。候选模型允许安全检查、去重、审批和版本更新。

### ADR-005：Skill 不作为 Memory

原因：Skill 是可执行的程序性知识，需要版本、签名、测试和审批；Memory 是事实和经历。两者生命周期和风险不同。

### ADR-006：采用主动/紧急双阈值

原因：只在窗口即将耗尽时压缩会增加同步延迟和 PTL 风险。主动阈值用于提前整理，紧急阈值用于安全兜底。

### ADR-007：第一阶段不引入图记忆

原因：当前主要问题是 Prompt 分层、预算、压缩和记忆生命周期，而不是复杂关系推理。先稳定 Provider 合同，未来可增加图 Provider，不影响 Runtime。

### ADR-008：统一合同，不统一所有 Runner 内部实现

原因：产品需要统一 Agent Revision/Build、部署、治理、Trace 和评测，但 Codex、ADK、LangGraph、未来 Claude 的 Agent loop、Session、Context 和恢复能力不同。强制统一会形成最低能力公分母，并导致双重历史、双重压缩或破坏原生恢复。KsADK 统一 semantic envelope、capability、projection 和事件合同，Runtime/框架保留原生 ownership。

### ADR-009：RuntimeAdapter 必须声明 Context Ownership

原因：仅依靠框架名称或反射无法安全判断谁管理 history/compaction。显式 capability 可让 Runtime、Studio、测试和故障处理采用一致决策；未知自定义 Adapter/Runner 默认保守且 opaque，避免平台越权改写输入。

### ADR-010：Context 可观测结果必须声明精度

原因：KsADK-owned 路径可以接近精确地解释最终输入，native Runtime 往往只暴露 usage 或部分事件。统一 Trace 不等于编造统一可见性，因此 planned、projected、runtime-reported 必须分开记录，并用 exact/runtime_reported/estimated/opaque 标明证据等级。

### ADR-011：Working State 属于 ContextCheckpoint，不属于长期 Memory

原因：当前目标、阶段、下一步、近期文件和 pending state 主要服务于当前 Session 的连续性，生命周期和删除语义与跨 Session 事实不同。将其放入长期记忆会导致旧计划污染新 Session；单独建设第五套存储又会重复 Session Store，因此作为强类型 checkpoint 字段最合适。

### ADR-012：动态上下文统一通过 ContextContributor

原因：Memory、Git、规则文件、附件和 Hook 如果分别在 Runner 中拼接，会重新产生顺序、预算、信任和错误语义漂移。Contributor 只负责产生候选 ContextItem，Planner 仍拥有是否进入请求的最终决策，外部内容不能借此提升权限。

### ADR-013：只诊断 Prompt Cache，不实现通用 Completion Cache

原因：稳定前缀命中率可以暴露 Prompt 漂移和不必要成本，但缓存完整模型回答涉及多租户隔离、权限、时效和副作用一致性，不属于本方案。KsADK 只消费 Provider usage、记录 expected/unexpected break，不缓存回答正文。

### ADR-014：外部分析只作设计参考，不复制内部实现

原因：第三方 Claude Code 分析和二次文章不构成官方 SDK 合同，且可能涉及未授权还原内容。KsADK 只吸收通用架构思想，具体行为以自身代码、测试、官方 SDK 和公开协议为准。

### ADR-015：Context 能力归属于 RuntimeAdapter

原因：当前 Studio 和服务端执行已经以 RuntimeRef、RuntimeRegistry、RuntimeExecutor、RuntimeAdapter 为稳定边界，Codex 也已是直接 Adapter，不再对应一个通用 Runner 文件。若继续只在 Runner 上声明 ownership，直接 Adapter 和未来原生 SDK Runtime 会绕过统一合同。Runner 可以提供细化能力，但必须由 RuntimeAdapter 汇总并对 Runtime 层负责。

### ADR-016：每个 Turn 只生成一份 canonical Prompt 和初始 ContextPlan

原因：conversation coordinator、preprocessing、Adapter 和 Runner 若都能自行准备输入，会造成重复 Memory Recall、重复历史、预算不一致和难以解释的 Prompt 漂移。canonical coordinator 生成带 `turn_id` 的 prepared turn；Adapter 只投影。只有 KsADK-owned compaction 或 PTL 恢复可以创建有关联原因的新 plan revision。

### ADR-017：Studio Agent Revision/Build 锁定语义配置

原因：RuntimeRef、Prompt、ContextPolicy、compiler/projection 版本共同决定 Agent 行为。只保存最终字符串或依赖部署时隐式环境变量，会让本地/云端、回放/线上和版本间无法对齐。Build 锁定来源与 hash，Environment 只做允许范围内的安全收紧和 Provider 绑定。

### ADR-018：部署位置与 Context Ownership 正交建模

原因：托管容器只说明 KsADK 负责运行生命周期，不说明平台拥有第三方 Agent 的 thread、history、compaction 或最终模型输入。把两者合并会让 Managed Codex 被错误接管，也会让可观测精度被夸大。`DeploymentMode` 放在 Runtime/Deployment 合同，`ContextIntegrationMode` 放在 Adapter capability；AgentVersion、Run、Trace 和评测同时记录二者。

### ADR-019：Codex SDK Runtime 不定义为 KsADK Harness

原因：Codex SDK/CLI 自己拥有 thread、Agent Loop、历史和压缩。KsADK 可以构建、部署、鉴权、绑定资源并统一事件，但不能因此声称拥有最终 Context。若 KsADK 自有 Agent Loop 通过 MCP 调用 Codex，则 Harness ownership 只覆盖外层，Codex 子线程保持 native ownership，使用嵌套 Trace 明确边界。

## 21. 当前落点与后续开发顺序

当前分支已经完成原“第一个 PR”范围的大部分内容：Prompt/Context 数据模型、RuntimeAdapter capability/ownership、shadow `CompiledPrompt/ContextPlan`、Runtime 主链路旁路采集、Conformance/invariant 测试和脱敏基线输出均已存在，且默认不改变 Runner 输入。不能再按旧计划重复搭建平行模型。

后续建议顺序：

1. **收口 Phase 0 合同**：新增独立 `DeploymentMode`，在保持历史 `execution_target` 兼容的前提下，把 deployment + integration/ownership 同时写入 AgentVersion、Run、Trace 和 baseline；采集 ADK、Codex、本地和云端预发真实样本。
2. **完成 Prompt shadow 验收**：明确 `platform_safety`、Agent identity/policy 的真实来源和发布语义；不得为了填满 Section 而虚构当前没有发送的 Prompt。补齐 projection evidence 和稳定前缀 cache-break 基线。
3. **仅对 KsADK-owned 路径启用 Prompt/Context 行为**：Prompt Compiler 与 assembler 分成可独立回退的开关，先处理 Tool Result 单项预算；Managed Codex 和其他 native Runtime 继续保持原生 history/compaction。
4. **Working State 与有序 Compaction**：只在 owner 明确且 Phase 0/Prompt 门禁通过的路径启用。
5. **Memory Candidate/Provider v2**：先保证 scope、删除、冲突和异常隔离，再做召回效果优化。
6. **Contributor 接管和逐 Runner 灰度**：Rules/Git/Memory/Skill/Attachment 逐个切换，不在一个 PR 同时改变多个来源。

每个行为型 PR 只引入一个主要变量，并使用评测文档中对应 Case 在同一 `DeploymentMode + ContextIntegrationMode + Runner` 组合内比较 Baseline/Candidate。这样才能把收益和回归归因到具体改动，并支持独立回退。

## 附录 A：ContextPolicy 示例

```yaml
version: v1

budget:
  soft_limit_percent: 50
  hard_limit_percent: 85
  safety_buffer_tokens: 8000
  reserved_output_tokens: auto
  reserved_reasoning_tokens: auto

sections:
  prompt:
    percent: 15
    max_tokens: 24000
  resource_manifest:
    percent: 5
    max_tokens: 8000
  core_memory:
    percent: 5
    max_tokens: 8000
  recalled_memory:
    percent: 10
    max_tokens: 16000
  checkpoint_summary:
    percent: 10
    max_tokens: 16000
  working_state:
    percent: 5
    max_tokens: 8000
  recent_history:
    percent: 35
    max_tokens: 64000
  tool_and_attachment:
    percent: 10
    max_tokens: 16000

compaction:
  keep_tail_groups: 8
  emergency_keep_tail_groups: 3
  semantic_enabled: true
  semantic_timeout_ms: 45000
  max_retry_after_prompt_too_long: 1
  flush_memory_before_compaction: true
  working_state:
    enabled: true
    max_tokens: 8000
    update_min_token_growth: 5000
    extraction_timeout_ms: 15000

tool_results:
  default_max_tokens: 8000
  replacement_strategy: artifact_summary
  preserve_error_tail: true

contributors:
  default_timeout_ms: 3000
  default_failure_mode: skip
  allow_external_platform_trust: false

cache_observability:
  enabled: true
  record_section_hashes: true
  completion_cache: false

memory:
  core_max_tokens: 4000
  recall_top_k: 8
  recall_max_tokens: 4000
  min_score: 0.45
  write_mode: propose
```

## 附录 B：结构化错误示例

```json
{
  "error": {
    "code": "context_budget_exceeded",
    "message": "required context exceeds the model input budget",
    "retryable": false,
    "details": {
      "model": "example-model",
      "context_window_tokens": 200000,
      "max_input_tokens": 140000,
      "required_tokens": 151200,
      "policy_version": "v1",
      "plan_id": "ctxplan_xxx"
    }
  }
}
```

禁止在错误 details 中返回完整 Prompt、Message、Memory 或 Tool 参数。

## 附录 C：首批测试文件建议

```text
tests/prompts/test_prompt_compiler.py
tests/context_engine/test_budget.py
tests/context_engine/test_planner.py
tests/context_engine/test_group_invariants.py
tests/context_engine/test_token_counter.py
tests/context_engine/test_assembler_chat.py
tests/context_engine/test_assembler_responses.py
tests/context_engine/test_contributors.py
tests/context_engine/test_cache_observability.py
tests/context_engine/test_runtime_adapter_conformance.py
tests/context_engine/test_tool_result_budget.py
tests/context_engine/test_working_state.py
tests/runtime/test_context_capabilities.py
tests/runtime/test_context_projection.py
tests/runtime/test_conversation_execution.py
tests/runtime/test_executor.py
tests/runners/test_codex_runtime_adapter.py
tests/server/test_runtime_executor_routes.py
tests/studio/test_context_spec.py
tests/studio/test_runtime_selection.py
tests/memory/test_provider_contract.py
tests/memory/test_memory_policy.py
tests/memory/test_memory_flush.py
tests/integration/test_context_compaction_v2.py
tests/integration/test_memory_recall_context.py
tests/integration/test_prompt_too_long_recovery_v2.py
```
