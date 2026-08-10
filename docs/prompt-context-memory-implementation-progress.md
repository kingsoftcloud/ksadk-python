# KsADK Prompt、Context 与 Memory 技术实现 —— 阶段性完成进度

> 关联文档：[技术实现方案](./prompt-context-memory-implementation.md)、[统一建设方案](./prompt-context-memory-unified-proposal.md)、[基线进度](./prompt-context-memory-baseline-progress.md)
> 分支：`feature-prompt-context-optimize`
> 本文档用途：记录本轮"完善剩余实现"每个模块的完成情况、验证证据和已知缺口。

## 总体进度

按技术实现方案 §16 的 Phase 划分估算（按工作量而非时间）：

| Phase | 内容 | 状态 |
|---|---|---|
| Phase 0 | 基线与可观测 | ✅ 完成（本轮补齐独立 `DeploymentMode` 二维字段；真实样本缺失条件已列） |
| Phase 1 | 数据模型与 Prompt Compiler | ✅ 完成（本轮补齐 `PromptProjectionResult` 投影、capability mismatch 熔断、provider tokenizer opt-in） |
| Phase 2 | ContextPlan 与预算 | ✅ 完成（本轮新增 `planner.py` / `assembler.py` / `policies.py` 与分区预算决策；接管启用留灰度开关） |
| Phase 3 | 有序降载与 Working State | ✅ 完成（本轮新增 Memory Flush、摘要 v2 解析、per-session lock；有序降载链已完整） |
| Phase 4 | Memory Candidate 与 Provider v2 | ✅ 完成（本轮全部新增 + `search_text()` 错误隔离修复） |
| Phase 5 | Contributor 接管与灰度 | ✅ 接口与首批 Contributor 完成（shadow）；逐 Runner 灰度启用留后续 |

纪律：所有行为型改动默认关闭 / shadow，不改变现有 Runner 输入；每块配真实验证测试。

## 本轮完成清单（按提交顺序）

### 1. Memory v2 数据模型与 Provider（Phase 4）

- [x] `ksadk/memory/models.py` — MemoryRecord / MemoryCandidate / MemorySearchRequest / MemorySearchResult / MemoryScope / MemoryType / MemoryStatus / MemoryCapabilities / CoreMemoryBlock / CoreMemoryRequest / MemoryDeleteRequest/Result
- [x] `ksadk/memory/provider.py` — MemoryProvider Protocol
- [x] `ksadk/memory/policy.py` — 写入策略、敏感信息拒绝（正则+显式标签）、阈值、冲突 supersede、content_hash
- [x] `ksadk/memory/providers/__init__.py` + `local_sqlite.py` — SQLite Provider（scope 隔离、乐观锁、TTL、hard/soft delete、content_hash 去重、max_tokens 装箱）
- [x] `ksadk/memory/coordinator.py` — core/recall/flush/commit 编排、`build_search_request`、`recall_to_context_item`（失败不注入噪声）
- [x] `ksadk/memory/service.py` — `search_text()` 错误隔离修复（§10.8：异常返空串而非错误文本），并同步更新 `tests/unit/memory/test_recall_failure_semantics.py` 中锁定旧错误行为的测试（原测试注释"不在本 PR 范围"，本 PR 按方案 §10.8 收口）
- [x] tests: `tests/memory/test_memory_v2_contract.py` —— **22 passed**

验证：`.venv/bin/python -m pytest tests/memory/test_memory_v2_contract.py` → 22 passed。
覆盖：upsert/get/search、scope 隔离、expected_version 冲突、superseded 不召回、hard/soft delete、content_hash 去重、空 scope 空返回、api_key/cookie/dsn/token 敏感拒绝、显式/隐式阈值、观察次数、model_guess 拒绝、delete 无目标拒绝、冲突 supersede、flush 拒绝、recall 失败不注入、hard delete 不支持返回 False。

### 2. Context Engine 核心规划与组装（Phase 2）

- [x] `ksadk/context_engine/policies.py` — ContextPolicy / PromptPolicy / ContextBudgetPolicy / CompactionPolicy / ToolResultPolicy / ContributorPolicy / MemoryPolicyConfig + `compute_budget_tokens` + 旧 env 映射（KSADK_LTM_BACKEND → provider 等）
- [x] `ksadk/context_engine/planner.py` — `ContextPlanner`：required 锁定 + group 原子拉入、§8.4 优先级排序、分区预算、确定性缩减（dedupe manifest → 大 tool result 转摘要 → 旧 tool 去重 → 冷轮次 drop → recall 降 top_k）、emergency reduce、`build_budget`
- [x] `ksadk/context_engine/assembler.py` — `ContextAssembler`：Chat messages / Responses items 投影（system 前缀优先、tool_result → function_call_output）
- [x] `__init__.py` 导出新类型
- [x] tests: `tests/context_engine/test_planner.py` —— **13 passed**（含 50 轮 property-based 无孤儿 group invariant）

