from copy import deepcopy
from itertools import count
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ksadk.studio.contracts import RunStatus
from ksadk.studio.run_activity import (
    project_run_activities,
    register_activity_routes,
    safe_activity_url,
)


def run(status=RunStatus.COMPLETED, runtime="harness"):
    return SimpleNamespace(id="run-a", status=status, runtime_type=runtime)


_event_ids = count()


def test_live_invocation_reference_resolves_to_exact_run_not_latest():
    from unittest.mock import AsyncMock, Mock

    from ksadk.studio.shared_web import StudioSharedWebBridge

    studio = SimpleNamespace(
        event_store=SimpleNamespace(get=Mock(return_value=run())),
        run_service=SimpleNamespace(events=AsyncMock(return_value=[])),
    )
    bridge = StudioSharedWebBridge(studio)
    bridge._run_ids_by_invocation["run-client-id"] = "run-a"
    bridge._run_ids_by_invocation["run-other-client"] = "run-b"
    assert bridge.resolve_run_reference("run-client-id") == "run-a"
    assert bridge.resolve_run_reference("unknown") == "unknown"
    app = FastAPI()
    register_activity_routes(app, studio, resolve_run_id=bridge.resolve_run_reference)
    with TestClient(app) as client:
        response = client.get("/api/v1/runs/run-client-id/activities")
    assert response.status_code == 200
    assert response.json()["runId"] == "run-a"
    studio.event_store.get.assert_called_once_with("run-a")
    studio.run_service.events.assert_awaited_once_with("run-a")


def test_workspace_switch_reads_activity_from_original_run_owner():
    from unittest.mock import AsyncMock, Mock

    owner = SimpleNamespace(
        event_store=SimpleNamespace(get=Mock(return_value=run())),
        run_service=SimpleNamespace(events=AsyncMock(return_value=[])),
    )
    active_store = Mock(side_effect=AssertionError("must not use the active workspace"))
    manager = SimpleNamespace(
        event_store=SimpleNamespace(get=active_store),
        runtime_for_run=Mock(return_value=owner),
    )
    app = FastAPI()
    register_activity_routes(app, manager)
    with TestClient(app) as client:
        response = client.get("/api/v1/runs/run-a/activities")
    assert response.status_code == 200
    assert response.json()["runId"] == "run-a"
    manager.runtime_for_run.assert_called_once_with("run-a")
    owner.run_service.events.assert_awaited_once_with("run-a")
    active_store.assert_not_called()


def event(kind, details, *, native_run="run-a", event_id=None, scope="main", ts=None):
    import json

    return {
        "data": {
            "runtimeEvent": {
                "item_kind": "status",
                "event_id": event_id or f"evt-{next(_event_ids)}",
                "run_id": "public-run",
                "scope_id": scope,
                "timestamp": ts,
                "source": {"native_run_id": native_run},
                "snapshot": {"parts": [{"text": json.dumps({"event": kind, "details": details})}]},
            }
        }
    }


def tool(name, call_id, status, **kwargs):
    return event(
        "tool.call.begin" if status == "started" else "tool.call.end",
        {"name": name, "call_id": call_id, "status": status},
        **kwargs,
    )


def codex_item(native_kind, item_id, event_type, *, ts=None):
    """Codex canonical item: native kind lives only in source metadata."""
    return {
        "data": {
            "runtimeEvent": {
                "item_kind": "tool_call" if native_kind != "contextCompaction" else "data",
                "event_type": event_type,
                "event_id": f"codex-{item_id}-{event_type}",
                "run_id": "public-run",
                "scope_id": "codex-scope",
                "timestamp": ts,
                "source": {
                    "framework": "codex",
                    "native_run_id": "codex-thread",
                    "native_item_id": item_id,
                    "metadata": {"native_item_kind": native_kind},
                },
            }
        }
    }


