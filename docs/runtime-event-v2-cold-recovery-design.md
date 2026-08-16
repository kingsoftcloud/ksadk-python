# runtime-event-v2 冷恢复与运行所有权设计

> 状态:proposed ｜ 分支:feat/runtime-event-v2-redesign ｜ 2026-08-17
> 范围:canonical kernel 的进程崩溃冷恢复(cold recovery)与运行所有权(run ownership),统一流内恢复与冷恢复为一条路径。参考 DeepSeek Harness `core/session/repair.ts` 的确定性结局思想,但按 ksadk 多框架 adapter + 托管多进程形态重新设计。

## 1. 问题

canonical 体系已有**流内恢复**(`ksadk/events/pipeline.py:124` `_recover`):进程活着时,事件流不符合 reducer 契约(乱序、缺事件、非法状态迁移)触发确定性 conformance recovery(recovery plan + fingerprint + owner/terminal event id),修复事件本身进 store。

缺**冷恢复**:进程崩溃/被杀/pod 漂移后,store 里留下**开放 run**(有 `run.started` 无终态)和**开放 item**(有 `item.started`/`item.updated` 无 `item.completed`/`item.failed`)。当前行为:

- replay(`canonical_replay.replay_projection`)对开放 run 的投影不确定:依赖消费方自行处理"流戛然而止";
- resume 路径(`ensure_canonical_resume_allowed`)只挡 v1 run,对"canonical run 崩溃后可否 resume"无裁决;
- 无所有权概念:两个进程(旧 pod 未死透 + 新 pod)可能同时操作同一 run。

## 2. 设计原则

1. **修复事件是事实,不是旁路**:冷恢复产生的结局事件与正常事件一样进 store、参与 replay——消费方无需特判"这条流是被修复过的"。
2. **先探测 resume,不可恢复才关 run**:与 dsh 单机"发现开放 turn 一律关闭"不同,ksadk 各框架有 checkpoint/continuation 能力(ADK graph_checkpoint、LangGraph thread、Codex session resume、A2A task resume),能接管就接管,不能接管才合成确定性结局。
3. **所有权靠幂等事实,不靠分布式锁**:恢复事件的 `event_id` 由确定性身份推导(同一 run 的冷恢复结局,谁算都是同一个 id),`(session_id, event_id)` 物理主键天然去重——双进程竞争恢复,第二个写入者被 `RuntimeEventStore._assert_same_fact` 拒绝,无需额外租约协议。租约(如果有)只用于**防止重复接管执行**,不参与事件写正确性。
4. **fail-closed**:探测不到明确结局时,宁合成 `outcome_unknown` 结局,不把开放 run 留给未来。

## 3. 冷恢复事件语义(不新增事件类型)

复用既有 canonical 事件,不扩 schema:

| 场景 | 合成事件 | 关键字段 |
|---|---|---|
| run 不可 resume | `run.interrupted` | `reason="process_exit"`,复用 `continuation_id`(若存在)指向 continuation |
| run 不可 resume 且策略要求终结 | `run.failed` | `error.code="run_orphaned"` |
| 开放 item(未 dispatch 的 tool_call) | `item.failed` | `error.code="tool_not_started"` |
| 开放 item(已 dispatch,结局未知) | `item.failed` | `error.code="tool_outcome_unknown"` |
| 开放 item(非 tool,如 message/reasoning) | `item.completed` + 当前折叠 snapshot | reducer 已有 provisional snapshot 语义 |

结局事件由**恢复者**在恢复时推导生成,`source.framework="ksadk"`、`source.metadata.recovery="cold"` 标记来源;`event_id` 用 `stable_event_id(framework="ksadk", scope, item, "cold_recovery", ...)` 保证确定性(原则 3)。

## 4. 运行所有权模型

**事件层(本设计范围)**:所有权是推导视图,不是新实体。定义 `RunOwnership` 投影:

