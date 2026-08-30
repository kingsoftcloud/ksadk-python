# Studio 评测列表与详情页改造计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建评测后立即返回可恢复的任务列表，并用独立详情页承载运行进度、报告、Case 和 TraceRef。

**Architecture:** 复用现有 `OperationManager` 作为任务状态来源，复用 `EvaluationStorage` 作为完成/部分报告来源；Studio API 只增加面向评测运行的聚合读模型。前端沿用现有 hash 导航和组件库，不引入 Router、状态库或第二套任务持久化。

**Tech Stack:** FastAPI、Pydantic、React 19、TypeScript、Radix Drawer、TanStack Table、Vitest、pytest。

---

## 设计基线

- 页面类型：面向开发者的 B2B Agent 评测工作台。
- 设计参数：`DESIGN_VARIANCE: 3`、`MOTION_INTENSITY: 2`、`VISUAL_DENSITY: 8`。
- `/evaluations` 是任务列表和监控面，不再同时展开完整报告。
- `#/evaluations/<runId>` 是可刷新、可返回的详情深链。
- “新建评测”保留在列表页右侧 Drawer，不增加创建二级页。
- 列表统一展示 `QUEUED / RUNNING / PASSED / FAILED / ERROR / CANCELLED / INTERRUPTED`。
- 运行中详情展示任务进度；报告存在后展示结果，不能把“报告尚未生成”当作 404 错误。

## 最小数据契约

新增 Studio 专用读模型，不修改 `EvalRunReport` 的共享 CLI/Studio 持久化格式：

```json
{
  "id": "eval_xxx",
  "operationId": "op_xxx",
  "status": "RUNNING",
  "createdAt": "2026-08-18T10:00:00Z",
  "completedAt": null,
  "evalset": { "name": "a2a-smoke", "caseCount": 5 },
  "target": { "kind": "a2a", "label": "A2A Agent" },
  "evaluators": ["response_contract@v1"],
  "progress": { "current": 2, "total": 5, "caseId": "arithmetic" },
  "summary": null,
  "hasReport": false,
  "error": null
}
```

接口：

- `GET /api/v1/evaluation-runs`：按创建时间倒序返回 operation 与 report 的合并摘要。
- `GET /api/v1/evaluation-runs/{run_id}`：返回同一摘要；`hasReport=true` 时附带或并行读取现有 report。
- 现有 `GET /api/v1/evaluations` 和 `GET /api/v1/evaluations/{id}` 暂时保留，避免破坏报告消费者。
- 现有 operation 查询、事件和取消接口继续复用。

## Task 1：补齐可持久化的评测任务摘要

**Files:**

- Modify: `ksadk/studio/contracts.py`
- Modify: `ksadk/studio/operations.py`
- Modify: `ksadk/studio/service.py`
- Test: `tests/studio/test_operations.py`
- Test: `tests/studio/test_evaluation_shell.py`

- [ ] **Step 1: 写失败测试**

覆盖 `OperationManager.list(kind=EVALUATION)` 的倒序、损坏记录跳过，以及评测 operation 在重启后仍保留安全摘要。摘要只包含 EvalSet 名称/Case 数、Target kind/显示名和 evaluator IDs，不持久化 token、完整 URL 查询参数或凭证。

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/studio/test_operations.py tests/studio/test_evaluation_shell.py -q
```

- [ ] **Step 3: 最小实现**

给 `Operation` 增加默认空的 `metadata`，给 `OperationManager.submit()` 增加可选 metadata，并新增按 kind 过滤的 `list()`。`submit_public_evaluation()` 在创建 operation 时写入安全评测摘要；不创建新的 run 文件。

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/studio/test_operations.py tests/studio/test_evaluation_shell.py -q
```

**Exit condition:** 新建任务后，即使报告尚不存在或 Studio 页面刷新，也能从 operation 恢复 EvalSet、Target、evaluators 和任务状态。

## Task 2：增加评测运行聚合 API

**Files:**

- Modify: `ksadk/studio/api_contracts.py`
- Modify: `ksadk/studio/service.py`
- Modify: `ksadk/studio/api.py`
- Test: `tests/studio/test_evaluation_shell.py`
- Test: `tests/studio/test_api.py`

- [ ] **Step 1: 写失败测试**

覆盖 queued/running/completed/failed/cancelled/interrupted 六类 operation；验证进度取最后一个 `evaluation.case.started` 事件，完成报告存在时合并 `summary`，报告缺失时返回 `hasReport=false` 而不是 404。

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/studio/test_evaluation_shell.py tests/studio/test_api.py -q
```

- [ ] **Step 3: 最小实现**

在 `StudioService` 中聚合 operation、events 和可选 report；API 只负责序列化。状态映射集中在服务层：operation 未完成时用 operation 状态，完成后优先使用 report 的 `PASSED/FAILED/ERROR/CANCELLED`。

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/studio/test_evaluation_shell.py tests/studio/test_api.py -q
```

**Exit condition:** 单个列表接口可完整呈现运行中和历史任务，详情深链可在任意状态打开。

## Task 3：把创建交互改为提交即返回

**Files:**

- Modify: `ksadk/studio/react-ui/src/pages/EvaluationsPage.tsx`
- Modify: `ksadk/studio/react-ui/src/pages/evaluations.css`
- Test: `ksadk/studio/react-ui/src/pages/EvaluationsPage.test.tsx`

- [ ] **Step 1: 写失败测试**