def public_commentary(text, *, event_id="commentary-1", ts=None):
    return {
        "data": {
            "runtimeEvent": {
                "item_kind": "message",
                "event_type": "item.completed",
                "event_id": f"canonical-{event_id}",
                "run_id": "public-run",
                "scope_id": "main",
                "timestamp": ts,
                "source": {
                    "framework": "ksadk",
                    "native_run_id": "run-a",
                    "native_event_id": event_id,
                    "metadata": {
                        "native_event_type": "text.completed",
                        "phase": "commentary",
                    },
                },
                "snapshot": {"parts": [{"text": text}]},
            }
        }
    }


def public_commentary_status(
    text, *, event_id="commentary-status-1", ts=None, native_run="run-a"
):
    import json

    return {
        "data": {
            "runtimeEvent": {
                "item_kind": "status",
                "event_id": f"canonical-{event_id}",
                "run_id": "public-run",
                "scope_id": "main",
                "timestamp": ts,
                "source": {
                    "framework": "ksadk",
                    "native_run_id": native_run,
                    "native_event_id": event_id,
                    "metadata": {
                        "native_event_type": "text.completed",
                        "phase": "commentary",
                    },
                },
                "snapshot": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {"event": "text.completed", "details": {"text": text}}
                            )
                        }
                    ]
                },
            }
        }
    }


def test_groups_real_tool_calls_without_duplicate_begin_end():
    events = [
        tool("web_search", "call-1", "started"),
        tool("web_search", "call-1", "completed"),
        tool("web_search", "call-2", "completed"),
        tool("write_workspace_file", "call-3", "completed"),
    ]
    original = deepcopy(events)
    result = project_run_activities(run(), events)
    assert result["status"] == "completed"
    assert [item["label"] for item in result["activities"]] == ["搜索资料", "编辑并保存文件"]
    assert len(result["activities"][0]["details"]) == 2
    assert result["activities"][0]["status"] == "completed"
    assert events == original


def test_child_ownership_uses_native_run_and_late_lifecycle_label():
    child_run = "run-a:sub:harness-call_child:call_child"
    events = [
        tool("delegate_task", "call_child", "started"),
        tool("web_search", "call-tool", "completed", native_run=child_run, scope="child"),
        tool("web_fetch", "call-read", "completed", native_run=child_run, scope="child"),
        event(
            "run.progress",
            {
                "kind": "subagent.event",
                "call_id": "call_child",
                "label": "调研 Codex",
                "status": "succeeded",
                "provider_ref": "plugin://example",
            },
        ),
    ]
    result = project_run_activities(run(), events)
    assert len(result["activities"]) == 1
    child = result["activities"][0]
    assert child["kind"] == "subagent"
    assert child["label"] == "调研 Codex"
    assert child["status"] == "completed"
    assert [group["label"] for group in child["children"]] == ["搜索资料", "查看资料"]
    assert child["details"] == []
    assert [group["count"] for group in child["children"]] == [1, 1]


def test_missing_completion_is_not_invented_from_run_completion():
    events = [tool("web_search", "call-1", "started")]
    activity = project_run_activities(run(), events)["activities"][0]
    assert activity["status"] == "unknown"
    assert activity["details"][0]["text"].startswith("未确认完成")
    assert (
        project_run_activities(run(RunStatus.RUNNING), events)["activities"][0]["status"]
        == "running"
    )


def test_agent_completion_closes_open_root_model_phase_after_resume():
    events = [
        event("model.call.started", {}, event_id="model-start"),
        event(
            "agent.completed",
            {"agent_id": "agent-a", "status": "completed"},
            event_id="agent-complete",
        ),
    ]

    activity = project_run_activities(run(), events)["activities"][0]

    assert activity["label"] == "分析任务"
    assert activity["status"] == "completed"
    assert activity["details"][0]["text"].startswith("已完成")


def test_cancelled_run_marks_still_open_activity_as_cancelled():
    events = [event("model.call.started", {})]
    activity = project_run_activities(run(RunStatus.CANCELLED), events)["activities"][0]
    assert activity["status"] == "cancelled"
    assert activity["details"][0]["text"].startswith("已取消")


