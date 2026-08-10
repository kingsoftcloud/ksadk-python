# KsADK Prompt/Context/Memory 影子基线开发进度与对照

> 状态：影子基线阶段（已完成），供后续行为型改造追加与 A/B 比对
> 分支：`feature-prompt-context-optimize`
> 关联文档：[技术实现方案](./prompt-context-memory-implementation.md)、[评测方案](./prompt-context-memory-evaluation-plan.md)、[统一建设方案](./prompt-context-memory-unified-proposal.md)
> 本文用途：记录"已做了什么、基线数据是什么、后续在哪一步对照"，方便追加新 PR 或对比效果时直接引用

## 1. 阶段定位

本文记录的是评测方案第 10 节的阶段 1–2（建立 Baseline + Shadow 对比）的落地情况。当前全部为**影子**：不改 Runner 实际发送的 Prompt/History/Context，不改 compaction 触发，不改 ambient 注入。所有新增都是 additive、只读旁路、env-gated。

影子阶段只回答"架构接入是否正确、观测是否可信、是否完全没有改变旧行为"，不评测回答质量（回答质量留到行为型改造后再对比，现在做不出有效差异）。

## 2. 已完成开发点

### 2.1 数据模型（公开稳定类型，从第一批版本化）

| 模块 | 内容 |
|---|---|
| `ksadk/prompts/models.py` | `PromptSection` / `CompiledPrompt` / `PromptProjectionResult` + `PromptSectionKind` 等 Literal（方案 7.2） |
| `ksadk/prompts/compiler.py` | `PromptCompiler` 确定性编译：排序、换行标准化、merge policy（replace/append/merge_unique/protected）、`protected` 覆盖与 untrusted 信任边界校验、SHA-256 content/section hash、`stable_prefix_hash`（仅 stable section）、section token 统计（方案 7.3/7.4） |
| `ksadk/prompts/sources.py` | section builders（platform_safety/agent_identity/agent_policy/resource_manifest/request_instructions）+ `discover_instruction_files`（父→子确定性发现、真实路径去重、单文件/总预算，`KSADK_PROMPT_AUTO_DISCOVERY` 默认关）（方案 7.6） |
| `ksadk/context_engine/models.py` | `ContextItem` / `ContextBudget` / `ContextPlan` / `ContextDecision` + `ContextKind`（方案 8.1/8.2/8.8），`policy_version=v1` |
| `ksadk/context_engine/capabilities.py` | `ContextCapabilities` dataclass + 三种 integration mode + 精度枚举 + 显式 registry 分派 + `capabilities_for_runner` / `capabilities_for_runtime_type` / `capability_hash`（方案 6.1） |
| `ksadk/context_engine/tokenizer.py` | `TokenCounter` Protocol + `HeuristicTokenCounter`（复用 `estimate_text_tokens`），记录所用 tokenizer 名（方案 8.9） |
| `ksadk/context_engine/projection.py` | `ProjectionResult` 语义信封（方案 6.3/8.8） |
| `ksadk/context_engine/cache_observability.py` | `CacheBreakDiagnosis` + `diagnose_cache_break`（expected/unexpected/cached/no_cache_info/opaque，兼容 OpenAI 与 Anthropic cache 字段）+ 进程内 best-effort `CacheBreakRegistry`（方案 7.5） |
| `ksadk/context_engine/shadow_plan.py` | `build_shadow_context_plan_dict` / `minimal_shadow_context_plan_dict`：旁路构造 ContextPlan dict（tokens_by_kind + ownership + prompt hash + runtime_type + capability_hash） |
| `ksadk/context_engine/baseline.py` | `BaselineCollector` + `BaselineTurnRecord` + env-gated 单例 + atexit JSONL dump + `summary()` Scorecard（方案 §10/§12.2） |

### 2.2 接线修正（capability 合同上移到平台边界）

- capability 主合同放在 `RuntimeAdapter` / `BaseRuntime`（`ksadk/runtime/adapter.py`），`RunnerRuntimeAdapter` / `_RunnerAsBaseRuntime`（`ksadk/runtime/runner_adapter.py`）按 `runtime_type` 显式分派，`CodexRuntimeAdapter` 声明 native ownership。
- canonical conversation execution（`ksadk/runtime/conversation_execution.py`）把 `launch_context.runtime_type` 传给 `build_run_input`，使 build_run_input 阶段（尚未拿到 adapter）也能取得正确 ownership，**不再落成默认 opaque**。
- `build_run_input`（`ksadk/conversations/runtime_preparation.py`）新增 `runner` / `runtime_type` 可选参数，capability 解析顺序：runner → runtime_type → DEFAULT。
- fallback preprocessing（`ksadk/runtime/preprocessing.py`）复用 `prepared_turn`（asdict roundtrip 已验证保留 shadow_context_plan），**单 Turn 只一份初始 Plan**，不重复规划。

### 2.3 shadow ContextPlan 挂载（只读旁路）