验证提交 `POST /api/v1/evaluations` 收到 202 后立即关闭 Drawer、插入/刷新 queued 行，不在创建表单里等待 operation terminal；提交失败保留表单和字段错误。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ksadk/studio/react-ui && npm run test:ui -- EvaluationsPage.test.tsx
```

- [ ] **Step 3: 最小实现**

复用现有 `Drawer`、`FormField`、`StudioSelect` 和 evaluator checkbox。删除创建流程中的 `waitForOperation()`；POST 成功后 toast 使用“评测任务已创建”，列表轮询负责后续状态。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd ksadk/studio/react-ui && npm run test:ui -- EvaluationsPage.test.tsx
```

**Exit condition:** 用户创建任务后可以立即继续浏览、创建第二个任务或离开页面，后台执行不受组件卸载影响。

## Task 4：将 `/evaluations` 收敛为任务列表

**Files:**

- Modify: `ksadk/studio/react-ui/src/pages/EvaluationsPage.tsx`
- Modify: `ksadk/studio/react-ui/src/pages/evaluations.css`
- Test: `ksadk/studio/react-ui/src/pages/EvaluationsPage.test.tsx`

- [ ] **Step 1: 写失败测试**

覆盖状态筛选、刷新、空态、错误态、运行中进度、失败原因和行键盘激活。验证聚合指标不把 queued/running 当作已完成报告。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ksadk/studio/react-ui && npm run test:ui -- EvaluationsPage.test.tsx
```

- [ ] **Step 3: 最小实现**

列表列建议固定为：`EvalSet / Run`、`Target`、`状态`、`进度/通过 Case`、`创建时间`、`耗时`。移除当前右侧内嵌报告 workbench；保留一层表格，不堆叠卡片。

轮询策略：页面可见且存在 queued/running 时每 1 秒刷新；全部终态后停止；`visibilitychange` 恢复时立即刷新；请求用 sequence/AbortController 防止旧响应覆盖新状态。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd ksadk/studio/react-ui && npm run test:ui -- EvaluationsPage.test.tsx
```

**Exit condition:** 刷新浏览器后运行中任务仍在列表，任务终态后自动停止轮询，移动端表格可横向浏览且无内容重叠。

## Task 5：增加可深链的评测详情页

**Files:**

- Create: `ksadk/studio/react-ui/src/pages/EvaluationDetailPage.tsx`
- Modify: `ksadk/studio/react-ui/src/App.tsx`
- Modify: `ksadk/studio/react-ui/src/pages/evaluations.css`
- Test: `ksadk/studio/react-ui/src/pages/EvaluationDetailPage.test.tsx`
- Test: `ksadk/studio/react-ui/src/evaluationRoute.contract.test.mjs`

- [ ] **Step 1: 写失败测试**

验证 `#/evaluations/<runId>` 刷新后仍打开详情、返回按钮回到列表；queued/running 展示进度和取消，failed/interrupted 展示错误，完成后展示 Target Snapshot、汇总、Case 列表和 Case 详情。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ksadk/studio/react-ui && npm run test:ui -- EvaluationDetailPage.test.tsx
npm test -- src/evaluationRoute.contract.test.mjs
```

- [ ] **Step 3: 最小实现**

沿用 `App.tsx` 的 hash 导航，扩展为解析 view 与可选 runId，不安装 React Router。详情页复用当前评测页已有的 snapshot、case list、metric evidence 和 output 结构；TraceRef 有 trace ID 时提供跳转到可观测页的命令入口。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd ksadk/studio/react-ui && npm run test:ui -- EvaluationDetailPage.test.tsx EvaluationsPage.test.tsx
npm test
```

**Exit condition:** 详情可直接打开和刷新；运行中可监控/取消；报告生成后原地切换为结果视图，不需要返回列表再进入。

## Task 6：集成与视觉验证

**Files:**

- Modify only if defects are found: `ksadk/studio/react-ui/src/pages/evaluations.css`
- Test: `tests/studio/test_evaluation_shell.py`
- Test: `ksadk/studio/react-ui/src/pages/EvaluationsPage.test.tsx`
- Test: `ksadk/studio/react-ui/src/pages/EvaluationDetailPage.test.tsx`

- [ ] **Step 1: 跑相关后端与前端测试**

```bash
uv run pytest tests/studio/test_operations.py tests/studio/test_evaluation_shell.py tests/studio/test_api.py -q
cd ksadk/studio/react-ui && npm run test:ui && npm test && npm run build
```

- [ ] **Step 2: 跑真实 Studio smoke**

至少创建一个多 Case A2A 或 Studio Build 评测，验证：提交立即返回、刷新恢复、进度更新、取消保留部分报告、失败详情、终态停止轮询、TraceRef 跳转。

- [ ] **Step 3: 做桌面和移动端视觉检查**

检查 1440x900、1024x768、390x844：无双层卡片、无文本截断导致信息不可辨、状态不只依赖颜色、键盘可进入列表行、Drawer 可关闭并恢复焦点、`prefers-reduced-motion` 下无多余动画。

**Exit condition:** 测试、构建和真实任务 smoke 均通过；不能跑 A2A E2E 时记录缺失 URL/凭证/上游服务，不用“应该可用”代替结果。

## 分期建议

1. **MVP（Task 1-4）**：先解决创建后阻塞、列表缺少运行中任务、刷新丢状态，预计 2-3 个开发日。
2. **详情闭环（Task 5）**：增加深链详情和运行中/结果双态，预计 1-2 个开发日。
3. **验证收口（Task 6）**：E2E、移动端与可访问性检查，预计 0.5-1 个开发日。

暂不加入：重试/克隆任务、任务比较、批量操作、服务端分页、WebSocket/SSE 常驻连接、趋势图。等本地运行量或真实使用反馈证明需要时再加。