def test_failure_cancel_and_lifecycle_only_child_remain_truthful():
    events = [
        tool("web_fetch", "call-1", "failed"),
        event(
            "run.progress",
            {
                "kind": "subagent.event",
                "call_id": "child-1",
                "label": "调研 ADK",
                "status": "cancelled",
            },
        ),
    ]
    activities = project_run_activities(run(), events)["activities"]
    assert activities[0]["status"] == "failed"
    assert activities[1]["status"] == "cancelled"
    assert activities[1]["children"] == []
    assert activities[1]["details"][0]["text"] == "暂未记录可展示的操作步骤"


def test_allowlisted_public_action_keeps_human_details_without_secrets():
    events = [
        event(
            "tool.call.begin",
            {
                "name": "web_fetch",
                "call_id": "call-1",
                "status": "started",
                "public_action": {
                    "text": (
                        "查看 https://docs.example.com/guide?token=private "
                        "api_key=private password=private"
                    ),
                    "href": "https://docs.example.com/guide?token=private#private",
                },
                "args": {"private": "raw-args-must-not-leak"},
                "output": "raw-output-must-not-leak",
            },
        ),
        tool("web_fetch", "call-1", "completed"),
    ]
    result = project_run_activities(run(), events)
    detail = result["activities"][0]["details"][0]
    assert detail["href"] == "https://docs.example.com/guide"
    assert "docs.example.com/guide" in detail["text"]
    assert "private" not in str(result)
    assert "raw-" not in str(result)


def test_recovered_public_action_does_not_mark_the_group_failed():
    target = {
        "text": "查看网页：docs.example.com/guide",
        "href": "https://docs.example.com/guide",
    }
    events = [
        event(
            "tool.call.begin",
            {
                "name": "web_fetch",
                "call_id": "first-attempt",
                "status": "started",
                "public_action": target,
            },
        ),
        tool("web_fetch", "first-attempt", "failed"),
        event(
            "tool.call.begin",
            {
                "name": "web_fetch",
                "call_id": "retry-attempt",
                "status": "started",
                "public_action": target,
            },
        ),
        tool("web_fetch", "retry-attempt", "completed"),
    ]

    activity = project_run_activities(run(), events)["activities"][0]

    assert activity["status"] == "completed"
    assert activity["count"] == 2
    assert activity["details"][0]["text"].endswith("；后续已成功")
    assert activity["details"][1]["text"].startswith("已完成：")


def test_successful_document_write_recovers_earlier_unpresentable_attempts():
    events = [
        tool("write_workspace_file", "bad-1", "failed", event_id="w1", ts=1.0),
        tool("write_workspace_file", "bad-2", "failed", event_id="w2", ts=2.0),
        event(
            "tool.call.end",
            {
                "name": "write_workspace_file",
                "call_id": "good",
                "status": "completed",
                "public_action": {"text": "保存文件：report.md"},
            },
            event_id="w3",
            ts=3.0,
        ),
    ]

    activity = project_run_activities(run(), events)["activities"][0]

    assert activity["status"] == "completed"
    assert activity["details"][0]["text"].endswith("；后续已成功")
    assert activity["details"][1]["text"].endswith("；后续已成功")
    assert activity["details"][2]["text"] == "已完成：保存文件：report.md"


def test_failed_tool_group_is_partial_while_child_continues_or_completes():
    child_run = "run-a:sub:child-a"
    events = [
        event(
            "run.progress",
            {
                "kind": "delegation.route",
                "call_id": "child-a",
                "label": "调研 ADK",
                "provider_ref": "harness://managed",
            },
            event_id="route",
            ts=1.0,
        ),
        tool(
            "web_search",
            "search-failed",
            "failed",
            native_run=child_run,
            event_id="search-failed",
            ts=2.0,
        ),
        tool(
            "web_fetch",
            "fetch-running",
            "started",
            native_run=child_run,
            event_id="fetch-running",
            ts=3.0,
        ),
    ]

    running_child = project_run_activities(run(RunStatus.RUNNING), events)["activities"][0]
    assert running_child["children"][0]["status"] == "partial"
    assert running_child["children"][0]["details"][0]["text"].startswith("未完成：")

    events.append(
        event(
            "run.progress",
            {"kind": "subagent.event", "call_id": "child-a", "status": "succeeded"},
            event_id="child-complete",
            ts=4.0,
        )
    )
    completed_child = project_run_activities(run(), events)["activities"][0]
    assert completed_child["status"] == "completed"
    assert completed_child["children"][0]["status"] == "partial"


