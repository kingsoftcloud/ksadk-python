# 开放式 Agent Loop 收敛控制设计

## 1. 决策摘要

开放式 Agent Loop 的目标不是让 Runtime 计算一个“进展分数”，而是让 Agent 保持路径选择和探索自由，同时让控制面掌握不可篡改的目标、预算、停滞检测和验收证据。

本设计采用四个决策：

1. **先停止丢失已有信号。** Codex 已经产生 `paused`、`blocked`、`usage_limited` 和 `budget_limited`，当前却都被转换为 `RUN_INTERRUPTED`；第一优先级是保留原始原因并贯穿 RuntimeEvent、Session/Run 状态和 Studio 重放。
2. **把执行生命周期和交付结果分开。** `completed` 只表示 runner 结束；只有外部验收通过，结果才能成为 `verified`。否则应明确是 `unverified`、`partial`、`blocked` 或 `budget_exhausted`。
3. **复用现有 RuntimeExecutor、StartRequest.config 和 RuntimeEvent。** 不新建第二套执行器，也不把控制器侵入 ADK、LangGraph 或 Codex 的内部状态机。控制器位于统一事件流之上，按一次 run 持有控制状态。
4. **首版只做可观测的负检测和硬约束。** 控制器可以发现预算耗尽、工具失败、重复动作、审批阻断和验收不通过；目标分解、工具选择、路径切换和 replan 仍由 Agent 负责。

一句话：**Runtime 负责有界终止和证据账本，Agent 负责开放探索；正常结束不等于目标完成。**

## 2. 十个问题逐项回答

### Q1. 怎样判断继续投入计算是否值得？

**我的判断**：通用 Runtime 无法可靠计算开放任务的“预期价值”，也不应该根据模型自评决定是否继续。可执行的判据不是“价值是否为正”，而是同时满足三个条件：仍有明确的未解决目标或验收项、存在一个能产生新证据的具体动作、开始该动作前的剩余预算尚未进入收口区间。

**代码依据**：当前 KsADK 能观察工具、产物、usage 和生命周期事件，但没有跨框架的收益或信息增益字段。Codex 能报告 token usage 和 native goal 状态，ADK/LangGraph 路径没有对称的任务级价值模型。现有 governance 也只是计数和熔断，不是在计算投入回报。

**建议**：

- 用 `RunControlSpec.limits` 建立 wall-clock、tool、step 和可用时的 token 预算。
- 将预算分成软阈值和硬阈值；软阈值触发收口，硬阈值强制结束。
- 在启动配置中向 Agent 暴露预算和未完成验收项；运行中的动态更新只在 runtime 支持 steering 时启用。下一步仍由 Agent 决定，不让控制器计算 VOC/VOI。
- 如果没有可指向具体未完成验收项的新动作，则进入收口，不继续泛化搜索。

### Q2. Agent 如何判断自己是否取得了真实进展？

**我的判断**：下一步做什么由 Agent 决定，什么算“真实进展”则必须尽量由外部账本确认。首版不要定义抽象的 Progress Vector；只承认新证据、验收状态变化、新产物或结构化阻断解除等可回放变化。

**代码依据**：`RuntimeEvent` 已经能表达 tool、artifact、usage 和 run 事件，tool receipt 也能关联调用与结果；但这些事件没有统一的“任务状态差值”。因此 Runtime 可以判断“是否出现新的可观测事实”，不能判断“这一步让任务前进了 23%”。

**建议**：

- 进展账本只记录可引用的 `EvidenceRef`、acceptance check 状态和 artifact 版本。
- 允许 Agent 描述计划和判断，但这些描述不能直接改变 outcome。
- 对不可枚举任务只报告新增证据和限制，不生成虚假的 coverage 百分比。
- 在 P3 verifier 落地前，先以停滞负检测代替正向进展评分。

### Q3. 如何识别有效探索与无效循环？

**我的判断**：不需要理解完整语义，也能识别一部分高置信空转。首版应检测重复 action、重复结构化 error 和重复 side-effect receipt；对 observation 只使用经过脱敏和规范化的结构化摘要，不能直接保存或哈希完整原文。