验证：`.venv/bin/python -m pytest tests/context_engine/test_planner.py` → 13 passed；全量 prompts+context_engine+memory+runtime_input → 181 passed。
覆盖：required 优先、group 原子性（tool pair、approval）、emergency 整组丢不孤儿、分区预算、dedupe、大 tool result 摘要、冷轮次 drop、recall 降 top_k、budget 计算、chat/responses 投影、`overwritten_by` 旧 tool 去重。

### 3. Context Contributor（Phase 5）

- [x] `ksadk/context_engine/contributors.py` — `ContextContributor` 基类、`ContributorCapabilities`（trust/timeout/max_tokens/failure_mode/cacheability）、`ContextContributionRequest`、`run_contributors`（并发 + 超时 + failure policy）、首批内置 Contributor（WorkspaceRules / MemoryRecall / SkillManifest）
- [x] tests: `tests/context_engine/test_contributors.py` —— **6 passed**

验证：6 passed。覆盖：并发执行、超时返空、failure_mode=skip/warn/fail、SkillManifest→resource_manifest、trust_level 永不 platform、不得自行声明 required。

### 4. Phase 0/1 收尾

- [x] 独立 `DeploymentMode` Literal（`context_engine/capabilities.py`）+ `RuntimeLaunchContext.deployment_mode`（默认 `local`，非法值回退）+ 写入 shadow_plan/baseline/trace span；二维字段正交，不互相推断
- [x] `ksadk/prompts/projection.py` — `project_compiled_prompt`（填充 `PromptProjectionResult`：projected_roles 按 integration_mode、section_hashes 取自编译、安全优先级校验）+ `project_to_runner_payload`（base_instructions/instruction/system_message）
- [x] capability mismatch 检测与熔断：`detect_capability_mismatch`（owner/token_accounting/duplicate_history/double_compaction）+ `mark_capability_mismatch`/`is_capability_circuit_open`/`reset_capability_circuit`（按 runtime_type 隔离，进程内 best-effort，不影响 shadow 与正常执行）
- [x] TokenCounter provider tokenizer：`_TiktokenTokenCounter`（tiktoken 优先，不可用回退 heuristic），`KSADK_TOKENIZER_PROVIDER=auto|tiktoken` opt-in，默认保持 heuristic baseline 不变
- [x] tests: `tests/context_engine/test_deployment_mode.py`（6）+ `test_phase01_finishing.py`（11）—— **17 passed**

验证：17 passed；全量 204 passed（prompts+context_engine+memory+runtime_input）+ `tests/runtime/test_conversation_execution.py` 5 passed。
覆盖：deployment_mode 默认/传播/正交（Managed Codex 不被误判 Harness）、RuntimeLaunchContext 校验、PromptProjection 各 integration_mode roles、mismatch owner/double_compaction 检测、熔断按 runtime_type 隔离、tokenizer opt-in 不改默认。

### 5. Phase 3 有序降载完善

- [x] `ksadk/memory/extraction.py` — `MemoryExtractor` + `propose_memory_candidates`（确定性提取：显式"记住/remember"→profile、工具确认事实→fact；无 LLM）
- [x] Memory Flush 接入 compaction 链路：`_maybe_memory_flush`（`ksadk_conversations/runtime_compaction.py`），门控 `KSADK_MEMORY_FLUSH_ENABLED`（默认关）+ policy，失败不阻止 compaction，审计写 checkpoint `memory_flush`
- [x] 摘要 v2 文本解析：`_parse_summary_v2_sections`（semantic_summary.py）解析 next_action/decisions/errors_and_corrections（中英标记，确定性，无 LLM），填进 `WorkingState`
- [x] 有序降载链已完整：per-tool result budget（`tools/result_budget.py`，工具执行时）→ snip → microcompact（compaction_pipeline）→ semantic compact → PTL force compact + retry once（`runtime_invocation.py` `range(2)` = 1 retry，方案 §9.1）
- [x] per-session lock + stale guard：`ksadk/conversations/session_lock.py` `session_compaction_lock`（进程内 per-session async lock，超时放弃避免并发覆盖），接入 `compact_conversation_history` checkpoint 提交
- [x] tests: `tests/conversations/test_phase3_memory_flush_summary.py`（12）+ `test_session_lock.py`（3）—— **15 passed**

验证：15 passed；`tests/conversations/` 全量 53 passed（含既有双阈值/WorkingState 测试无回归）。
覆盖：显式记忆抽取（中英）、工具事实抽取、普通消息无候选、摘要 v2 next_action/decisions/errors 解析、Memory Flush 默认关/开启/secret 拒绝、per-session lock 串行/隔离/超时 stale guard。