def test_private_reasoning_is_never_used_as_activity_detail():
    result = project_run_activities(
        run(),
        [
            event("reasoning.completed", {"text": "private-thought"}),
            event("text.completed", {"text": "child-output-is-not-progress"}),
            event("model.call.started", {"model": "private-model"}, event_id="m1"),
            event("model.call.completed", {"model": "private-model"}, event_id="m2"),
        ],
    )
    assert result["activities"][0]["label"] == "分析任务"
    assert result["activities"][0]["status"] == "completed"
    assert len(result["activities"][0]["details"]) == 1
    assert "private" not in str(result)
    assert "child-output" not in str(result)


def test_public_commentary_is_a_first_class_chronological_activity():
    activities = project_run_activities(
        run(),
        [
            event("model.call.started", {}, event_id="m1", ts=1.0),
            event("model.call.completed", {}, event_id="m2", ts=2.0),
            public_commentary(
                "我先分别核对三款产品的官方资料。",
                event_id="commentary-plan",
                ts=2.1,
            ),
            tool("web_search", "search-1", "completed", event_id="search", ts=3.0),
            public_commentary(
                "官方资料已经核对完成，接下来统一比较六个维度。",
                event_id="commentary-result",
                ts=4.0,
            ),
        ],
    )["activities"]

    assert [activity["kind"] for activity in activities] == [
        "step",
        "commentary",
        "step",
        "commentary",
    ]
    assert [
        activity.get("text")
        for activity in activities
        if activity["kind"] == "commentary"
    ] == [
        "我先分别核对三款产品的官方资料。",
        "官方资料已经核对完成，接下来统一比较六个维度。",
    ]


def test_status_backed_commentary_renders_without_becoming_a_chat_message():
    activities = project_run_activities(
        run(),
        [public_commentary_status("已完成第一阶段，接下来比较 Checkpoint。", ts=3.0)],
    )["activities"]

    assert activities == [
        {
            "id": "run-a:commentary:commentary-status-1",
            "kind": "commentary",
            "label": "公开进展",
            "status": "completed",
            "text": "已完成第一阶段，接下来比较 Checkpoint。",
            "details": [],
        }
    ]


def test_punctuation_only_commentary_is_omitted_for_old_and_new_event_shapes():
    activities = project_run_activities(
        run(),
        [
            public_commentary("。", event_id="legacy-punctuation", ts=1.0),
            public_commentary_status("...！？", event_id="status-punctuation", ts=2.0),
            public_commentary_status("资料已核对。", event_id="real-progress", ts=3.0),
        ],
    )["activities"]

    assert [activity.get("text") for activity in activities] == ["资料已核对。"]


def test_child_commentary_stays_out_of_parent_timeline_and_is_curated_in_detail():
    from unittest.mock import AsyncMock, Mock

    child_run = "run-a:sub:child-a"
    events = [
        event(
            "run.progress",
            {
                "kind": "delegation.route",
                "call_id": "child-a",
                "label": "调研 Codex",
                "provider_ref": "harness://managed",
            },
            event_id="route",
            ts=1.0,
        ),
        *[
            public_commentary_status(
                text,
                event_id=f"child-progress-{index}",
                ts=float(index + 2),
                native_run=child_run,
            )
            for index, text in enumerate(
                ["开始核对资料。", "正在比较规划。", "正在核对审批。", "资料核对完成。"]
            )
        ],
        event(
            "run.progress",
            {
                "kind": "subagent.event",
                "call_id": "child-a",
                "label": "调研 Codex",
                "status": "succeeded",
            },
            event_id="terminal",
            ts=8.0,
        ),
    ]

    parent = project_run_activities(run(), events)["activities"]
    assert [activity["kind"] for activity in parent] == ["subagent"]
    assert "开始核对资料" not in str(parent)

    studio = SimpleNamespace(
        event_store=SimpleNamespace(get=Mock(return_value=run())),
        run_service=SimpleNamespace(events=AsyncMock(return_value=events)),
    )
    app = FastAPI()
    register_activity_routes(app, studio)
    with TestClient(app) as client:
        facts = client.get("/api/v1/runs/run-a/subagents/child-a").json()["facts"]
    progress = [fact["text"] for fact in facts if fact["kind"] == "commentary"]
    assert progress == ["开始核对资料。", "正在核对审批。", "资料核对完成。"]