**代码依据**：统一事件面已有 `TOOL_CALL_BEGIN/END`，会话路径已有稳定 receipt 和幂等键，因此 action/error/receipt 指纹有实现基础。但非流式 runner 是否完整发出工具事件尚未确认，当前也没有跨框架 active-run steering 操作。

**建议**：

- 对 `(tool_name, canonical_args)`、结构化错误码和 receipt idempotency key 建滑动窗口。
- 首次命中发 `RUN_PROGRESS{control_action: replan_required}`；重复命中或预算不足时进入收口。
- 轮询工具必须携带 version/state/expiry，或显式声明 `stagnation_exempt`，防止把正常等待误判为空转。
- 阈值通过真实 run 回放校准，不直接采用外部系统的固定数字。
- 在通用 steering API 出现前，`replan_required` 只是通知；真正改变路径仍由 Agent/runner 完成。

### Q4. 如何建立稳定收敛的任务控制机制？

**我的判断**：能保证的是“有限预算内一定停，并且停止原因可解释”，不能保证“有可行解时一定找到”。稳定收敛来自几个简单不变量，而不是一个会替 Agent 思考的复杂状态机。

这些不变量是：

1. objective、acceptance 和 limits 在 run 启动前固化，执行期间不可由 Agent 改写。
2. wall-clock、tool、step、token 等计量单调增加，且至少存在一个可执行的硬上限。
3. `verified` 只能由 verifier 产生，runner 正常返回不能自行晋级。
4. 暂停、等待输入、收口和终止具有明确边界，resume 后继续使用同一控制契约和累计账本。

**代码依据**：`StartRequest.config`、`RuntimeExecutor` 和 `RuntimeEvent` 已经提供公共入口、handle 生命周期和事件流；应在这条链路外侧增加 run-scoped controller，而不是改写每个框架内部循环。

**建议**：首版只实现 `running -> closing -> terminal` 的控制阶段，并把 `paused/waiting_input` 作为 lifecycle 状态处理。暂不引入 EXPLORE/DIAGNOSE/DEGRADE 等智能状态，因为当前没有通用 steering 能力支撑这些状态的执行语义。

### Q5. 如何定义合理的任务终止条件？

**我的判断**：终止条件需要有确定的优先级，而且“暂停等待输入”不能被当成终止。建议按以下顺序判断：

| 顺序 | 条件 | Lifecycle | Outcome |
| --- | --- | --- | --- |
| 1 | 用户取消 | `cancelled` | 已有验收交付则 `partial`，否则 `unverified` |
| 2 | 平台策略强制停止 | `interrupted` | `blocked`，reason 保留具体策略 |
| 3 | 所有必需 acceptance check 通过 | `completed` | `verified` |
| 4 | 结构化外部阻断且当前无可执行路径 | `interrupted` | `blocked` |
| 5 | hard limit 到达 | `interrupted` | `budget_exhausted` |
| 6 | 停滞重复触发且 replan 无效 | `interrupted` | 已有验收交付则 `partial`，否则 `unverified` |
| 7 | runner 正常返回但验收未通过或未配置 | `completed` | `partial` / `unverified` |
| - | 等待审批、用户输入或可恢复暂停 | `waiting_input` / `paused` | 暂不产生最终 outcome |

**代码依据**：当前 runner 流自然结束时会自动补 `RUN_COMPLETED`，Studio 也会把正常结束写成 `COMPLETED`，所以必须在兼容 lifecycle 的同时增加 outcome，不能直接改变 completed 的旧语义。

**建议**：把这张决策表固化成统一终态归约函数，并用同一组 fixture 验证 adapter、session、Studio record 和 replay 的结果一致。

### Q6. 如何定义成功、部分成功、外部阻断和不可完成等终态？

**我的判断**：不要继续扩大单一 `RunStatus` 枚举。采用 native status、lifecycle、outcome 三层模型，才能表达“执行完成但未验证”“执行中断但已有部分交付”等真实情况。

- native status 保留框架原始事实，例如 Codex `blocked` 或 `budget_limited`。
- lifecycle 表达执行是否仍在运行、能否恢复，例如 `paused`、`completed`、`interrupted`。
- outcome 表达交付质量：`verified`、`unverified`、`partial`、`blocked`、`budget_exhausted`、`failed`。

