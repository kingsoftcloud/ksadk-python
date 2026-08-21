# Studio Semantic Trajectory Records Implementation Plan

> **For agentic workers:** Execute this plan task-by-task with focused tests after each task.

**Goal:** 将 Studio 轨迹从按 Event 粗暴折叠改为基于 RuntimeEvent 的 User、Context、Assistant、Tool、System 业务节点增量归并。

**Architecture:** SQLite 中继续只追加 RuntimeEvent。后端为每个事实生成稳定的语义贡献，前端 reducer 以 `recordKey` 原位 upsert 为业务节点；历史分页和 SSE 都经过同一归并路径。Spans 保持独立的 OTLP 拓扑视图。

**Tech Stack:** Python、Pydantic、FastAPI/SSE、React/TypeScript、Vitest、pytest。

---

### Task 1: 固化语义节点投影契约

**Files:**
- Modify: `ksadk/observability/trajectory.py`
- Test: `tests/observability/test_trajectory.py`

- [ ] 为 User、Context、Assistant、Tool、System、Approval、Artifact 定义稳定 `recordId` 和 `nodeKind`。
- [ ] Assistant 优先使用 `model_call_id`，旧事件回退到 `step_id`；Tool 使用 `call_id`；未知事件保留为 System。
- [ ] 保留原始 `seqId/eventId/type/details`，并让同一模型调用的 reasoning、text、usage 贡献指向同一 Assistant。
- [ ] 增加用户输入和上下文事件的兼容投影；旧 Run 没有这些事件时不伪造内容。

### Task 2: 实现后端节点快照与实时一致性

**Files:**
- Modify: `ksadk/studio/service.py`
- Modify: `ksadk/studio/react-ui/src/pages/trajectory.ts`
- Test: `tests/studio/test_observability_api.py`
- Test: `ksadk/studio/react-ui/src/pages/trajectory.test.ts`

- [ ] 以事件序号排序后重放历史页，形成稳定节点快照；SSE 新事件复用同一 reducer。
- [ ] 节点原位更新，保留首次出现位置；记录组成节点的 source event 序号。
- [ ] 处理重复、缺口、分页拼接、completed 优先、缺 begin/end 和中断终态。
- [ ] 保持现有 API 路径和 SSE cursor，不新增第二份持久化事件表。

### Task 3: 按 Harness 语义重做轨迹 UI

**Files:**
- Modify: `ksadk/studio/react-ui/src/pages/TrajectoryView.tsx`
- Modify: `ksadk/studio/react-ui/src/studio.css`
- Test: `ksadk/studio/react-ui/src/pages/TrajectoryView.test.tsx`

- [ ] 展示 Turn 分组、Input/Model/Tools 时间带和 User/Context/Assistant/Tool 节点。
- [ ] `Duration` 切换真实耗时/等宽时间带，`Turns` 折叠 Turn，`Calls` 折叠 Tool/Subtool。
- [ ] System Events 默认折叠；节点详情提供 Summary、Preview、Raw Events、Source。
- [ ] Usage 不存在时不渲染空 token 列；搜索和选中节点保持稳定。

### Task 4: 迁移校验与回归

**Files:**
- Modify: `docs/superpowers/plans/2026-08-14-local-agent-observability.md`

- [ ] 用真实 SQLite RuntimeEvent 回放验证旧 Run 不丢事实，新 UI 节点数量符合语义粒度。
- [ ] 运行 Python/TypeScript 聚焦测试、格式检查和 UI 构建。
- [ ] 记录旧数据兼容边界：没有 `model_call_id`、user event 或 usage 时显示明确缺失状态。
