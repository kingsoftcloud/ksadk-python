# ksadk 迭代规划压缩版

> 状态：建议稿  
> 更新：2026-05-05  
> 依据：`ksadk-python` 本地 `master@5d723ab9d15a7233eebe9623e82865de4b4a745a`、同工作区 hosted UI 源码、`agentengine-server` gateway/Helm 配置，以及 `veadk-python` 本地 `main@bf3c45d0384e52ed205ff637e55bd5ec8845f8c0`（`git pull --rebase` 已确认 Already up to date）。  
> 边界：veadk 能力核验聚焦在当前对标文档涉及的 memory、builtin tools、eval、skills、prompt、realtime、RL/Vanna 目录；本建议仍以 ksadk 当前实现和生产风险为主。

---

## 1. 结论

原 `veadk-benchmark-and-iteration-plan.md` 对 veadk 能力的主体判断基本成立，但不适合直接作为 ksadk roadmap。它列了很多 veadk 特性，其中真正值得 ksadk 近期投入的只有三类：

1. P0：会话与运行时连续性生产化，目标是跨 pod 可恢复。
2. P1：高频内置工具产品化，目标是降低用户从 0 写工具的成本。
3. P1：评估闭环，目标是让 agent 质量可回放、可比较、可回归。

其它能力按需吸收，不建议为了对标而追：

- 实时语音：除非业务明确要求语音入口，否则不进入近期 roadmap。
- RL 脚手架、Vanna SQL、Ghostchar：供应商或场景色彩重，短期不跟。
- PromptManager / Reflector：有参考意义，但应排在 eval 之后。
- 技能系统：有中长期价值，但先做轻量 validation / approval metadata，不上完整市场化体系。

veadk 最新代码里可确认的参考点：

- `veadk.memory.ShortTermMemory` 支持 `local/mysql/sqlite/postgresql/database`，PostgreSQL 后端基于 ADK `DatabaseSessionService`。
- `veadk.tools.builtin_tools` 已有 `web_search`、`link_reader`、`run_code`、`image_generate`、`tts`、`web_scraper`、`lark` 等工具。
- `veadk.evaluation` 包含 eval set recorder、ADK evaluator、DeepEval evaluator。
- `veadk.skills`、`veadk.prompts`、`veadk.reflector`、`veadk.realtime`、`veadk/cli/templates/rl`、`veadk/tools/vanna_tools` 都是实际存在的能力目录。

---

## 2. P0：跨 pod 会话可恢复

### 当前问题

当前 hosted UI 前端会缓存 `SessionId`，并依赖这些 action：

- `CreateSession`
- `ListSessions`
- `ListSessionEvents`
- `RunAgent`
- `DeleteSession`

本地 runtime 实现中，`ListSessions`、`ListSessionEvents` 直接读当前进程的 `resolve_session_service()`。`ksadk.sessions` 默认返回 `LocalSessionService`，底层是 pod 本地 sqlite 文件。

这意味着多副本部署时，如果同一个浏览器的请求被调度到不同 pod，会出现：

- 会话列表拿不全。
- 已存在 session 在另一个 pod 上 `GetSession/ListSessionEvents` 查不到。
- LangGraph 没有共享 checkpointer 时，只能依赖平台层 history replay；平台 session 也不共享时就会失效。
- compaction boundary、context checkpoint、approval 事件等平台状态跟着丢。

### “跨 pod 可恢复”的含义

这里不是要求所有请求必须回到同一个 pod，而是要求同一个 `session_id` 访问任意 pod 都能恢复会话。

最低目标：

- `ListSessions` 完整。
- `ListSessionEvents` 稳定。
- conversation runtime 可以用共享事件重建 history。
- compaction state 不随 pod 切换丢失。

更高目标：

- LangGraph 原生 runtime state 也能恢复。
- 如果 graph 挂了共享 checkpointer，continuity level 可以从 `semantic/replay` 提升到 `runtime/checkpoint`。

### 不建议把 sticky session 当根治方案

同一 session 优先调度到同一个 pod 可以作为缓解，但不能作为正确性保证。

原因：

- 当前 `agentengine-server` gateway ingress 主要配置了 auth、SSE、限流，没有看到基于 `SessionId` 的粘性路由策略。
- Nginx sticky 通常更容易按 cookie/client IP 做，不适合稳定识别 JSON body 里的 `SessionId`。
- SSE、刷新、移动网络、pod 重启、滚动发布都会破坏“回同 pod”的假设。
- 即使 sticky 生效，也解决不了会话列表聚合、pod 下线迁移、并发写入一致性。

更合理的定位：