**建议**：首版不要自动提供 `infeasible`。不可完成是强语义，只有存在确定性证伪或受信 verifier 证明时才能使用；否则保守地返回 `blocked`、`unverified` 或 `failed`，并附结构化原因。

### Q7. 如何保证未完全完成时仍有可控、有价值的交付？

**我的判断**：部分交付的价值来自可复用证据和明确边界，不来自一段更委婉的总结。即使预算耗尽或外部阻断，也应尽量生成同一个 `RunResult` 契约。

**建议的最小交付内容**：

- 已通过、失败和未知的 acceptance check id。
- 可回放的 tool、artifact、command 或 assertion 证据引用。
- 已知限制以及哪些结论不能成立。
- 下一步最小动作、所需输入和是否可 resume。
- 实际消耗、触发的 limit 和原始 termination reason。

只有至少一个可枚举交付项通过且仍有必需项未完成时，才叫 `partial`。如果没有验收项或证据，只能是 `unverified`，不能用模型自报 coverage 把它包装成部分成功。

### Q8. 如何建立验证和收口机制，避免 Agent 自说完成？

**我的判断**：完成判定必须有一个 Agent 不能修改的验收闩。Codex native `complete`、ADK final response、runner 自然返回和模型声称“已完成”都只是执行信号，不是外部验收结果。

**代码依据**：现有 `RUN_COMPLETED` 可以由 adapter 自动补发；tool receipt 只证明调用与结果关联。这两者都不足以证明业务正确性。

**建议**：

- acceptance 在 build/run 创建时固化，并记录 digest 和 verifier version。
- 首版只支持受控命令退出码、artifact digest/schema 和平台确定性 assertion。
- verifier 读取 evidence，不读取模型自报 confidence 作为通过条件。
- verifier 无法执行或自身失败时进入 `unverified`/`failed`，绝不能 fail-open 为 `verified`。
- runtime 支持 active-run steering 时，收口阶段先停止新的探索性动作，再执行 verifier、汇总证据并生成 RunResult；当前通用接口不支持时，只能在 run 返回或被硬终止后执行 verifier，不能声称已经实现优雅收口。

### Q9. 开放式 Agent Loop 如何兼顾专业性与稳定性？

**我的判断**：专业性和稳定性属于不同责任层。专业性主要来自目标表达、领域 prompt、工具质量和 Agent 的规划能力；稳定性来自预算、审批、安全策略、账本和验收。Runtime 不应通过更复杂的通用状态机替代领域 Agent 的判断。

**建议**：

- 统一的是控制契约和结果语义，不强求不同框架的内部 loop 一致。
- Agent 保留分解目标、选择工具、探索替代路线和解释结果的自由。
- 控制器只在预算、安全、重复和验收边界上介入。
- 每个 runtime 声明 usage、tool event、cancel、pause/resume、native goal 等能力；缺失能力时显式降级，而不是宣传表面对称。
- 用真实任务回放评估控制规则；模型或 runner 能力变化后允许关闭无效规则，避免控制层成为能力上限。

### Q10. 哪些能力开放给 Agent，哪些放到 Loop 外围？

**我的判断**：Agent 应开放“如何做”，外围必须控制“做到什么算完成、最多花多少、哪些动作需要批准、最终如何定性”。

| 能力 | 所有者 | 原因 |
| --- | --- | --- |
| 用户目标确认、acceptance、预算和策略 | Control Plane | 必须在执行前固化，避免执行者修改评分规则 |
| 任务分解、路线选择、工具选择和 replan | Agent/Runner | 依赖领域上下文和模型能力，应保持开放 |
| approval、安全策略和 side-effect 幂等 | Runtime/Platform | 需要独立于模型执行并可审计 |
| 计量、停滞指纹和 close 触发 | Run Controller | 必须跨 run 可回放，不能依赖模型记忆 |
| evidence 提交 | Agent/Runner | Agent 知道哪些结果支持其结论，但只能提交引用 |
| evidence 验证和 outcome 归约 | Verifier/Controller | 防止自评完成和篡改验收标准 |
| 最终文本的专业表达 | Agent | 属于领域交付内容，不等同于状态判定 |