def test_subagent_public_conclusion_does_not_duplicate_parent_timeline():
    activities = project_run_activities(
        run(),
        [
            event(
                "run.progress",
                {
                    "kind": "delegation.route",
                    "call_id": "child-a",
                    "label": "调研 Codex",
                    "provider_ref": "harness://managed",
                },
                event_id="route",
                ts=2.0,
            ),
            event(
                "run.progress",
                {
                    "kind": "subagent.event",
                    "call_id": "child-a",
                    "label": "调研 Codex",
                    "status": "succeeded",
                    "public_summary": "Codex 的长任务资料已核对，关键依据来自官方文档。",
                },
                event_id="terminal",
                ts=5.0,
            ),
        ],
    )["activities"]

    assert [(activity["kind"], activity.get("text")) for activity in activities] == [
        ("subagent", None),
    ]


def test_safe_urls_reject_credentials_local_hosts_and_unsafe_schemes():
    for value in [
        "file:///tmp/report.md",
        "javascript:alert(1)",
        "https://user:pass@example.com/x",
        "http://127.0.0.1:8085/x",
        "http://10.1.2.3/x",
        "http://localhost/x",
        "http://host.internal/x",
        "https://docs.example.com:invalid/x",
    ]:
        assert safe_activity_url(value) is None
    assert (
        safe_activity_url("https://docs.example.com/x?q=secret#token")
        == "https://docs.example.com/x"
    )


def test_other_runtimes_and_malformed_records_are_not_interpreted():
    # 统一活动协议后，canonical 执行事实按事件内容解释（不再按 runtime 门禁），
    # 但无法解析的记录仍然必须被忽略。
    assert (
        project_run_activities(run(), [{"data": {}}, {"data": {"runtimeEvent": []}}])["activities"]
        == []
    )
    assert (
        project_run_activities(
            run(),
            [
                {"data": {"runtimeEvent": {"item_kind": "tool_call", "source": {}}}},
                {
                    "data": {
                        "runtimeEvent": {
                            "item_kind": "status",
                            "source": {},
                            "snapshot": {"parts": [{"text": "not-json"}]},
                        }
                    }
                },
            ],
        )["activities"]
        == []
    )


def test_codex_tool_items_project_into_unified_activities():
    events = [
        codex_item("commandExecution", "exec-1", "item.started", ts=100.0),
        codex_item("commandExecution", "exec-1", "item.completed", ts=104.5),
        codex_item("webSearch", "search-1", "item.started", ts=105.0),
        codex_item("fileChange", "patch-1", "item.started", ts=106.0),
        codex_item("fileChange", "patch-1", "item.failed", ts=107.0),
        codex_item("contextCompaction", "compact-1", "item.started", ts=108.0),
        codex_item("contextCompaction", "compact-1", "item.completed", ts=110.0),
    ]
    result = project_run_activities(run(status=RunStatus.RUNNING, runtime="codex"), events)
    labels = {activity["label"]: activity for activity in result["activities"]}
    assert set(labels) == {"运行命令", "搜索资料", "编辑并保存文件", "压缩上下文"}
    assert labels["运行命令"]["status"] == "completed"
    assert labels["运行命令"]["durationMs"] == 4500
    assert labels["搜索资料"]["status"] == "running"
    assert labels["编辑并保存文件"]["status"] == "failed"
    assert labels["压缩上下文"]["durationMs"] == 2000