## 验证记录

每块完成后以 `.venv/bin/python -m pytest` 真实跑过。不能跑的场景（真实 ADK/Codex 凭证、云端预发）明确列出缺失条件。

### 真实跑过的测试（汇总）

| 套件 | 命令 | 结果 |
|---|---|---|
| prompts + context_engine + memory + runtime_input | `pytest tests/prompts/ tests/context_engine/ tests/memory/ tests/conversations/test_runtime_input_prompt_compiler.py` | 204 passed |
| conversations 全量 | `pytest tests/conversations/` | 53 passed |
| runtime/runners | `pytest tests/runtime/ tests/runners/` | 125 passed, 1 skipped |
| integration E2E | `pytest tests/integration/test_context_e2e.py` | 2 passed |
| 本次新增全部 | prompts/context_engine/memory/conversations/integration + runtime conversation_execution + unit/memory + platform_memory + runtime_common_memory | 375 passed |

### 已知未覆盖（明确缺失条件，非"应该能跑"）

1. **真实 ADK/Codex/云端预发样本**：Phase 0 基线只覆盖 langgraph/minimax 离线路径；ADK/Codex 需安装对应 extra + 真实 agent + 凭证，云端预发需 agentengine-server 注册与 template id，本轮无法在本机跑。
2. **`test_codex_api` 3 个失败 + `test_unified_agent_ui_local` 10 个失败**：经 `git stash` 在干净 HEAD 验证为**预先存在**的失败（`sharedChat` feature flag 默认值 / web server + 本地静态 UI + 环境依赖），与本次 Prompt/Context/Memory 改动无关。
3. **真实 Memory Provider**：Memory Flush 当前使用可配置文件路径的 SQLite Provider；本地和单副本 PVC canary 可验证持久化，生产多副本仍应接入 `LongTermMemoryService`/HTTP/SDK Provider，需凭证与 endpoint。
4. **tiktoken 精确 tokenizer**：`KSADK_TOKENIZER_PROVIDER=auto|tiktoken` 可启用，默认保持 heuristic baseline；精确偏差监控需真实 model usage 对比。

## 总结

本轮按技术实现方案 §16 的 Phase 0–5 完善了剩余实现：

- **Phase 0/1 收尾**：独立 `DeploymentMode` 二维字段、`PromptProjectionResult` 投影逻辑、capability mismatch 检测与熔断、provider tokenizer（opt-in）。
- **Phase 2**：`policies.py`（ContextPolicy 归一化）、`planner.py`（预算 + group 原子性 + 确定性缩减，含 50 轮 property-based 无孤儿 group invariant）、`assembler.py`（Chat/Responses 投影）。
- **Phase 3**：Memory Flush 接入 compaction、摘要 v2 文本解析、有序降载链确认完整、per-session lock + stale guard。
- **Phase 4**：Memory v2 全套（models/provider/policy/providers/local_sqlite/coordinator/extraction）+ `search_text()` 错误隔离修复。
- **Phase 5**：`contributors.py`（Protocol + 并发/超时/信任约束 + WorkspaceRules/MemoryRecall/SkillManifest）。

## 第二轮：真实链路接线（接成一条链路）

按"先观测、后接管"已建好模块，本轮把它们接进 canonical 调用链，形成真实链路：

```
Resolved AgentVersion → Prompt Compiler → Contributors → Context Planner →
Context Assembler → RuntimeAdapter → 模型 → Usage/Trace → Session/Memory
```

- [x] `ksadk/context_engine/hosted_pipeline.py` — `run_hosted_pipeline`：从 PreparedConversationTurn 取 compiled_prompt/history/user_input/working_state → 运行 Contributors → ContextPlanner → ContextAssembler，产出真实 `(ContextPlan, AssembledInput)`；`assembled_to_payload` 投影成 runner 的 instructions/input/history
- [x] `ksadk/conversations/runtime_payloads.py` — `PreparedConversationTurn` 新增 `context_plan` / `assembled_input` 字段
- [x] `ksadk/conversations/runtime_preparation.py` — `build_run_input` 构造 prepared 后调用 `_maybe_fill_hosted_pipeline` 回填真实 plan/assembly（门控：`KSADK_CONTEXT_ENGINE_V2_ENABLED` × `prompt_integration_mode=ksadk_hosted` × prompt_owner=ksadk；native_runtime 不进入）
- [x] `ksadk/conversations/runtime_input.py` — `_should_use_hosted_assembly` + assembled payload 接管（优先于 PR B/D2，避免双重注入；codex 的 assembled_input 恒 None → 不受影响）
- [x] `ksadk/runtime/conversation_execution.py` — `_finalize_hosted_turn`：usage 回填进 `context_plan.runtime_reported_input_tokens` + 压缩后 Memory Candidate 抽取/flush（门控 `KSADK_MEMORY_FLUSH_ENABLED`，best-effort 不阻断）
- [x] 真实 LangGraph 本地 E2E：`tests/integration/test_hosted_chain_e2e.py` —— **5 passed**（含真实 `StateGraph` + FakeLLM 经 `invoke_conversation_once` canonical 路径；验证 agent_system/task 进模型 system、安全规则不被覆盖、usage 回填、Session append-only、默认关闭字节级一致、Managed Codex 不被接管）
- [x] 云端预发 E2E 占位：`tests/integration/test_cloud_managed_e2e.py` —— 2 skipped（明确列出缺失条件：agentengine endpoint / AgentVersion / template id / Memory endpoint / deployment_mode，不假装能跑）