- `claimed`:最近的 `continuation.resumed` 事件携带 `resume_attempt_id`(已存在字段),即当前执行属主标识(pod/进程以 attempt id 区分);
- `released`:终态事件(run.completed/failed/canceled/interrupted)即释放;
- 冷恢复裁决规则:**只有当恢复者看到的 `resume_attempt_id` 与自己的 attempt 不同、且旧 attempt 无心跳证据时,才允许合成旧 attempt 的结局**;同 attempt 不自杀(自己崩了自己恢复走正常 resume 路径)。

**执行层(托管,agentengine-server/pod 侧,不在本仓)**:pod 租约/k8s termination 语义决定"旧 attempt 确实死了",通过 `resume` 请求的 `resume_attempt_id` 传递给数据面。数据面不做活性探测,只消费执行层的裁决——字段归属跨界清晰(AGENTS.md §5)。

## 5. 统一恢复路径

流内恢复与冷恢复共享一张恢复事实表(扩展 pipeline 的 recovery plan 机制):

```
cold_recovery_scan(store, session_id) -> list[RecoveryFinding]
    # 1. replay_projection 检出开放 run + 开放 items
    # 2. 每个开放 run: 查 continuation(continuation.created resumable?)
    #    - resumable=True 且执行层 attempt 裁决允许接管 -> 不产事件,交 resume 路径
    #    - resumable=False 或裁决拒绝 -> 合成 run.interrupted/run.failed + items 结局
    # 3. 产物走 CanonicalEventPipeline.ingest(与流内恢复同一入口,复用幂等/fingerprint)
```

入口:`CanonicalEventStore` 新增 `recover_session(session_id, attempt_id) -> RecoveryReport`(只读扫描 + 事件合成,报告哪些 run 接管/哪些关闭);`replay_projection` 内部在检出开放 run 时调用同一裁决,保证任何读路径看到的要么是完整流、要么是带确定性结局的流。

## 6. 与 resume 能力的框架差异

| 框架 | continuation_kind | 冷恢复裁决 |
|---|---|---|
| ADK | graph_checkpoint | checkpoint 存在且 resumable → 接管 |
| LangGraph | thread_resume | thread 状态可加载 → 接管 |
| Codex | thread_resume / task_resume | app-server session 可恢复 → 接管 |
| A2A | task_resume(远程) | 远程 task 查询超时/终态 → 本地关 run |
| 无 continuation 事件 | — | 直接合成结局 |

裁决依据全部来自 store 内的 `continuation.created` 事件(`resumable` 字段已存在),数据面不直接调框架——保持"事件是唯一事实源"。

## 7. 交付切分

1. **P0**:`cold_recovery_scan` + 结局合成 + `RecoveryReport`,纯数据面,单测覆盖每个结局分支(含双恢复者幂等);
2. **P0**:`replay_projection` 接入裁决(开放 run 不再裸露给消费方);
3. **P1**:`resume_attempt_id` 所有权裁决(执行层字段打通后启用,先留接口);
4. **P1**:release gate 测试补冷恢复场景(崩溃中点 fixture → replay 必须产出完整流)。

## 8. Alternatives considered

- **dsh 式"发现开放一律关闭"**:单机单进程成立;ksadk 托管形态下会错杀可 resume 的 run(ADK checkpoint 白做)。败于框架 resume 能力浪费。
- **新增 `run.recovery.*` 事件类型**:envelope-first lenient 解析已允许扩类型,但结局语义用既有事件已可完整表达,新类型只增加 reader 负担。败于最小 schema。
- **分布式租约管事件写正确性**:把正确性从"确定性 event_id 幂等"挪到"租约协议",引入新故障模式(租约服务挂了恢复也挂)。租约只该管执行不写事件。
- **恢复旁路(修复不进 store,消费方各自处理)**:每个消费方重新发明悬空流处理,重演 v1 时代"下游从文本反推"的病根。