def test_model_phase_titles_are_derived_from_execution_order():
    events = [
        event("model.call.started", {"model": "m1"}, event_id="a1"),
        event("model.call.completed", {"model": "m1"}, event_id="a2"),
        tool("web_search", "call-1", "completed", event_id="t1"),
        event("model.call.started", {"model": "m1"}, event_id="a3"),
    ]
    result = project_run_activities(run(), events)
    labels = [activity["label"] for activity in result["activities"]]
    assert labels == ["分析任务", "搜索资料", "汇总结果"]


def test_delegation_marks_following_model_phase_as_summary_without_tool_row():
    events = [
        event("model.call.started", {"model": "m1"}, event_id="a1"),
        event("model.call.completed", {"model": "m1"}, event_id="a2"),
        tool("delegate_task", "call-child", "started", event_id="d1"),
        tool("delegate_task", "call-child", "completed", event_id="d2"),
        event("model.call.started", {"model": "m1"}, event_id="a3"),
        event("model.call.completed", {"model": "m1"}, event_id="a4"),
    ]

    activities = project_run_activities(run(), events)["activities"]

    assert [activity["label"] for activity in activities] == ["分析任务", "汇总结果"]
    assert all(activity["label"] != "使用工具处理任务" for activity in activities)


def test_live_activity_keeps_subagents_and_compaction_in_execution_order():
    events = [
        event("model.call.started", {"model": "m1"}, event_id="a1", ts=1.0),
        event("model.call.completed", {"model": "m1"}, event_id="a2", ts=2.0),
        tool("delegate_task", "child-1", "started", event_id="d0", ts=2.5),
        event(
            "run.progress",
            {
                "kind": "delegation.route",
                "call_id": "child-1",
                "label": "调研 Codex",
                "provider_ref": "harness://managed",
            },
            event_id="d1",
            ts=3.0,
        ),
        event(
            "run.progress",
            {"kind": "subagent.event", "call_id": "child-1", "status": "succeeded"},
            event_id="d2",
            ts=4.0,
        ),
        event("context.compaction.started", {}, event_id="c1", ts=5.0),
        event("context.compaction.completed", {}, event_id="c2", ts=6.0),
        event("model.call.started", {"model": "m1"}, event_id="s1", ts=7.0),
    ]

    activities = project_run_activities(run(RunStatus.RUNNING), events)["activities"]

    assert [activity["label"] for activity in activities] == [
        "分析任务",
        "调研 Codex",
        "压缩上下文",
        "汇总结果",
    ]
    assert activities[0]["details"][0]["text"] == "已完成：理解任务并确定执行步骤"
    assert activities[2]["details"][0]["text"] == (
        "已完成：整理并压缩较早的上下文，关键执行状态继续保留"
    )
    assert activities[3]["details"][0]["text"] == "正在：整理已完成的执行结果"


def test_running_tool_and_subagent_durations_grow_with_poll_time(monkeypatch):
    events = [
        tool("web_search", "search-1", "started", event_id="t1", ts=100.0),
        event(
            "run.progress",
            {
                "kind": "delegation.route",
                "call_id": "call-child",
                "label": "调研 Codex",
                "provider_ref": "harness://managed",
            },
            event_id="d1",
            ts=105.0,
        ),
    ]
    monkeypatch.setattr("ksadk.studio.run_activity.time.time", lambda: 115.0)
    first = project_run_activities(run(RunStatus.RUNNING), events)["activities"]
    monkeypatch.setattr("ksadk.studio.run_activity.time.time", lambda: 120.0)
    second = project_run_activities(run(RunStatus.RUNNING), events)["activities"]

    assert first[0]["durationMs"] == 15000
    assert second[0]["durationMs"] == 20000
    assert first[1]["durationMs"] == 10000
    assert second[1]["durationMs"] == 15000