验证：`.venv/bin/python -m pytest tests/integration/` → 7 passed, 2 skipped；全量 prompts+context_engine+memory+conversations+integration+runtime+runners+unit memory → **500 passed, 1 skipped**。

### 完成度更新

接成真实链路后，整体完成度从约 65% 提升至约 75%：
- 设计方案 ~95%（未变）
- 核心代码模块 ~65% → ~80%（模块已全部接进 canonical 链路，hosted 路径可端到端跑通）
- 真实 Runtime 接管 + 云端可灰度 40% → ~55%（hosted 链路本地 E2E 已验证；云端预发 E2E 待真实部署条件；逐 Runner 灰度启用与旧路径清理待后续 PR）

### 仍需后续（不在本轮）

1. **云端预发 E2E**：需 agentengine-server 真实注册 AgentVersion + Runtime template + 云端 Memory Service + 凭证（本机缺失，已占位 skip）。
2. **逐 Runner 灰度 + 旧路径清理**：hosted 链路默认关闭，按 Agent/dev/预发灰度，稳定后清理 PR B 旧分支（方案 §14.2 发布策略 1-6）。
3. **真实 Memory Provider 接入**：当前 Memory Flush 用临时 SQLite Provider；生产应接 `LongTermMemoryService`/HTTP/SDK Provider。
4. **LangGraph 长会话 E2E**：触发 proactive/emergency compaction + WorkingState 重注入的完整长链路 E2E（现有 `test_dual_threshold_compaction.py` 覆盖阈值，但未跑 hosted assembler 全链路）。

纪律遵守：所有行为型改动默认关闭 / shadow / opt-in（`KSADK_PROMPT_COMPILER_ENABLED`、`KSADK_CONTEXT_ENGINE_V2_ENABLED`、`KSADK_MEMORY_FLUSH_ENABLED`、`KSADK_TOKENIZER_PROVIDER`、`prompt_integration_mode=ksadk_hosted` 等），不改变现有 Runner 输入；新增模块均配单测，端到端集成测试验证安全规则不可被 Memory/Tool 覆盖。

## 第三轮：四项接入主链路（接成真实链路 v2）

按"剩余工作不再是增加数据模型，而是接入主链路 + 跑通 Conformance"，完成四项：

### 1. Runtime 真正注入 Contributor

- [x] `ksadk/context_engine/hosted_pipeline.py` 新增 `default_hosted_contributors`：按 policy 构造 MemoryRecallContributor（`KSADK_MEMORY_ENABLED` 门控）+ WorkspaceRulesContributor（`KSADK_PROMPT_AUTO_DISCOVERY` 门控），Provider 构造失败不阻断
- [x] `_maybe_fill_hosted_pipeline`（runtime_preparation）调用 `default_hosted_contributors` 并传入 `run_hosted_pipeline`——Contributor 首次在真实 hosted 链路运行
- [x] tests: `test_main_chain_integration.py::test_hosted_chain_runs_memory_recall_contributor`（预存记忆经 hosted 链路召回）+ `test_default_hosted_contributors_returns_empty_when_memory_disabled`

### 2. 持久化 Memory Provider 替换临时 :memory:

- [x] `ksadk/memory/providers/local_sqlite.py` 新增 `resolve_default_memory_provider` + `_resolve_default_db_path`：优先级 `KSADK_MEMORY_DB_PATH` env > 本地 session dir `memory.db` > `:memory:`（回退）
- [x] 替换 `runtime/conversation_execution.py`（`_finalize_hosted_turn`）的 `SqliteMemoryProvider()` → `resolve_default_memory_provider()`
- [x] 替换 `conversations/runtime_compaction.py`（`_maybe_memory_flush`）的 `SqliteMemoryProvider()` → `resolve_default_memory_provider()`
- [x] tests: `test_persistent_memory_provider_survives_reopen`（重开同库数据保留）+ `test_resolve_default_db_path_env_override`