- sticky session：降低短期用户可见抖动。
- shared session store：保证正确性。
- LangGraph shared checkpointer：保证框架原生 runtime state。

### 建议实施

第一阶段：新增共享平台 session backend。

- 支持 `AGENTENGINE_SESSION_BACKEND=postgres|redis|local|memory`。
- 保持 `local` 作为本地开发默认。
- 生产环境默认切到 `postgres` 或 `redis`。
- `ListSessions/GetSession/ListSessionEvents/CreateSession/DeleteSession/append_event/update_state` 都走同一个共享后端。
- 需要版本号或乐观锁，避免同一 session 并发写覆盖。

第二阶段：LangGraph continuity 分级。

- 无 checkpointer：明确标记为 `semantic/replay`，依赖共享平台 events 重建上下文。
- 有本地 MemorySaver：只适合单 pod，本地开发可用，生产提示风险。
- 有 Postgres/Redis checkpointer：标记为 `runtime/checkpoint`。

第三阶段：部署侧提供缓解选项。

- 可选开启 cookie affinity，作为兼容期缓解。
- 不把 affinity 写成生产正确性的前置条件。
- 在 dashboard/bootstrap capability 中暴露 session backend / continuity status，便于 UI 或诊断页提示。

---

## 3. P1：高频内置工具产品化

原文说 ksadk “几乎零工具”不准确。当前 ksadk 已经有 sandbox、MCP toolsets、知识库、长期记忆、approval/resume 等平台能力。

真正缺的是“默认可用、文档清楚、跨框架一致”的高频工具包。

建议优先级：

1. `web_search`
2. `link_reader`
3. `code_runner`
4. `file_reader/file_writer` 的受控版本
5. `image_generate`、`tts/asr` 放到业务明确后再做

实现原则：

- 不把工具堆进所有 agent；按 env/capability 开关注入。
- 工具 schema、错误格式、approval 策略统一。
- ADK / LangGraph / LangChain 的接入方式保持一致文档口径。
- 对外重点宣传“开箱即用工具集”，不是追 veadk 的工具数量。

---

## 4. P1：评估闭环

这项比 prompt optimize 更优先。

当前 ksadk 已有 session events、tracing、responses/chat runtime、hosted UI action，适合先做轻量 eval：

1. 从 session events 导出 eval case。
2. 支持固定输入回放。
3. 比较输出文本、tool call、approval、attachments 等关键行为。
4. 接入 LLM-as-judge 作为可选指标。
5. 输出稳定的 CLI/JSON 报告，供 CI 或发布前检查使用。

建议先做 `ksadk eval run`，不要先做复杂 PromptManager。

---

## 5. P2：技能与提示优化

技能系统有价值，但现阶段不建议完整复刻 veadk 的技能市场。

短期只做轻量机制：

- tool metadata 增加 `risk_level`、`approval_required`、`validation_steps`。
- runner 在关键工具调用前后记录 validation 结果。
- hosted UI 展示 approval/validation 的结构化状态。

Prompt optimize / Reflector 的前置条件是 eval。没有 eval 的优化只能生成“看起来更好”的 prompt，不能证明质量提升。

---

## 6. 删除噪声项

这些不进入近期路线图：

- 实时语音 WebSocket 协议。
- RL 数据集生成与训练平台初始化。
- Vanna SQL。
- Ghostchar。
- 完整 PromptManager / PromptPilot。
- 完整技能市场。
- 为了对标而扩展一堆供应商专用工具。

---

## 7. 推荐近期路线

### 1-2 周

- 设计并实现共享 `BaseSessionService` 后端，优先 Postgres 或 Redis 二选一。
- hosted UI / runtime action 全部走共享 session store。
- 增加多副本风险诊断：启动时输出 session backend、storage path、continuity mode。
- 文档明确：本地 `LocalSessionService` 不适合多 pod。

### 1 个月

- LangGraph checkpointer best practice 文档和示例。
- `SessionContinuityStatus` 区分 `replay`、`checkpoint`、`native_session`。
- 增加 `web_search/link_reader/code_runner` 的第一版工具集。
- 增加 session events 导出 eval case 的最小闭环。

### 1-3 个月

- `ksadk eval run` + LLM judge。
- tool validation / approval metadata。
- 生产部署中增加可选 affinity，但仅作为兼容缓解。
- 基于 eval 结果再考虑 prompt optimize。

---

## 8. 判断标准

优先做能直接改善生产可用性的能力：

- 多副本下会话列表完整。
- 同 session 切 pod 后仍能续聊。
- LangGraph 是否能恢复 runtime state 可被明确诊断。
- agent 质量能回放和回归。
- 高频工具开箱可用。

不优先做只增强“能力列表观感”的能力。