**建议**：以只读 `RunControlSpec` 和 additive `RunResult` 作为内外边界。不要把控制策略塞进 prompt 后假装已经受到平台约束，也不要让控制器接管 Agent 的每一步规划。

## 3. 设计取舍

### 3.1 保留的判断

开放式循环如果只有模型输出，没有外部账本和硬约束，就会出现无限探索、重复工作、提前宣布完成和预算不可控。

以下判断与当前代码相容，应保留：

- 预算、停滞和终止原因应由 Runtime/控制面掌握，而不是由模型口头声明。
- 验收标准应在执行前固化，Agent 只能提交证据，不能修改通过条件。
- 部分交付必须带限制、证据引用和下一步，而不是把所有非完整结果归为失败。
- 停滞检测先做重复动作、重复观测和重复失败等外部可观测信号，不实现跨框架的“正向进展评分”。
- 需要给长任务一个收口阶段，避免硬上限到达后只留下一个无上下文的错误。

### 3.2 常见误解与校正

以下说法容易混淆当前能力与目标设计，需要结合代码边界校正：

| 常见误解 | 校正后的说法 |
| --- | --- |
| Codex 的丰富终态已经是 KsADK 的通用终态 | Codex 原生状态已被接收，但在 KsADK 边界被压扁；应先做保真映射，不能假设 ADK/LangGraph 已有同等能力 |
| 增加 `budget_exceeded` 等枚举即可解决终态问题 | 至少要同步 adapter、RuntimeEvent payload、Session 状态、Studio RunRecord 和重放投影，单改枚举不够 |
| Tool receipt 可以证明 coverage 或业务正确性 | Tool receipt 只能证明调用与结果的关联，是 EvidenceRef，不是通用业务验收器 |
| `goal`、`max_steps`、`timeout_seconds` 已经对所有 runtime 生效 | Studio 有 authoring 字段，但当前未确认被一致编译进所有 StartRequest，也未确认由公共控制器执行 |
| Runtime 可以计算六维进展、信息增益或 VOC | 当前事件面不提供跨框架、可审计的正向进展定义；首版只做预算和负检测 |
| 可以把外部系统的 3/4/6 次循环阈值直接照搬 | 阈值要基于本仓回放和误杀率校准，并为轮询、等待外部异步结果保留显式例外 |

## 4. 当前代码基线

### 4.1 已有的可复用基础设施

以下结论已通过当前源码确认：

| 能力 | 代码依据 | 设计含义 |
| --- | --- | --- |
| 公共启动配置 | `ksadk/runtime/adapter.py:184-194` 的 `StartRequest.config`；`ksadk/runtime/runner_adapter.py:604-617` 合并到 runner input | `RunControlSpec` 可以 additive 地从控制面下沉，不需要新启动协议 |
| 统一执行器 | `ksadk/runtime/executor.py:39-140` 的 `RuntimeExecutor` 负责 adapter、handle、stream、cancel、resume、checkpoint、close | 控制器应包在 executor/事件流外侧，不复制 handle 生命周期 |
| 统一事件面 | `ksadk/events/runtime_event.py:41-84` 定义 run、tool、artifact、approval、checkpoint、usage 事件 | 预算、停滞和证据可以消费统一事件；不要依赖框架内部 state |
| 追加式会话账本 | `ksadk/sessions/base.py:38-49` 的 `SessionEvent`；`ksadk/sessions/local_service.py:137-171` 支持追加、分页读取和计数 | 可以按 run 重放控制状态；生产环境的原子写入和唯一写者仍需验证 |
| 工具回执 | `ksadk/conversations/runtime_resume.py:265-296` 生成 receipt/idempotency 字段；`runtime_stream_events.py:550-650` 持久化结果和回执 | 可作为证据引用与幂等关联，不自动推出业务正确性 |
| usage 事件 | `ksadk/events/runtime_event.py:73-74`；Codex 在 `ksadk/codex/runtime.py:558-583` 发 `USAGE_REPORTED` | Codex 有 token 计量基底；其他 runner 只有在发出 usage 时才可按 token 计费或限额 |