### 3. capability mismatch 接入 Adapter 熔断

- [x] `ksadk/context_engine/capabilities.py` 新增 `CapabilityCircuitOpen` + `assert_capability_not_circuit_open`（门禁，熔断时抛）
- [x] `_maybe_fill_hosted_pipeline` 接入门禁：熔断时回退旧路径（不接管 instructions）
- [x] `runtime/conversation_execution.py` 新增 `_maybe_detect_capability_mismatch`：turn 收尾时据 usage 证据检测（声明 runtime_reported 但无 usage → 熔断），证据驱动、不阻断主链路
- [x] tests: `test_circuit_breaker_gates_hosted_pipeline`、`test_evidence_driven_mismatch_triggers_circuit`、`test_mismatch_does_not_circuit_when_usage_present`、`test_assert_capability_not_circuit_open_raises_when_circuit_open`、`test_circuit_breaker_isolates_by_runtime_type`

### 4. LangGraph 云端预发 + 真实 Codex/ADK Conformance

- [x] 真实 Codex Conformance：`test_codex_adk_conformance.py` 用真实 `CodexRuntimeAdapter` + streaming fake client 跑完整 start→stream→close，断言 native_runtime ownership、base_instructions 进 thread、history 不二次注入、hosted pipeline 不接管 native（PCM-RUNNER-003）
- [x] ADK Conformance：`framework_assisted` ownership、history/compaction owner=framework、native_skills=True、hosted pipeline 跳过 framework_assisted
- [x] 未知 Runner 保守 opaque 默认
- [x] 云端预发 E2E：真实跨部署 E2E 仍 skip（缺 agentengine/template/凭证）；新增本机可验证的本地侧一致性合同：canonical hash 跨两次 build 确定一致（PCM-DEPLOY-001 本地侧）+ deployment_mode 变化不静默改变 ownership（PCM-DEPLOY-002）

验证：`.venv/bin/python -m pytest tests/integration/` → **23 passed, 2 skipped**；全量确定性 → **516 passed, 3 skipped**。

### 完成度更新（达成 ~85%）

四项接入后，整体完成度从 ~70% 提升至约 **85%**：
- 设计方案 ~95%（未变）
- 核心代码模块 ~80% → **~90%**（Contributor/持久 Provider/熔断全部接入主链路）
- 真实 Runtime 接管 + 云端可灰度 ~55% → **~80%**（hosted 本地 E2E + Codex/ADK Conformance 跑通；云端跨部署 E2E 待真实凭证）

### 仍需后续（云端真实部署）

1. 真实跨部署云端预发 E2E：需 agentengine-server 预发 + Runtime template + 云端 Memory + 凭证（本机缺失，已占位 skip + 明确条件）
2. 逐 Runner 灰度启用 + PR B 旧路径清理（方案 §14.2）
3. 生产 Memory Provider 接入 `LongTermMemoryService`/HTTP/SDK（当前 SQLite 文件库，可本地验证）

纪律遵守：所有行为型改动默认关闭 / shadow / opt-in；新增均配单测与集成测试；云端不可跑场景明确 skip 并列缺失条件，不假装能跑。

## 第四轮：标准 Code 部署前收口

- [x] 标准 canonical runtime 从 `agentengine.yaml.context` 解析 Build 级 Prompt 合同，传递 `agent_system` / `agent_task` / `prompt_integration_mode`；不再只有 Studio 路径能启用 hosted pipeline。
- [x] Serverless 部署默认注入 `KSADK_DEPLOYMENT_MODE=ksadk_managed_cloud`，并保留调用方显式 override。
- [x] 每轮结束和 compaction 前的 Memory Candidate 按 `user_id` 写入，修正之前按 `session_id`/空 scope id 导致无法跨 Session 召回的问题。
- [x] baseline collector 支持 `KSADK_BASELINE_FLUSH_EACH_TURN=true`，云端长驻进程每轮原子更新 JSONL，不依赖 `atexit`。
- [x] 新增[云端预发验证手册](./prompt-context-memory-cloud-validation.md)，列明配置、部署、Case、证据与回退。

验证：`.venv/bin/python -m pytest -q tests/prompts tests/context_engine tests/memory tests/conversations tests/runtime tests/runners tests/integration tests/unit/memory tests/test_deploy_integration.py` → **536 passed, 3 skipped**。其中云端真实 E2E 仍按缺失条件 skip，未把本地合同测试冒充为云端通过。

## 第五轮：真实云端预发验证（2026-08-07）

- [x] 使用当前工作区源码构建并部署 `ksadk-pcm-canary`（Serverless Code，
  `cn-beijing-6`，单副本），Agent ID `ar-20260807141359-21a82339`；控制面后来已收敛为
  `RUNNING`、1/1 Ready。后续更新期间控制面 GetAgent/Deploy 连续返回 502，但既有数据面仍正常。