def test_subagent_row_carries_call_id_duration_and_provider():
    child_run = "run-a:sub:call_child"
    events = [
        event(
            "run.progress",
            {
                "kind": "delegation.route",
                "call_id": "call_child",
                "label": "调研 DSH",
                "provider_ref": "codex://app-server",
            },
            event_id="d1",
            ts=10.0,
        ),
        tool(
            "web_search",
            "t1",
            "started",
            native_run=child_run,
            scope="child",
            event_id="t1",
            ts=20.0,
        ),
        tool(
            "web_search",
            "t1",
            "completed",
            native_run=child_run,
            scope="child",
            event_id="t2",
            ts=22.0,
        ),
        event(
            "run.progress",
            {
                "kind": "subagent.event",
                "call_id": "call_child",
                "label": "调研 DSH",
                "status": "succeeded",
            },
            event_id="s1",
            ts=25.0,
        ),
    ]
    result = project_run_activities(run(), events)
    child = result["activities"][0]
    assert child["kind"] == "subagent"
    assert child["callId"] == "call_child"
    assert child["provider"] == "Codex"
    assert child["durationMs"] == 15000
    assert [group.get("durationMs") for group in child["children"]] == [2000]


def test_subagent_detail_endpoint_serves_curated_facts():
    from unittest.mock import AsyncMock, Mock

    child_run = "run-a:sub:call_child"
    events = [
        event(
            "run.progress",
            {
                "kind": "delegation.route",
                "call_id": "call_child",
                "label": "调研 DSH",
                "provider_ref": "codex://app-server",
            },
            event_id="d1",
            ts=10.0,
        ),
        event(
            "model.call.started",
            {"model": "gpt-test"},
            native_run=child_run,
            event_id="m1",
            ts=11.0,
        ),
        tool(
            "web_search",
            "t1",
            "completed",
            native_run=child_run,
            scope="child",
            event_id="t2",
            ts=12.0,
        ),
        event(
            "run.progress",
            {
                "kind": "subagent.event",
                "call_id": "call_child",
                "label": "调研 DSH",
                "status": "succeeded",
            },
            event_id="s1",
            ts=25.0,
        ),
    ]
    studio = SimpleNamespace(
        event_store=SimpleNamespace(get=Mock(return_value=run())),
        run_service=SimpleNamespace(events=AsyncMock(return_value=events)),
    )
    app = FastAPI()
    register_activity_routes(app, studio)
    with TestClient(app) as client:
        response = client.get("/api/v1/runs/run-a/subagents/call_child")
        assert response.status_code == 200
        detail = response.json()
        assert detail["label"] == "调研 DSH"
        assert detail["status"] == "completed"
        assert detail["provider"] == "Codex"
        assert detail["durationMs"] == 15000
        assert detail["models"] == ["gpt-test"]
        assert detail["facts"]
        missing = client.get("/api/v1/runs/run-a/subagents/unknown-call")
        assert missing.status_code == 404


def test_route_returns_projection_and_no_store_without_studio_initialization():
    class RunService:
        async def events(self, run_id):
            assert run_id == "run-a"
            return [tool("read_workspace_file", "call-1", "completed")]

    studio = SimpleNamespace(
        event_store=SimpleNamespace(get=lambda run_id: run()), run_service=RunService()
    )
    app = FastAPI()
    register_activity_routes(app, studio)
    with TestClient(app) as client:
        response = client.get("/api/v1/runs/run-a/activities")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["activities"][0]["label"] == "读取文件"


def public_retry_event(operation, *, native_id="retry-a", native_run="run-a"):
    content = {"text": "模型服务繁忙，2 秒后自动重试（第 2/3 次）"}
    return {
        "data": {
            "runtimeEvent": {
                "item_kind": "message",
                "event_type": f"item.{operation}",
                "event_id": f"retry-{operation}",
                "scope_id": "main",
                "source": {
                    "native_event_id": native_id,
                    "native_run_id": native_run,
                    "metadata": {"native_event_type": "model.call.failed"},
                },
                **(
                    {"update": content}
                    if operation == "updated"
                    else {
                        "initial" if operation == "started" else "snapshot": {"parts": [content]},
                    }
                ),
            }
        }
    }


