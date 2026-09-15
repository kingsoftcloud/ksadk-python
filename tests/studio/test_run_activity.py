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


def event(kind, details, *, native_run="run-a", event_id=None, scope="main"):
    import json

    return {
        "data": {
            "runtimeEvent": {
                "item_kind": "status",
                "event_id": event_id or f"evt-{next(_event_ids)}",
                "run_id": "public-run",
                "scope_id": scope,
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
    assert result["activities"][0]["label"] == "分析与整理"
    assert result["activities"][0]["status"] == "completed"
    assert len(result["activities"][0]["details"]) == 1
    assert "private" not in str(result)
    assert "child-output" not in str(result)


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
    assert (
        project_run_activities(run(runtime="codex"), [tool("web_search", "x", "completed")])[
            "activities"
        ]
        == []
    )
    assert (
        project_run_activities(run(), [{"data": {}}, {"data": {"runtimeEvent": []}}])["activities"]
        == []
    )


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
    assert complete[0]["count"] == 2
    assert "未完成" in complete[0]["details"][0]["text"]
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
                "delay_ms": 3000,
                "error": "authorization=private",
                "label": "private credentials",
            },
        )
    ]
    activity = project_run_activities(run(RunStatus.RUNNING), events)["activities"][0]
    assert activity["label"] == "自动重试"
    assert "3 秒后" in activity["details"][0]["text"]
    assert "private" not in str(activity)


def test_duplicate_model_events_do_not_inflate_processing_count():
    start = event("model.call.started", {}, event_id="start-a")
    end = event("model.call.completed", {}, event_id="end-a")
    activity = project_run_activities(run(), [start, start, end, end])["activities"][0]
    assert activity["status"] == "completed"
    assert activity["count"] == 1


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