### 4.2 已确认的缺口

1. `ksadk/conversations/runtime_governance.py:42-97` 的治理由环境变量驱动，统计 invocation turn、tool call、连续工具失败、审批拒绝和 compact 失败；它不是一次内部 Agent Loop 的统一控制器。
2. `KSADK_MAX_TURNS` 在会话入口记录一次 invocation turn，不能解释为 runner 内部每个 plan/act/observe step 的计数。
3. `ksadk/runtime/runner_adapter.py:553-568` 会在 runner 流没有终态时补发 `RUN_COMPLETED`；会话非流式路径也会在 runner 正常返回后写 completed。因此当前 completed 不能解释为目标已验证。
4. `ksadk/codex/runtime.py:585-600` 把 Codex goal 的 `paused`、`blocked`、`usage_limited`、`budget_limited` 都包装为 `RUN_INTERRUPTED`。
5. `ksadk/studio/run_service.py:212-226` 只特殊处理 `paused`，其他中断原因均成为 `INTERRUPTED`；`run_service.py:671-680` 的事件投影同样只有 paused/interrupted。
6. `ExecutionSpec` 在 `ksadk/studio/contracts.py:209-215` 声明 `max_steps` 和 `timeout_seconds`，但 `FrameworkRunSpecResolver` 当前主要下沉 instructions、entry point 和 agent variable（`ksadk/studio/framework_run.py:64-88`）。字段存在不等于跨 runtime 生效。
7. `goal_objective` 是 Codex 专属消费路径：Studio 可以把它放进 spec config，但只有 `ksadk/codex/runtime.py:415-426` 使用它。不能把 Codex ThreadGoal 当作 ADK/LangGraph 的公共语义。

## 5. 三层状态模型

单一扁平状态会把“暂停等待输入”“runner 正常结束”“预算耗尽”和“验收通过”混在一起。首版使用三层状态：

### 5.1 Native status：适配器原生状态

这是 adapter 观察到的框架或服务原始状态，必须保留在事件 payload 的 `native_status` 或 `native_data` 中，不用于跨框架直接比较。

Codex 当前已知值包括：

| Native status | 语义 | 首版 canonical 处理 |
| --- | --- | --- |
| `active` | 仍在执行 | lifecycle=`running` |
| `paused` | 可恢复暂停 | lifecycle=`paused`，保留 resume 能力 |
| `blocked` | 原生目标被阻断 | lifecycle=`interrupted`，outcome=`blocked`；是否为外部阻断需 verifier/控制面确认 |
| `usage_limited` | 原生 usage 上限触发 | lifecycle=`interrupted`，outcome=`budget_exhausted`，reason=`usage_limited` |
| `budget_limited` | 原生预算上限触发 | lifecycle=`interrupted`，outcome=`budget_exhausted`，reason=`budget_limited` |
| `complete` | Codex 原生目标完成信号 | 仍需按本次 RunControlSpec 的 acceptance 验证，不能仅凭模型/服务信号晋级 verified |

已安装 Codex schema 还包含 `tokenBudget`、`tokensUsed` 和 `timeUsedSeconds`；但当前 `ksadk/codex/client.py:729-744` 的 goal wrapper 只调用 `start_goal_operation(thread_id, objective)`，尚未确认 KsADK 可以配置原生 token budget。因此“能观察到 usage”与“能通过 KsADK 配置 native budget”必须分开记录。

### 5.2 Lifecycle：执行生命周期

Lifecycle 描述 run 是否仍然存在、是否可恢复：

```text
created -> running -> paused -> running
                 -> waiting_input -> running
                 -> completed
                 -> failed
                 -> cancelled
                 -> interrupted
```

`paused`、`waiting_input` 和 `interrupted` 不是同一个状态：前两者通常有明确恢复入口，`interrupted` 表示本次执行已停止但未必可恢复。现有 `RuntimeEvent v1` 的 `RUN_INTERRUPTED` 可以继续承载原因，不要为每一个原因新增 event type。

### 5.3 Outcome：交付结果