- `PreparedConversationTurn.shadow_context_plan: dict | None`（`ksadk/conversations/runtime_payloads.py`），三处 return（normal / approval resume / checkpoint resume）挂载 shadow plan。
- `runtime_observability._set_context_plan_attributes` + `_set_prompt_cache_attributes`（`ksadk/conversations/runtime_observability.py`）把 `context.plan_id` / `tokens_by_kind` / `accounting_accuracy` / `runtime_type` / `capability_hash` + `prompt.content_hash` / `prompt.stable_prefix_hash` / `prompt.cache.*` 诊断写到 conversation span。
- 两个调用点：`invoke_conversation_once`（非流式）与 `_iter_conversation_turn_events`（流式）+ canonical `iter_runtime_conversation_events`（terminal 事件后）。

### 2.4 env-gated 基线采集

- env `KSADK_BASELINE_COLLECT=1` 开启，`KSADK_BASELINE_PATH` 指定落盘路径（默认 `/tmp/ksadk-context-baseline.jsonl`）。
- 三个主链路旁路：非流式 `invoke_conversation_once`、流式 `_iter_conversation_turn_events`、canonical `iter_runtime_conversation_events`。
- atexit 自动 dump JSONL（每行一条 turn + 末尾 `__summary__`）。
- 只记 hash/计数/分类，**不含 Prompt/凭证正文**；采集异常被吞，绝不影响主链路。

### 2.5 验收测试（118 个，全部通过）

| 文件 | 覆盖 |
|---|---|
| `tests/prompts/test_prompt_models.py` | frozen/hash/默认值 |
| `tests/prompts/test_prompt_compiler.py` | 确定性/排序/merge policy/protected 覆盖/稳定前缀/换行标准化 |
| `tests/prompts/test_prompt_sources.py` | section builders + 指令文件发现（flag 默认关/预算截断） |
| `tests/context_engine/test_capabilities.py` | capability 字段/DEFAULT 保守/runtime_type dispatch/capability_hash 稳定 |
| `tests/context_engine/test_token_counter.py` | heuristic 与 estimate_text_tokens 一致/CJK-ASCII/空串 |
| `tests/context_engine/test_cache_observability.py` | expected/unexpected/cached/no_cache_info/opaque |
| `tests/context_engine/test_shadow_plan.py` | tokens_by_kind 累加/ambient/ownership 对齐/prompt hash |
| `tests/context_engine/test_shadow_plan_integration.py` | 真实 InMemorySessionService 跑 build_run_input + span 挂载 |
| `tests/context_engine/test_runner_conformance.py` | 各 Runner `describe_context_capabilities` 与 ownership 一致 + native 隔离 invariant |
| `tests/context_engine/test_adapter_context_capability.py` | RuntimeAdapter 合同 + canonical runtime_type 与 adapter hash 一致 |
| `tests/context_engine/test_shadow_baseline_acceptance.py` | 基线验收：旧行为不变/单 Plan/Token 分类 property/Trace 安全/native 隔离 |
| `tests/context_engine/test_baseline_collector.py` / `_wiring.py` / `_runtime_wiring.py` | 采集器单测 + env-gated 开关 + canonical 路径采集 + 安全脱敏 |
| `tests/runtime/test_conversation_execution.py`（追加） | canonical 路径单 Plan + ownership 非 opaque + resume 不重复 Plan |

## 3. 基线采集结果

### 3.1 离线 Fixture 基线（`scripts/collect_context_baseline.py`）

5 个固定 Case（对齐评测方案 §7/§13 落地顺序），用真实 `build_run_input` + `InMemorySessionService` 生成真实 shadow plan，runtime usage/compaction/PTL 用确定性 Fixture 信号模拟（标注 `execution_target=local-shadow-sim`）。

```bash
uv run python scripts/collect_context_baseline.py --out /tmp/ksadk-context-baseline.jsonl
```

### 3.2 真实模型基线（`scripts/collect_real_model_baseline.py`）

用真实模型（minimax openai 兼容端点，凭证来自 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL`）经最小自定义 `BaseRunner`（detection_type=langgraph → capability estimated）+ `ChatOpenAI`，通过真实 `invoke_conversation_once` 触发 baseline 旁路。凭证缺失时 exit code 2 + 列出缺失 env（不假装能跑）。

```bash
KSADK_BASELINE_COLLECT=1 KSADK_BASELINE_PATH=/tmp/ksadk-real-model-baseline.jsonl \
  .venv/bin/python scripts/collect_real_model_baseline.py
