# KsADK Prompt、Context 与 Memory 全量验证报告（2026-08-12）

## 1. 验证结论

本轮已完成本地 Studio 真实体验、跨 Runner 行为一致性、Prompt/Context/Memory 契约、长上下文与 PTL、故障降级、审计证据以及既有云端预发实例可用性验证。

结论为：**PCM 的平台管理、映射和审计主链路已经成立，但当前版本只达到“可继续灰度”的条件，不满足“全部 Runner 全量接管并直接生产发布”的条件。**

已证明的核心能力：

- LangGraph 的 `ksadk_hosted` 路径由 KsADK 生成 Prompt 和 ContextPlan，并可展示 planned/projected/actual；
- ADK、LangGraph、Codex 对相同不可覆盖规则表现出一致的关键语义；
- Native Runtime 的实际上下文没有被误报为平台精确拥有，证据精度与 ownership 如实展示；
- 平台长期 Memory 能跨 Session 召回，并按 tenant/workspace/scope 隔离；
- Memory 写入经过候选、敏感信息拒绝、冲突更新、版本和删除语义；
- 大 Tool Result 可降载，Tool Call/Result 保持原子配对；
- 主动软/硬阈值、PTL emergency、最多一次重试、Compaction 失败治理和 Memory Flush 已通过自动化链路；
- Studio 已能用开发前 Baseline 与当前 Candidate 做用户可见 A/B 对比。

本轮验收后已补齐的两个代码缺口：

- Codex Manifest 新增独立 `task_prompt` 快照；运行时在 `codex_run.py` 合并为原生 `base_instructions`，Prompt evidence 仍以 system/task 两个来源生成分段 hash；
- extractive fallback 会把修正语句写入结构化“错误修正”段，并对最新长指令做有界首尾保留，避免既丢关键 Region 修正，又因“完整保留”重新撑爆上下文。

仍然不应宣称已经完成的能力：

- Studio Context Preview 的模型预算解析与 Agent ContextSpec 仍存在不一致场景；
- 当前分支的最新代码尚未重新构建并部署为新的云端 Artifact；现有云端验证对象是此前部署版本；
- 云端长期记忆仍是容器本地 SQLite，滚动更新持久性未通过，生产应接 LongTermMemoryService/HTTP/SDK Provider；
- 真实云端 PTL 压力、滚动更新、Provider 故障和关闭开关回退尚需在新 Artifact 上执行。

## 2. 验证环境

| 项目 | 验证对象 |
|---|---|
| 本地分支 | `feature-prompt-context-optimize` |
| Python | 仓库 `.venv`，Python 3.12 |
| Studio | `http://127.0.0.1:8899` |
| Workspace | `ksadk-pcm-canary` |
| Baseline | `PCM Baseline（开发前）`，LangGraph，framework assisted |
| Candidate | `PCM Candidate（当前）`，LangGraph，ksadk hosted |
| Conformance | LangGraph、ADK、Codex 三个独立 Agent Build |
| 云端 Runtime | `ksadk-pcm-canary`，北京 6，1/1 Ready |

报告不记录 API Key、AK/SK、Cookie、私有镜像凭证或 Prompt 正文中的敏感信息。

## 3. Studio 用户可见 A/B

### 3.1 长期记忆

在 Candidate 的第一个 Session 明确保存暗号 `琥珀松鼠-812-PCM`，在新 Session 提问暗号：

- Candidate 回答：`琥珀松鼠-812-PCM`；
- Baseline 回答：`不知道`。

这证明当前差异不是聊天窗口继续携带历史，而是 Candidate 的平台 Memory 跨 Session 召回。

### 3.2 Context 可解释性

同一类本地调用中：

| 指标 | Candidate | Baseline |
|---|---:|---:|
| Prompt 组成 | 3 部分 | 3 部分 |
| planned | 242 | 未上报 |
| projected | 242 | 未上报 |
| actual/runtime input | 206 | 64 |
| ownership | `ksadk_hosted` | `framework_assisted` |
| 用量 | 输入 206、输出 833、总计 1039 | 输入 64、输出 236、总计 300 |

该结果证明平台能解释“计划选择了什么、交给 Runner 什么、Provider 实际报告多少”，但 `estimated` 仍只是估算，不等同于模型最终精确输入。

## 4. 跨 Runner Conformance

三类 Runner 使用同一组关键规则：线上变更先 dry-run、缺少 Region 必须询问、最后输出 `NEXT_STEP`。输入同一条覆盖攻击后：

| Runner | 结果 | ownership / integration | accounting |
|---|---|---|---|
| LangGraph | 保留全部三条规则 | `ksadk_hosted` | estimated，planned/projected 可见 |
| ADK | 保留全部三条规则 | `framework_assisted` | runtime reported |
| Codex | 保留全部三条规则，并询问 Region | `native_runtime` | runtime reported |

因此当前可证明的是“关键语义一致”，不是“各 Runner 收到字节级完全相同 Prompt”。Codex/ADK 的原生上下文仍由其 Runtime 负责，KsADK 通过 Adapter 投影并记录可取得的证据。

## 5. Prompt 验证

### 已通过

- PromptSection/CompiledPrompt 模型、顺序、hash、稳定前缀、请求级 instructions；
- LangGraph 与 ADK 的相同 system/task/request 编译结果一致；
- 不可覆盖规则在三类 Runner 的真实回答中均未被用户请求覆盖；
- 运行记录能关联 Prompt 组成、Runner、模型、Build 和用量。

### Codex 分段修复