Outcome 描述目标交付质量，与 lifecycle 独立：

| Outcome | 含义 | 进入条件 |
| --- | --- | --- |
| `verified` | 已满足本次固化的确定性验收项 | 所有必需 acceptance check 通过，证据可回放 |
| `unverified` | 执行结束但没有足够外部证据 | runner 正常返回或原生完成，但验收未配置、未执行或无法判定 |
| `partial` | 一部分可枚举验收项通过，仍有明确缺口 | 至少一个交付项通过，至少一个必需项未通过或未完成 |
| `blocked` | 依赖、权限、审批或外部服务使任务无法继续 | 有结构化阻断证据；不要由模型单独声明“不可完成” |
| `budget_exhausted` | 触发 step、tool、wall-clock、token 或 native budget | 记录触发的 limit 和实际计量 |
| `failed` | 执行或验收基础设施失败，无法形成更具体结果 | adapter、verifier 或持久化发生不可恢复错误 |

`partial` 与 `blocked` 可以同时有更细的 `reason` 字段，但首版不把 `blocked_external`、`infeasible_with_evidence` 等词直接扩成公共枚举，除非已经有稳定的 verifier 和跨 runner 语义。

## 6. 公共控制契约

控制面在启动前生成并冻结 `RunControlSpec`，通过已有 `StartRequest.config` 传递。Agent 不能修改 objective、acceptance 或 limits；resume 只能携带允许的输入和控制面签发的补充信息。

### 6.1 RunControlSpec

```text
RunControlSpec
  objective: string
  acceptance: AcceptanceCheck[]
  limits:
    max_steps?: integer
    max_tool_calls?: integer
    timeout_seconds?: number
    token_budget?: integer
  close_reserve:
    enabled: boolean
    threshold?: number
  stagnation:
    enabled: boolean
    action_window?: integer
    observation_window?: integer
    failure_window?: integer
```

字段语义：

- `objective` 是任务目标，不等价于模型最后一段文本。
- `acceptance` 是控制面签发的验收项，必须带稳定 id，写入 build/run 版本或 digest。
- `max_steps` 只在控制器能识别统一 step 边界时生效；不能把现有 invocation turn 计数冒充内部 step。
- `max_tool_calls` 可在 `TOOL_CALL_BEGIN` 事件边界计数；对于不产生统一 tool event 的 runner，应标记能力缺失而不是假装精确。
- `timeout_seconds` 使用 wall clock；超时处理需要可取消或关闭底层 handle，否则只能记录超时而不能声称已终止。
- `token_budget` 只有收到 `USAGE_REPORTED` 时才按平台账本累计；缺失 usage 时不得估算成精确 token 数。
- `close_reserve` 是软阈值，不是固定的 85% 或 90%；首版可由控制面配置，达到后发出收口信号并优先产出当前证据。

### 6.2 AcceptanceCheck

首版只支持确定性、可重放的检查：

```text
AcceptanceCheck
  id: string
  type: command_exit | artifact | assertion
  required: boolean
  spec: object
  verifier_version: string
```

推荐的 verifier：

- `command_exit`：受控命令退出码、超时和 stdout/stderr 摘要。
- `artifact`：产物存在性、digest、媒体类型、schema 或大小约束。
- `assertion`：平台定义的确定性断言，例如字段存在、状态值匹配或数值范围。

模型自报 confidence、coverage、完成理由不能把 `unverified` 晋级为 `verified`。Tool receipt 只能作为 `EvidenceRef`，证明某次工具调用及其表层结果已发生。

### 6.3 RunResult

终态事件和持久化记录增加 additive 的结果对象，不立即破坏现有 lifecycle 字段：

```text
RunResult
  execution_status: completed | failed | cancelled | interrupted
  outcome: verified | unverified | partial | blocked | budget_exhausted | failed
  acceptance_summary:
    passed: string[]
    failed: string[]
    unknown: string[]
  evidence_refs: EvidenceRef[]
  limitations: string[]
  next_actions: string[]
  termination_reason: string
  counters: object
```

`coverage` 只在 acceptance 可枚举且计算规则固定时提供。不能让模型随意提交 `0.87` 这样的数字；无法计算时留空，并通过 `limitations` 解释原因。