- [x] 基本调用：返回 `PCM-CANARY-OK`。
- [x] 同 Session 连贯性：代号“白鹭”在第二轮正确恢复，当前输入不再作为旧 history 重复投影。
- [x] 跨 Session Memory：显式“记住”写入 user scope；SQLite 直接召回 1 条；新 Session
  最终回答“先 dry-run”。
- [x] 修复 Code/Container 入口丢失 Build Context：生成的 `entrypoint.py` 现在把
  `agentengine.yaml` 解析结果固化到 `RuntimeLaunchContext.config`，云端不再静默丢失
  `prompt_ownership`、agent system/task 和 integration mode。
- [x] 修复默认 `MemoryRecallContributor` 漏导入导致的静默跳过，并补默认 Contributor 回归测试。
- [x] 修复 SQLite 中文自然语言查询的空格分词假设，增加有限 CJK 二元词组召回。

本轮定向验证：Context/Memory/Runtime/Builder 相关测试分别完成 318、43、37、49 个通过批次；
最后一批 Builder + Runtime + Context + Memory 回归为 **49 passed**，相关 Builder 文件 Ruff
通过，`git diff --check` 通过。批次有测试重叠，因此不能把这些数字相加当作唯一用例总数。
最终 fresh verification 为 **547 passed, 3 skipped**；3 个 skip 均保留其外部环境条件，未冒充通过。

真实预发继续验证新增一项部分结果：降低 canary soft/hard 阈值后，第 5 轮真实触发 1 次
Compaction，baseline 显示 `last_compacted=true`、`last_trigger=auto`、计划输入 12,228 tokens。
但压缩后没有稳定恢复原“不得操作生产环境”和精确下一步，因此机制触发通过、Working State
连续性行为验收未通过，已形成回归 Bad Case。

真实预发仍有四个明确未完成项：

1. `/home/node/.agentengine/memory.db` 在 Agent 滚动更新后记录归零；当前文件 API 映射的
   workspace 根为 `/app/code/.agentengine/ui/workspace`。需确认 Serverless PVC 的真实挂载与
   复用合同，或直接接生产 `LongTermMemoryService`/HTTP/SDK Provider。
2. 修复并复测 Compaction 后 Working State 的目标、下一步和关键约束恢复。
3. 真实模型 PTL/emergency retry 尚未触发；一次性模拟 Provider PTL 版本因 Deploy 502 未上线。
4. V2 关闭配置已在本地 canary 准备，但控制面连续 502，回退滚动更新尚未执行；需先恢复
   AgentEngine 控制面可用性。现有数据面仍可调用，不应误报为 Agent 宕机。