```

### 3.3 真实基线数据（2026-08-06 采集，3 个真实 turn）

| 指标 | 值 |
|---|---|
| turn_count | 3 |
| runner_type / accounting_accuracy | langgraph / estimated（全部） |
| runtime_reported input tokens | mean=205, median=198, p95=229 |
| runtime output tokens | ~133 |
| cache_read_tokens | 128（minimax 真实命中缓存） |
| planned (heuristic estimate) | mean=43, p95=76 |
| turn_latency | mean≈2.2s, p95≈3.3s |
| PTL rate / compaction / opaque request rate / capability_mismatch / unexpected_cache_break | 全 0 |
| stable_prefix_hash_changes | 0 |

**单条记录字段**（落盘 JSONL 每行）：`ksadk_commit` / `ksadk_version` / `context_policy_version` / `runner_type` / `integration_mode` / `accounting_accuracy` / `capability_hash` / `model` / `execution_target` / `plan_id` / `prompt_content_hash` / `prompt_stable_prefix_hash` / `tokenizer` / `tokens_by_kind` / `prompt_tokens_by_section` / `planned_input_tokens` / `runtime_reported_input_tokens` / `runtime_output_tokens` / `cache_read_tokens` / `cache_creation_tokens` / `cache_status` / `unexpected_break` / `compaction_triggered` / `compaction_trigger` / `prompt_too_long` / `retry_attempts` / `turn_latency_ms` / `capability_mismatch` / `session_id` / `invocation_id` / `recorded_at`。

### 3.4 基线如实暴露的缺口（不是 bug，是后续 PR 要解决的）

1. **启发式估算偏差大**：planned mean=43 vs 真实 205，约 4.7×。方案 §8.9 要求接入 provider tokenizer + 启用 10% 安全系数；当前只有 heuristic。
2. **stable_prefix_hash 为空**：当前只编译 volatile `request_instructions`（shadow 不虚构未发送的 platform_safety），故 cache-break 诊断标 `no_cache_info`，不编造命中率。待 Prompt Compiler 接管 stable section 后才有可诊断稳定前缀。
3. **PTL/compaction 基准为 0**：3 个短 turn 未触发；需扩展到长会话/大 tool result 才能拿到非零 PTL/compaction 基准。
4. **cache_status 在 baseline collector 为空**：完整诊断在 trace span（`prompt.cache.*`），collector 只记 raw cache tokens（避免与 span 路径共享 registry 的记录顺序污染）。

## 4. 提交记录

| commit | 主题 |
|---|---|
| `18c52b5` | feat(context_engine): add shadow Prompt/Context observability baseline |
| `2197dc1` | chore(baseline): add real-model baseline collection script |
| `e4393e0` | fix(baseline): don't double-record cache-break registry from baseline collector |

文档与架构图（`docs/prompt-context-memory-*.md`、`docs/architecture-diagrams/`）按要求**未提交**，保留在工作区未跟踪状态。

## 5. 后续 A/B 比对方法

每个行为型 PR 都与本文 3.3 的真实基线对比（评测方案 §10 阶段 3）：

```text
1. 在同一 commit 基线（本文记录的真实基线）与 Candidate commit 上分别跑
   scripts/collect_real_model_baseline.py（固定相同 Fixture/模型/温度）
2. 比对 BaselineTurnRecord 的关键字段：
   - planned_input_tokens（启发式 vs 接管后精确 tokenizer）
   - runtime_reported_input_tokens（A/B 一致性，除非行为变化）
   - tokens_by_kind 各分项（Prompt/History/Memory/Tool 分类是否更准）
   - prompt_content_hash / stable_prefix_hash（Prompt 是否漂移）
   - cache_read_tokens / cache_status（稳定前缀命中率是否提升）
   - PTL rate / compaction_count / retry_attempts（降载效果）
   - turn_latency_ms（P95 无不可解释回归）
   - capability_mismatch / opaque_request_rate（应为 0）
3. 硬断言（评测方案 §7.6 / §11）必须全部通过才能进灰度
```

## 6. 明确不做（边界，留后续行为型 PR）

按方案 §21 与评测方案 §13 的顺序：

```text
Prompt Compiler 接管发送（先 shadow 已做，切换留 PR4）
  ↓
Hosted Context Assembler + Tool Result 预算
  ↓
Working State + Compaction（双阈值、Memory Flush）
  ↓
Memory Candidate + Provider v2
  ↓
Contributor 接管真实来源
```

每一步都与本文基线对比；memory 错误串注入修复（`_ambient_context_has_error` 拦截「未找到…」+ `search_text` 异常不返回错误文本）单独 PR。

## 7. 当前局限（诚实标注）

- 真实基线只覆盖 langgraph 路径（estimated）；ADK/Codex 路径需装对应 extra + 真实 agent，未采。
- 真实模型为 minimax openai 兼容端点，非生产模型目录；model_metadata 走默认 context_window=200k。
- Studio build→run 路径（`studio/service.run_build` 经 `RuntimeExecutor`）会触发 canonical `iter_runtime_conversation_events` 旁路，但未用 Studio HTTP 入口实测过；`invoke_conversation_once` 直跑已验证。
- shadow 阶段不改行为，故 runtime_reported token/latency 在行为型改造前不会有有意义变化——这正是基线的意义（建立"不变"的基准）。