## 7. 执行与持久化流程

```text
Control Plane
  | 固化 objective / acceptance / limits / policy
  v
StartRequest.config
  v
RuntimeExecutor -> Adapter -> Runner
  | RuntimeEvent: run / tool / artifact / usage / approval
  v
Run Controller
  | 计量、停滞检测、账本、verifier 调度
  +--> RUN_PROGRESS: closing / replan_required / budget_warning
  +--> RUN_INTERRUPTED: paused / input_required / budget_exhausted / blocked
  +--> RUN_COMPLETED: execution completed + RunResult
  +--> RUN_FAILED: verifier/control failure
  v
SessionEvent / RunRecord / replay projection
```

控制器需要记录：

1. `run_id`、session、agent、build/manifest digest 和 `RunControlSpec` 版本。
2. 每个计量的来源 event seq：tool call、usage、artifact、verifier result 和 termination reason。
3. 原始 native status 与 canonical lifecycle/outcome 的映射。
4. 验收标准的只读版本和 verifier 版本。

持久化仍沿用现有 SessionEvent/RuntimeEvent 账本，但生产部署还需验证跨 pod 的原子追加、resume 并发和唯一写者。源码接口能证明有追加/回放能力，不能单独证明分布式一致性。

## 8. 分阶段实现路线

### P0：保留终止原因

目标是“先不丢信息”，不引入通用智能控制器。

- Codex adapter 保留 `native_status`、原始 goal 对象和 `termination_reason`。
- `blocked` 映射到 `outcome=blocked`；`usage_limited` 和 `budget_limited` 映射到 `outcome=budget_exhausted`；`paused` 保持可恢复 lifecycle。
- 同步修正 RuntimeEvent payload、conversation run status、Studio RunRecord 和 replay projection。
- 对现有自动补发的 `RUN_COMPLETED` 添加 `outcome=unverified`，保持旧客户端仍看到 lifecycle `completed`。

验收：同一组 adapter fixture 覆盖四种 Codex goal 状态，持久化、Studio 展示和 replay 后均保留原始 reason；普通 runner 正常返回仍兼容，但不再被解释为 verified。

### P1：公共硬预算和收口

- 从 resolved `ExecutionSpec` 生成公共 `RunControlSpec.limits`，通过 `StartRequest.config` 下沉。
- 控制器按可观测来源累计 wall clock、统一 event/step、tool calls 和 usage。
- 达到软阈值时发 `RUN_PROGRESS`，要求 runner/Agent 进入 close；达到硬阈值时输出 `budget_exhausted`。
- 原生 Codex budget 可以更早停止，但平台账本必须记录原始 native reason。

验收：对 Codex、ADK 和 LangGraph fixture 使用同一 time/step/tool 限额时，outcome 语义一致；缺 usage 的 runner 明确标记 `token_usage_unavailable`。

### P2：停滞检测

- 在 `TOOL_CALL_BEGIN/END` 规范化 `tool_name + args`、结果状态和安全摘要，计算可复现指纹。
- 首版检测连续相同 action、相同 action-observation、重复结构化错误和重复 side-effect receipt。
- 命中后先发 `RUN_PROGRESS` 的 `control_action=replan_required`；再次命中或预算不足时收口，不直接假设任务失败。
- 轮询/等待异步结果必须带显式 state/version/expiry，或者由工具声明 `stagnation_exempt=true`；不能简单按相同调用次数杀掉正常等待。
- 原始敏感输出不进入 hash 或日志；参数规范化规则要处理无序 JSON、默认值和可忽略字段。

验收至少覆盖：连续相同 action、参数顺序变化、相同观测但不同 action、正常轮询、超大输出和敏感参数。

### P3：验收闩和部分交付

- acceptance 随 build/run 固化，Agent 只能提交 evidence ref。
- verifier 只接受确定性命令、产物和断言；verifier 自身失败进入 `failed` 或 `unverified`，不能伪装成通过。
- 根据 passed/failed/unknown 生成 `verified`、`partial` 或 `unverified`，并持久化 limitations、evidence_refs 和 next_actions。
- 只有 acceptance 可枚举时计算 coverage；工具回执不直接转换为业务 coverage。

