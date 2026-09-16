# Skill 评测 Trace 类型清单

本文只统计 Skill 评测相关的两类数据：

- Base Agent 的 LangGraph/LangChain 自动插桩 span；
- KsADK `execute_skills` 下重建的 Skill child span。

这里的 `observation type` 指 Langfuse 中的 observation 类型；`span event` 指 OTLP span 的 `events[]`，不是独立 observation。

## Base Agent LangGraph spans

Base Agent 没有手写 `span.add_event()` 业务事件。相关 span 由 KsADK 启用的 `openinference-instrumentation-langchain` 产生，按 `openinference.span.kind` 映射：

| LangGraph/LangChain span 场景 | OpenInference kind | Langfuse observation type |
|---|---|---|
| 模型调用：Skill 选择、Specialist、执行结果总结 | `LLM` | `generation` |
| 工具调用：`list_skills`、`search_skills`、`load_skill` | `TOOL` | `tool` |
| LangGraph graph/node 编排 | `CHAIN` | `chain` |
| 名称包含 `agent` 的运行 | 自动识别为 `AGENT` | `agent` |
| 没有 OpenInference kind 且没有 token usage 的普通 span | 无 | `span` |

当前 Base Agent 源码中明确存在的调用点包括：`selector.ainvoke()`、`specialist.ainvoke()`、结果生成模型的 `model.ainvoke()`，以及 `load_skill.invoke()`；它们是否形成独立 observation 取决于运行时是否启用 LangChain 自动插桩。

## KsADK Skill child spans

`project_skill_events()` 在当前 `execute_skills` span 下重建以下 child span；span 名就是事件名：

| Skill child span | Langfuse observation type |
|---|---|
| `skill.package.downloaded` | `span` |
| `skill.package.hash_verified` | `span` |
| `skill.package.extracted` | `span` |
| `skill.load.completed` | `span` |
| `skill.load.failed` | `span` |
| `sandbox.session.cleaned_up` | `span` |
| `sandbox.session.cleanup_failed` | `span` |
| `skill.execution.completed` | `span` |
| `skill.execution.failed` | `span` |
| `skill.artifact.created`（事件带 `ended_at` 时） | `span` |

`skill.load.started`、`skill.execution.started`、`sandbox.session.created` 只用于和结束事件配对，不单独生成 child span。

Skill child span 只写入 `ksadk.skill.*` 属性，没有 `openinference.span.kind` 或 token usage，因此不会映射成 `generation`、`tool` 或原生 `skill` 类型；在无额外兼容属性时按普通 `span` 观察。

## Parent span events

以下事件写入当前 `execute_skills` parent span 的 `events[]`，不生成独立 Langfuse observation：

```text
skill.candidates.resolved
skill.selection.completed
skill.selection.skipped
skill.package.cache_hit
skill.manifest.parsed
skill.artifact.created
skill.result.consumed
sandbox.envelope.rejected
```

其中 `skill.result.consumed` 只有 Base Agent 后续步骤实际消费执行结果时才产生。

## EvalSmith 可消费的 Base Agent 轨迹字段

Base Agent 在 LangGraph state 中维护 `skill_eval_control`、`skill_selection`、`skill_load_records`、`skill_use_plan`、`skill_execution_records` 和 `skill_consumption_records`，并在最终结果的 `[SKILL_EVAL_RESULT]` 块中整理为 `base_agent.skill_eval_result.v2`。这些字段是评测证据，不是 Langfuse observation attribute：

| JSON 路径 | 含义 | 评测用途 |
|---|---|---|
| `run.eval_run_id` / `run.mode` / `run.status` | 评测运行标识、`auto_select`/`forced_skill`/`without_skill`、运行状态 | 关联 EvalSmith case/run，区分评测模式 |
| `skill.candidate_snapshot` | `source`、`snapshot_id`、候选 `count`、`hash` | 候选集覆盖与版本快照归因 |
| `skill.selection` | `status`、`selection_control`、`selection_skipped`、`decision_id`、`selection_receipt_id`、`selected_skill_refs` | 判断是否选择、选择是否来自允许集合 |
| `skill.loaded[]` | 已加载 Skill 的 `name`、`space_id`、`skill_id`、`version_id`、`version`、`source` | 判断是否可加载及身份是否正确 |
| `skill.load_errors[]` | 加载失败类型、请求 Skill 身份 | 区分加载失败与未选择 |
| `skill.uses[]` | `use_id`、`skill_refs`、`path`（`instruction`/`execute`）、`reason_code`、`load.status`、`instructions_injected` | 判断 Skill 是仅提供指令还是实际执行 |
| `skill.uses[].execution` | `status`、`selected_ref_verification`、结果/产物摘要、错误类型 | 判断执行成功、失败、跳过及身份校验 |
| `skill.uses[].result_consumed` | `status`（`verified`/`observed`/`not_observed`/`not_evaluable`/`not_applicable`）、`skill_invocation_id`、`agent_step_id`、`evidence` | 判断结果是否真正进入后续 Agent 步骤 |

工具调用的 LangGraph 轨迹可从 `TOOL` observation 及其输入/输出中关联：

| 工具 | 评测事实 |
|---|---|
| `list_skills` | 实际看到的候选 Skill；成功结果形成候选快照来源 |
| `search_skills` | 辅助检索结果；不能单独证明 Skill 可选或被使用 |
| `load_skill` | 请求的 Skill 身份、加载成功/失败、脚本入口信息；成功后才可进入 `skill.loaded`/`skill.uses` |

当前 `_skill_tool_observations()` 只把 `list_skills` 和 `load_skill` 汇总到 `skill_eval_result.v2`；`search_skills` 仍需从原始 LangGraph 消息或 `TOOL` observation 读取。

评测优先级：`skill.candidate_snapshot` → `skill.selection` → `skill.loaded` → `skill.uses.execution` → `skill.uses.result_consumed`。缺少 binding、完整 Skill 身份、invocation 或消费步骤时应判为 `not_evaluable`，不能从最终回答文本推断成功。

## Mapping source

- Langfuse 类型映射：`ksadk/tracing/setup.py` 中 `_LANGFUSE_OBSERVATION_TYPES`；
- Skill child/event 投影：`ksadk/skills/observability.py` 中 `project_skill_events()`；
- Base Agent 相关模型/工具调用：`source/skill_eval_base/agent.py`。

Generic OTLP（Langfuse 主路）发送原始 span；CloudMonitor 才额外补充兼容性的 `langfuse.observation.type` 属性。代码中没有 Langfuse 原生 `skill` observation type。
