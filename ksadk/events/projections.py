"""Canonical 内部事实与公开投影的边界（重设计要素 3/3）。

canonical store（``ksadk/events/canonical_store.py`` 及 ``canonical.py`` 的事件
模型）是唯一的存储事实（append-only、schema_version=2、含内部字段如
``seq``/``run_seq``/``native_*``/``source.metadata``）。所有对外 wire 形态都是
从 canonical 事实派生的**投影**：投影只承诺下表列出的最小公开字段集，除此之外
的字段（含内部序号、native 游标、source 原始 metadata）均属内部不保证字段，
消费方不得依赖。

本模块是边界的**声明处**；契约的**执行形态**是 golden 测试
``tests/protocol/test_cross_projection_golden.py`` 与 fixture
``tests/events/fixtures/runtime_projection_golden.json``。修改任何投影的公开字段
承诺必须同时更新本表与 golden 测试。

投影矩阵（投影 | 实现位置 | 消费方 | 最小公开字段承诺）：

| 投影 | 实现位置 | 消费方 | 公开承诺字段 |
| --- | --- | --- | --- |
| v1 legacy wire | ``events/v1_compat.py::project_to_v1`` | canonical_replay、cli cmd_replay、旧 SDK 客户端 | RuntimeEventV1 事件类型 + 各类型 payload（approval_id/call_id/kind/detail、surface_id/block_id/data、output_refs、status/error/reason）+ 身份字段 run_id/scope_id/item_id |
| Studio 事件流 | ``studio/run_service.py::project_runtime_event`` | 本地/托管 Studio UI（SSE） | ``runId``/``scopeId`` 全事件；message.*/thinking.* 含 ``itemId``/``partId``；tool.*/command.*/approval.*/a2ui.* 含 ``itemId``；a2ui.surface.* 含 ``surfaceId``/``a2uiOperations`` |
| AG-UI/A2UI operations | ``agui/a2ui_projection.py::project_a2ui_operations`` | AG-UI 兼容客户端与 message_projection | 操作列表 ``[{version: "v0.9", createSurface|updateComponents|updateDataModel|deleteSurface: ...}]``，每条含 ``surfaceId`` |
| 会话消息历史 | ``conversations/message_projection.py::project_session_messages`` | server GetSession/ListMessages、hosted UI 历史接口 | 消息 dict：``Role``/``Content.text``/``SeqId``/``StartSeqId``，可选 ``Reasoning``/``ToolEvents``（approval 含 ``ApprovalRequestId``）/``Activities``（含 ``surfaceId``） |
| Responses 历史 | ``conversations/context.py::project_responses_history`` | Responses API 请求回放 input | OpenAI Responses input items（``type``/``call_id``/``output``/role 消息），仅可靠 call_id 的 tool 项 |
| 模型 history | ``conversations/context.py::project_model_messages`` | 运行时模型上下文（内部投喂） | ``role``/``content`` 消息列表；control 事件不进入上下文 |
| server checkpoint payload | ``server/routes/projection.py::_checkpoint_event_to_action_payload`` | REST 断点续跑/预览接口 | ``EventId``/``SessionId``/``RunId``/``CheckpointId``/``Framework``/``FrameworkRef``/``IsResumable``/``ResumeStatus``/``IsTerminal``/``NextNode``（经 ``run_checkpoint`` 元数据或 ``continuation.created`` 投影） |
| server 动作事件 payload | ``server/routes/projection.py::_event_to_action_payload`` | REST 会话动作接口（事件原始形态透传） | ``EventId``/``SessionId``/``Author``/``EventType``/``Content``/``Timestamp``/``SeqId``（可选 ``InvocationId``）——序列化存储形态本身，非 canonical 派生 |

内部不保证（任何投影都不承诺、消费方不得依赖）：
- ``seq``/``run_seq`` 的具体数值与连续性（仅保序语义）；
- ``source.native_event_id``/``native_cursor``/``native_run_id``/``native_item_id``；
- ``source.metadata`` 原始键值（capability 等仅经投影显式提炼后可见）；
- 未列入上表的 payload 附加键。

内部投影（非 wire，不构成公开承诺）：
- ``events/reducer.py::StreamReducer.snapshot`` — 进程内 UI 聚合状态；
- ``events/canonical_replay.py::replay_projection`` — 内部重建 RunProjection 的路径，
  其 v1 输出复用 ``project_to_v1`` 的承诺。
"""

from __future__ import annotations

PROJECTION_CONTRACT_VERSION = 1

#: 各投影实现位置的机器可读索引（供文档/校验工具引用；承诺文本见模块 docstring）。
PROJECTIONS: dict[str, str] = {
    "v1": "ksadk.events.v1_compat:project_to_v1",
    "studio": "ksadk.studio.run_service:project_runtime_event",
    "a2ui": "ksadk.agui.a2ui_projection:project_a2ui_operations",
    "session_messages": "ksadk.conversations.message_projection:project_session_messages",
    "responses_history": "ksadk.conversations.context:project_responses_history",
    "model_messages": "ksadk.conversations.context:project_model_messages",
    "server_checkpoint": "ksadk.server.routes.projection:_checkpoint_event_to_action_payload",
    "server_action_event": "ksadk.server.routes.projection:_event_to_action_payload",
}

#: 仅内部使用的投影（对外无 wire 契约）。
INTERNAL_PROJECTIONS: dict[str, str] = {
    "stream_reducer": "ksadk.events.reducer:StreamReducer",
    "replay_projection": "ksadk.events.canonical_replay:replay_projection",
}

__all__ = [
    "INTERNAL_PROJECTIONS",
    "PROJECTION_CONTRACT_VERSION",
    "PROJECTIONS",
]