验收：伪造完成、无证据完成、部分通过、外部依赖阻断、verifier 失败分别得到稳定结果，不能全部落到 completed。

### P4：按数据重新评估控制强度

P4 不是默认增加更多状态机，而是根据真实 run 回放评估：

- 停滞规则的误杀率、漏检率和正常轮询占比。
- close reserve 是否提高 partial 可用率，是否造成过早收口。
- 模型升级后某些控制是否已经成为负担；可按 runtime/model 能力关闭，而不是永久固化。
- 是否出现足够稳定的跨框架 step、artifact 和 verifier 语义，再考虑更丰富的策略或 route 状态机。

## 9. 兼容性与所有权边界

### 9.1 兼容性策略

- 保留现有 lifecycle event type；使用 payload additive 字段承载 `native_status`、`outcome`、`result` 和 `termination_reason`。
- 保留旧客户端的 `completed/failed/cancelled/interrupted` 读取路径；新客户端读取 `RunResult`。
- `RunStatus` 的扩展必须同步 conversation canonical 定义、server/API schema、Studio contract 和 replay projection，不能只改一个 Literal。
- 缺少 usage、统一 tool event 或可取消句柄时，返回能力标记和限制，不伪造精度或终止保证。

### 9.2 所有权边界

| 责任 | Control Plane / Controller | Agent / Runner |
| --- | --- | --- |
| objective 与 acceptance | 固化、版本化、只读 | 读取并执行 |
| tool/path 选择 | 不干预，除非安全策略要求 | 自主决定 |
| step/tool/time/token limit | 计量、告警、终止 | 可读取剩余预算并调整计划 |
| replan | 发出结构化信号，必要时暂停 | 解释信号并选择新路径 |
| evidence | 保存、关联、验证 | 提交引用，不修改标准 |
| outcome | 根据账本和 verifier 计算 | 不能自行宣布 verified |
| native status | 保留原文并映射 | 提供 adapter 观察结果 |

不在本次设计中引入：跨框架统一的内部 state schema、在线 VOC/VOI 计算、六维 progress vector、route 多状态机、自报 confidence 晋级规则，以及把平台 registry、sandbox 生命周期或 gateway discovery 搬进 SDK。

## 10. 未决问题与验证前提

以下内容不能从当前源码直接确认，实施前必须通过针对性测试或跨仓协议核对：

1. 非流式 runner 的所有工具调用是否都能投影为统一 `TOOL_CALL_BEGIN/END`；目前明确证据主要来自流式 conversation 路径。
2. 不同部署 backend 是否支持控制器所需的原子账本追加、唯一写者和 resume 并发保护。
3. Codex wrapper 是否会开放原生 `tokenBudget` 的配置；当前可确认观察到 `tokensUsed/timeUsedSeconds`，不能据此声称 KsADK 已配置 native budget。
4. `blocked` 是否代表外部阻断、策略阻断还是 native goal 状态；没有结构化原因时先保留 native reason，不自动升级为 `blocked_external`。
5. `max_steps` 在各框架中的 step 边界是否可一致定义；在此之前不能把 Studio authoring 字段宣传为公共 hard limit。
6. verifier 失败时是将执行标记为 `failed`，还是保留已交付的 `partial`；需要由产品 SLA 和重试语义决定，首版应保留原始 verifier error。

## 11. 最终建议

建议以 P0 作为第一项实现和评审边界：它改动小、能立即提升事实准确性，也能为 P1-P3 提供稳定的终态字段。P0 完成并通过 replay/Studio fixture 后，再下沉公共预算；停滞检测和验收闩不要与状态映射混在同一个大改动中。

本方案不承诺“任务一定完成”，只承诺三件更可验证的事：

1. 运行何时结束、为什么结束，能从事件和账本中解释。
2. 已完成、未验证、部分交付、阻断和预算耗尽不会再被同一个 completed/interrupted 值掩盖。
3. Agent 保持开放式探索，但平台拥有可配置、可回放、可逐步加强的终止和验收边界。