详细命令、Case 和证据口径见
[云端预发验证手册](./prompt-context-memory-cloud-validation.md#9-2026-08-07-真实预发结果)。

## 第四轮：Studio 产品化与全链路（方案 §5/§6/§8.1）

按《Studio 产品化与全链路技术方案》§10 阶段 0-3 中**能在本仓库独立完成**的部分推进。跨仓
（agentengine-server 控制面、生产 Memory Service）只交付合同字段，不实现服务端。

### PR-S0：修复 Studio 基线 ✅
- 3 个 `test_codex_api` 失败的根因是环境依赖：`openai-codex` 包未装 + `ksadk-web` 静态产物未同步（非代码问题）。
- 装 `openai-codex==0.144.4` + `make sync-ksadk-web-static KSADK_WEB_VERSION=0.3.0` 后全绿。
- Studio 全量：165→**168 passed**（3 个失败全修，2 skip 是长依赖）。

### PR-S0.7：ManifestResolver 多 Runner 导入统一入口 ✅
（见下文）

### PR-S1.5：Framework Manifest 真正导入 ✅
方案 §6.1：根 framework manifest 不再只返回空列表，而是完整导入生成 Studio Draft。
- `service.detect_importable_project()`：检测根 framework manifest，返回待导入信息（runtime/name/model/prompt/task）。
- bootstrap 暴露 `importableProject` 字段，前端可提示用户确认导入。
- `commit_project` 补读 config.model→ModelSpec、config.task→Instructions.task、config.context→ContextSpec、
  config.memory→MemorySpec，**完整保留 Runtime/Prompt/Model/Tool/Context 配置**（方案 §6.1）。
- 测试：`test_framework_import.py` **8 passed**（detect/bootstrap/commit 保留 model/task/context/list 显示）。

### PR-S3.5：PCM 前端从只读升级为可配置 ✅
- `index.html` 加 Context Ownership/Rollout/Memory 表单字段（quickAgentEditorForm）。
- `authoring.js` `prepareQuickCreate` 回填 PCM 值 + `submitQuickCreateAgent` 把 context/memory 写进 spec PUT。
- `pcmUpdateOwnershipChoices(runtimeType)`：capability 限制可选项（codex→native, langgraph→framework/ksadk, adk→framework），
  runtime 变化时收窄（方案 §5.2）；保存 Revision 经 If-Match，Build 固化 policy。
- 重建 `static/app.js`（build-static.mjs）。

### PR-S4.5：Context Inspector 完整页签 ✅
- `pcm.js` `renderRunContextInspector` 升级为 4 页签：Context（planned/projected/actual + prompt hash + 决策表）、
  Memory（memory-events）、Working State（goal/next_action/constraints/pending）、Raw Evidence（脱敏 JSON，方案 §19）。
- 页签切换、精度色标、ownership 标签。

### PR-S5：浏览器 E2E ✅
- `test_pcm_e2e.py` **3 passed**：API 级完整闭环（创建含PCM → 编辑policy → 编译预览 → Context预览 → build → run → inspector 四证据 API）。
- `pcm_browser_smoke.py`：浏览器级 E2E 脚本（需 playwright + studio server，独立运行，验证表单/capability限制/预览/保存联动）。

### PR-S3：Studio PCM 前端 ✅
方案 §7：新增 `ksadk/studio/web/src/app/pcm.js` + `styles/pcm.css`，经 `build-static.mjs` 拼接到
`static/app.js`/`app.css`（纯文件拼接，无需 vite/npm）：
- **Agent Detail Context Policy 区**：展示 ownership/runtimeType/rollout/tokenizer/policyVersion + 预算条。
- **Prompt 编译预览**：调 `POST /agents/{id}/prompt:compile`，展示 section hash/tokens。
- **Context 预览**：调 `POST /agents/{id}/context:preview`，展示 accuracy/预算/decisions/tokensByKind。
- **Run Context Inspector**：调 `GET /runs/{id}/context` + `/prompt`，展示 planned/projected/actual +
  精度色标 + ownership + prompt hash + 决策表。
- 样式全用语义 token（font-size/font-weight），通过 `test_style_system` 校验。
- `renderAgentDetail` 末尾调 `renderPcmPanel`；`renderTraceExplorer` 末尾调 `renderRunContextInspector`。

### PR-S0.7：ManifestResolver 多 Runner 导入统一入口 ✅
方案 §2.4 第 1 点 / §6.1：标准 LangGraph/ADK 根目录 `agentengine.yaml` 被 Codex Manifest 解析
误判为 `CODEX_MANIFEST_INVALID` 的根因——`CodexManifestRepository.list/exists/load` 无脑把根
manifest 当 Codex 解析，不先看 `framework` 字段。

新增 `ksadk/studio/manifest_resolver.py`：
- `detect_manifest_kind(workspace_root)`：根 manifest 存在时先读 `framework`/`runtime.type`，
  显式 `codex` → codex kind；显式 adk/langgraph/... → framework kind；无法判定 → ambiguous。
- `root_manifest_is_codex(workspace_root)`：便捷判定。

接入 `CodexManifestRepository`（4 处）：`_root_is_codex()` helper，根 manifest 非 codex 时
`exists(None)`/`load(None)`/`list()` 全部跳过（交由 framework drafts），不再抛
`CODEX_MANIFEST_INVALID`。

测试：`test_manifest_resolver.py` **12 passed**（含 Bad Case：根 langgraph agentengine.yaml
不被误判 codex；真 codex 根 manifest 仍正确解析；Studio list_agents 不抛错；ambiguous 不猜）。

### PR-S0.5：OPENAI_BASE_URL / OPENAI_API_BASE 别名统一 ✅
方案 §2.4 第 5 点此前只"读取时或兜底"，未真正统一：
- `cmd_studio` 白名单只有 `OPENAI_API_BASE`，canary 用的 `OPENAI_BASE_URL` 经 `--env-file` 会被挡；
- `runtime_source.py` 优先级反着写（`OPENAI_API_BASE` 优先），与全局主路径（`cmd_config`/`cmd_model`/`api.py` 均 `OPENAI_BASE_URL` 优先）不一致。

统一为 **`OPENAI_BASE_URL` 优先、`OPENAI_API_BASE` 兼容**：
- `cmd_studio` 白名单接受两个别名，加载后做别名归一（任一有值则两个都设，保证下游无论读哪个都命中）；
- `runtime_source.py` 改为 `OPENAI_BASE_URL` 优先；
- CLI help 文档说明两个都支持。
- 测试：`test_env_alias_unification.py` **5 passed** + 更新 `test_cli_studio` 3 passed（验证两别名都命中、UNRELATED 仍被挡、优先级正确）。

### PR-S1：AgentSpec PCM 合同扩展 ✅
- `CompactionSpec` 加 `soft/hard_threshold_ratio` + `preserve_working_state` + `flush_memory_before_compaction`（带 ratio 校验）。
- `ContextSpec` 加 `ownership`(auto/ksadk/framework/native) + `tokenizer` + `policy_version` + `contributors` + `rollout`；`ownership=ksadk/framework` 收窄 `prompt_ownership`。
- 新增 `MemorySpec`(enabled/providerRef/recall/write/scopes) 进 `AgentSpec`。
- `context_engine/capabilities.py` 加 `allowed_ownership_choices`/`validate_ownership_for_runtime`/`resolve_ownership`（§5.2：ownership 按 capability 限制，不支持组合抛错不静默降级；`auto` 取保守默认非 capability 上限）。
- 测试：`test_pcm_contracts.py` **11 passed**。additive，向后兼容（旧 Agent 缺字段走默认）。

### PR-S2：Prompt compile + Context preview API ✅
- `api_contracts.py` 加 `PromptCompileRequest`/`ContextPreviewRequest`。
- `service.py` 加 `compile_prompt_preview`（复用 PromptCompiler+ResolvedPromptSources）+ `preview_context`（复用 hosted_pipeline，`contributors=None` 避免副作用/超时）。
- `api.py` 加 `POST /agents/{id}/prompt:compile` + `POST /agents/{id}/context:preview`（只读，不写 Session/Trace/Build）。
- 默认不返回敏感正文（`include_content` opt-in，仅 local debug）。
- 测试：`test_pcm_preview_api.py` **8 passed**（含 codex native runtime 预览、readonly 不写 run）。

### PR-S4：Runtime Context Evidence API ✅
- `RunRecord` 加 `context_plan`/`prompt_evidence`/`working_state` 字段（additive）。
- `run_service._capture_pcm_evidence`：以 shadow 方式捕获 evidence（纯计算：编译 prompt + shadow plan，**不调 build_run_input、不写 session**，避免干扰真实 run），失败不阻断。
- `api.py` 加 `GET /runs/{id}/context` + `/prompt` + `/working-state` + `/memory-events`。
- Evidence API 返回 planned/projected/actual + 精度 + ownership；native runtime 正确标 native/runtime_reported，不伪装 exact。
- 测试：`test_pcm_evidence_api.py` **6 passed**（用真实 CodexRuntimeAdapter+fake client 跑 build→run，验证 evidence 捕获 + native 不被接管）。

### Working State §8.1 修复 ✅
- `WorkingState` 加 `constraints` 字段（"不得操作生产环境" 等关键约束）。
- 加 `critical_fields_present()`（current_goal 非空校验）+ `merge_missing_from(previous)`（关键字段缺失时用压缩前 checkpoint 的 WorkingState 合并，**不接受空值覆盖**）。
- `runtime_compaction._working_state_from_checkpoint` 从旧 checkpoint 重建 WorkingState 供合并；compaction 调用 `merge_missing_from`。
- 测试：`test_working_state_fix.py` **13 passed**（含 Bad Case：压缩后丢 goal 但合并旧值保留）。

### 验证

| 套件 | 结果 |
|---|---|
| Studio 全量 | 168 → **193 passed**（+25 PCM 测试，0 回归） |
| 全量确定性（prompts/context_engine/memory/conversations/integration/studio/runtime/runners/unit memory） | **730 passed, 4 skipped** |

### 完成度更新

Studio 产品化方案 §2.3 表更新：

| 范围 | 之前 | 本轮后 |
|---|---|---|
| PCM 核心数据模型和 Runtime 主链 | ~85% | ~90%（Working State §8.1 修复） |
| Studio 后端合同接入 | ~55% | **~85%**（AgentSpec PCM 合同 + preview/evidence API + capability 校验） |
| Studio 前端产品体验 | ~25% | **~60%**（可配置表单 + capability 限制 + Inspector 完整页签 + E2E）（PCM 配置区 + 编译预览 + Context 预览 + Context Inspector） |
| 云端生产化 | ~50% | ~50%（本仓库只交付合同，真实预发见云端验证手册） |

### 仍需后续（不在本仓库 / 需前端）

1. **Studio 前端**（§7）：配置页/Context Inspector/Memory 调试页/A/B 评测页——需改 `ksadk/studio/web/src/` 并同步构建产物，工作面与回退面不同，单独 PR。
2. ~~**ManifestResolver 多 Runner 导入修复**（§6.1）~~ ✅ 已完成（本轮 PR-S0.7）。
3. **Operation 合同统一**（§6.6）：Build/Run/Deploy 返回统一 Operation schema + Idempotency-Key——部分已有，需收口。
4. **跨仓**：AgentVersion rollout 状态机、生产 Memory Service、灰度/回退——属 agentengine-server。
