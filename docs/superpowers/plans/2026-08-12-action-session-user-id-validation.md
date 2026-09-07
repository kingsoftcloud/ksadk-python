# Action Session User ID 校验调整实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 允许 action API 在请求 `UserId` 与 session 记录的 user id 不同时，仍按 `SessionId` 和 `AgentId` 访问该 session。

**Architecture:** 保留 `_require_action_session` 的现有调用签名，仅从其拒绝条件中移除 user id 比较。通过 `ListSessionEvents` 的真实 API 测试覆盖该行为，同时保留 agent id 和 session 存在性校验。

**Tech Stack:** Python、FastAPI、pytest、httpx、uv

## Global Constraints

- 不删除请求模型或调用链中的 `UserId` 字段。
- 不放宽 `agent_id` 匹配规则。
- 不调整无 `SessionId` 场景下按 user id 过滤列表结果的行为。
- 不触碰工作区中与本任务无关的未跟踪文档。

---

### Task 1: 放宽 action session 的 user id 校验

**Files:**
- Modify: `tests/test_server_session_app.py:6048`
- Modify: `tests/test_unified_agent_ui_local.py:1165`
- Modify: `ksadk/server/routes/projection.py:35`

**Interfaces:**
- Consumes: `_require_action_session(service, *, session_id: str, agent_id: Optional[str] = None, user_id: Optional[str] = None) -> Session`
- Produces: 相同函数签名；`user_id` 不再参与拒绝判断。

- [x] **Step 1: 写入失败测试**

将 `test_list_session_events_rejects_mismatched_session_user` 改名为 `test_list_session_events_allows_mismatched_session_user`。为 session 追加一个真实 `user_message` 事件，请求携带不同的 `UserId`，并断言 HTTP 200、事件总数为 1、返回的 `SessionId` 为 `sess-owned-by-b`。同步更新直接依赖共享 helper 旧 user scope 契约的 checkpoint action、订阅事件与本地消息列表测试：不同 user id 应成功，错误 agent id 仍应返回 404。

- [x] **Step 2: 运行测试并确认按预期失败**

运行：

```bash
uv run pytest tests/test_server_session_app.py::test_list_session_events_allows_mismatched_session_user -q
```

预期：旧实现返回 404，测试在 `assert response.status_code == 200` 处失败。

- [x] **Step 3: 写入最小实现**

在 `_require_action_session` 中删除：

```python
or (user_id is not None and session.user_id != user_id)
```

并将 docstring 调整为只声明解析 session 以及校验 agent 范围，不改变函数签名和调用方。

- [x] **Step 4: 运行针对性测试并确认通过**

运行：

```bash
uv run pytest tests/test_server_session_app.py::test_list_session_events_allows_mismatched_session_user tests/test_server_session_app.py::test_list_session_events_without_session_id_filters_total_by_user -q
```

预期：两项测试均通过，证明指定 session 放宽 user id，而无 session 的 user 过滤保持不变。

- [x] **Step 5: 运行受影响测试文件**

运行：

```bash
uv run pytest tests/test_server_session_app.py -q
```

预期：测试文件全部通过；如环境缺少外部条件，记录准确的失败原因，并至少确保与 `_require_action_session` 相关的测试通过。

- [x] **Step 6: 检查差异并提交**

只暂存本任务的实现、测试和实施计划：

```bash
git diff --check
git diff -- ksadk/server/routes/projection.py tests/test_server_session_app.py tests/test_unified_agent_ui_local.py docs/superpowers/plans/2026-08-12-action-session-user-id-validation.md
git add ksadk/server/routes/projection.py tests/test_server_session_app.py tests/test_unified_agent_ui_local.py docs/superpowers/plans/2026-08-12-action-session-user-id-validation.md
git commit -m "fix(server): 放宽 action session user id 校验"
```

## 验证记录

- RED：旧实现下 `test_list_session_events_allows_mismatched_session_user` 返回 404，按预期失败。
- GREEN：相关 server 路由和本地消息列表测试共 134 项通过。
- `tests/test_server_session_app.py`：133 项通过。
- 全量 `uv run pytest -q`：2479 项通过、13 项跳过、7 项失败；更新另一项相关旧 user scope 测试后，剩余 6 项失败均为 `cmd_web` checkpoint 环境变量清理测试，与本次 action session 调用链无关，并可在 `tests/test_unified_agent_ui_local.py` 中单独复现。
- `ruff check`：本次涉及的三个 Python 文件通过。
- `ruff format --check`：三个既有文件均被当前 formatter 判定需要全文件格式化；为避免引入大量无关 diff，本次未执行机械重排。
- `git diff --check`：通过。