新建或重新保存的 Codex Agent 会把 system 保存为 `prompt`，把 task 保存为可选 `task_prompt`。不可变 Build 同时锁定两者；`CodexRunSpecResolver` 只在运行投影时合并为 `base_instructions`，并继续通过 `agent_system`/`agent_task` 生成分段证据。旧版已经合并的 Codex Manifest 仍可兼容运行，但要获得分段审计必须重新保存并构建新 Artifact。

## 6. Context、Compaction 与 Working State

### 已通过

- Prompt、当前输入、历史、Memory 与 Tool Result 进入统一 ContextPlan；
- required/current input 不被丢弃；
- User/Assistant 历史按 round 原子保留或丢弃；
- 大 Tool Result 从 20,000 tokens 降载到预算内，并与 Tool Call 保持同组；
- 软阈值、硬阈值、PTL emergency、最多一次受控重试；
- Compaction 失败触发治理，不无限循环；
- Checkpoint 与 Working State 下一轮重注入；
- Memory Flush 为 best effort，不阻断紧急 Compaction。

### 压力场景修复

无摘要模型时，修正语句不再依赖“恰好是最后一条消息”：fallback 会从所有被压缩 User Event 中识别修正标记，形成有预算上限的“错误修正”段；Working State 可确定性解析该段。最后一条超长用户指令同时保留首尾，最大 8192 字符，避免无界增长。新增回归覆盖“Region 修正在前、后面还有普通消息”和“超长指令的修正在尾部”两类场景。

## 7. Memory 验证

| Case | 结果 |
|---|---|
| 同一用户/scope 召回 | 通过 |
| 跨 Session 召回 | 通过 |
| 其他用户/scope 隔离 | 通过，返回 0 条 |
| 冲突更新 | 通过，`conflict_supersede`，版本 1 → 2 |
| API Key 敏感信息 | 通过，候选拒绝且未提交 |
| 用户明确删除 | 通过，删除后召回 0 条 |
| Provider 异常 | 通过，返回 `provider_error`，不注入错误文本 |
| 云端滚动更新持久性 | 未通过，SQLite 文件不可作为生产事实源 |

## 8. 自动化结果

本轮 fresh verification：

- PCM Prompt/Context/Memory/Conversation/Studio 相关集合：`345 passed, 1 skipped`；
- Tool Result 原子降载与孤儿历史回归：`19 passed`；
- PTL、双阈值、Memory Flush、Working State：`58 passed`；
- Codex 与非 Codex 根 Manifest 共存回归：`23 passed`；
- 此前同一工作区的扩展 Runtime/Conversation/Adapter 回归：`437 passed`。
- 两个生产缺口及 Studio 预览/Build/Working State 调用链的定向回归：`82 passed`；
- Prompt、Context Engine、Conversation 三个受影响模块的完整回归：`276 passed`；Codex 并发证据用例独立复跑：`7 passed`；
- 全仓运行结果：`3122 passed, 20 skipped, 28 failed`。28 项包含既有模块体积门禁、未构建 wheel、缺少可选 checkpoint 依赖、CLI 环境污染和全仓顺序下的异步测试隔离问题；本次涉及的 Codex 失败独立复跑均通过，因此不得把全仓结果表述为“零失败”。

唯一 skip 为需要真实云端部署条件的用例，不计作通过。

本轮验证发现并修复：

- Codex Agent 创建时误解析/覆盖 LangGraph 根 Manifest；
- 超大 Tool Result 降载时可能只保留 Result、丢失同组 Tool Call。

## 9. 云端验证边界

当前云端 Runtime 状态：`RUNNING`，副本 `1/1 Ready`。实际 invoke 成功并返回指定验收标记，证明此前部署版本的 Endpoint、鉴权、模型和基础 Prompt 链路可用。

但这不能证明当前工作区未提交修改已经在云端生效。完成当前代码的真实云端验收还必须：

1. 从当前提交生成新的不可变 Artifact，并记录 git SHA、依赖锁和 content hash；
2. 新建 canary Runtime 或部署为新 revision，不能覆盖现有可用版本；
3. 在新 Runtime 执行 Prompt Conformance、跨 Session Memory、长会话 Compaction/PTL、Provider 故障、关闭开关和回滚；
4. 用生产 LongTermMemoryService/HTTP/SDK Provider 替换容器 SQLite；
5. 做滚动更新后召回、双副本隔离和 tenant/workspace 权限验证；
6. 验收通过后再逐 Runner 灰度，保留旧 Artifact 一键回滚。

## 10. 最终验收判定

| 能力域 | 判定 |
|---|---|
| LangGraph 平台 Prompt/Context 接管 | 通过 |
| ADK/Codex 关键语义映射 | 通过 |
| 跨 Runner 字节级/结构级完全一致 | 不适用且未实现 |
| Prompt 来源审计 | 通过；旧 Codex Artifact 需重新保存/构建后获得分段证据 |
| Context 预算与决策审计 | 本地通过，Preview 预算口径待修 |
| 长任务 Compaction/PTL | 自动化通过，真实新云 Artifact 待测 |
| Working State | 结构化场景与 fallback 修正保留通过 |
| 本地长期 Memory | 通过 |
| 生产长期 Memory | 未完成 Provider 接入 |
| Studio A/B 用户体验 | 通过 |
| 当前代码云端发布验收 | 未完成新 Artifact 部署 |

因此本期当前建议状态为：**平台能力主链路完成，Codex Prompt 分段与 fallback 修正保留代码缺口已补齐，可进入受控灰度；在生产 Memory Provider 接入和新 Artifact 云端验收完成前，不建议宣布生产全量完成。**