def test_public_retry_keeps_streaming_progress_and_deduplicates_canonical_triplet():
    events = [event("model.call.started", {}, event_id="model-1")]
    events += [public_retry_event(operation) for operation in ["started", "updated", "completed"]]
    active = project_run_activities(run(RunStatus.RUNNING), events)["activities"]
    assert active[1]["label"] == "自动重试"
    assert active[1]["status"] == "running"
    assert active[1]["count"] == 1
    assert "第 2/3 次" in active[1]["details"][0]["text"]
    events += [
        event("model.call.started", {}, event_id="model-2"),
        event("model.call.completed", {}, event_id="model-3"),
    ]
    complete = project_run_activities(run(), events)["activities"]
    assert complete[0]["status"] == "completed"
    assert "count" not in complete[0]
    assert complete[0]["details"] == [
        {
            "id": "run-a:parent:model:summary",
            "text": "已完成：理解任务并确定执行步骤",
        }
    ]
    assert complete[1]["status"] == "completed"
    assert "重试后已恢复" in complete[1]["details"][0]["text"]


def test_retry_does_not_claim_recovery_from_global_run_completion():
    activities = project_run_activities(run(), [public_retry_event("completed")])["activities"]
    assert activities[0]["status"] == "unknown"
    assert "未记录重试结果" in activities[0]["details"][0]["text"]


def test_structured_retry_never_exposes_error_payload():
    events = [
        event(
            "run.progress",
            {
                "kind": "provider.retry",
                "next_attempt": 2,
                "max_attempts": 10,
                "delay_ms": 3000,
                "error": "authorization=private",
                "label": "private credentials",
            },
        )
    ]
    activity = project_run_activities(run(RunStatus.RUNNING), events)["activities"][0]
    assert activity["label"] == "自动重试"
    assert "3 秒后" in activity["details"][0]["text"]
    assert "第 2/10 次" in activity["details"][0]["text"]
    assert "private" not in str(activity)


def test_duplicate_model_events_do_not_inflate_processing_count():
    start = event("model.call.started", {}, event_id="start-a")
    end = event("model.call.completed", {}, event_id="end-a")
    activity = project_run_activities(run(), [start, start, end, end])["activities"][0]
    assert activity["status"] == "completed"
    assert "count" not in activity
    assert len(activity["details"]) == 1


def test_legacy_repetitions_are_counted_without_hiding_failed_or_cancelled_calls():
    events = [tool("web_fetch", f"call-{index}", "completed") for index in range(47)]
    events += [tool("web_fetch", "failure", "failed"), tool("web_fetch", "cancel", "cancelled")]
    activity = project_run_activities(run(), events)["activities"][0]
    assert activity["count"] == 49
    assert activity["status"] == "failed"
    assert len(activity["details"]) == 3
    assert "已完成 47 次查看资料" in activity["details"][0]["text"]
    assert "未完成" in activity["details"][1]["text"]
    assert "已取消" in activity["details"][2]["text"]


def test_terminal_child_status_does_not_revert_to_running_from_late_notice():
    events = [
        event(
            "run.progress",
            {
                "kind": "subagent.event",
                "call_id": "child-a",
                "label": "调研 ADK",
                "status": state,
            },
        )
        for state in ["succeeded", "running"]
    ]
    assert project_run_activities(run(), events)["activities"][0]["status"] == "completed"


def test_foreign_run_and_session_events_cannot_pollute_activity_counts():
    record = run()
    record.session_id = "session-a"
    own = tool("web_search", "same-call", "completed")
    foreign_run = tool("web_search", "other-call", "completed", native_run="run-b")
    foreign_session = tool("web_search", "same-call", "failed")
    foreign_session["data"]["runtimeEvent"]["source"]["metadata"] = {"session_id": "session-b"}
    result = project_run_activities(record, [own, foreign_run, foreign_session])
    assert len(result["activities"]) == 1
    assert result["activities"][0]["count"] == 1
    assert result["activities"][0]["status"] == "completed"


def test_public_native_run_mapping_and_parent_child_identical_call_ids_remain_separate():
    record = run()
    record.runtime_handle = {"run_id": "native-a"}
    events = [
        tool("web_search", "same-call", "completed", native_run="native-a"),
        tool("web_search", "same-call", "completed", native_run="native-a:sub:child:call-child"),
    ]
    result = project_run_activities(record, events)
    assert result["activities"][0]["count"] == 1
    assert result["activities"][1]["children"][0]["count"] == 1
