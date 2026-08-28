"""收口 4：Local Deployment 真实启动 Runtime 子进程 + HTTP Health Check。

不再只是进程内对象与配置级检查：deploy(launch_process=True) spawn
``ksadk.harness.runtime_server``（uvicorn），Health Check 是对 /health 的
真实 HTTP 请求；激活取代/回滚/宿主关闭都真实下线进程。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ksadk.harness.lifecycle import (
    LifecycleError,
    LifecycleStatus,
    LocalLifecycleManager,
)

from .test_lifecycle import _revision_payload


class _OpenAIHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request_payload = json.loads(self.rfile.read(length))
        type(self).requests.append(request_payload)
        response_payload = {
            "id": "chatcmpl-runtime-e2e",
            "object": "chat.completion",
            "created": 1,
            "model": request_payload["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "deployed-runtime-ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }
        body = json.dumps(response_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args):
        return


class _ToolHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    attempts: list[dict] = []
    idempotent_results: dict[str, dict] = {}

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/tool":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        idempotency_key = self.headers.get("Idempotency-Key", "")
        type(self).attempts.append({"payload": payload, "idempotency_key": idempotency_key})
        result = type(self).idempotent_results.get(idempotency_key) if idempotency_key else None
        if result is None:
            type(self).calls.append(payload)
            result = {"status": "paid", "receipt": "receipt-e2e-1"}
            if idempotency_key:
                type(self).idempotent_results[idempotency_key] = result
        body = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args):
        return


def _start_model_server():
    _OpenAIHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _start_tool_server():
    _ToolHandler.calls = []
    _ToolHandler.attempts = []
    _ToolHandler.idempotent_results = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ToolHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _post_json(url: str, payload: dict, *, timeout: float = 15.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_process_exit(process, *, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            return return_code
        time.sleep(0.05)
    raise AssertionError("runtime process did not exit after injected crash")


def test_deploy_spawns_real_runtime_process_with_http_health():
    manager = LocalLifecycleManager()
    manifest = manager.build(
        revision_payload=_revision_payload(),
        revision_ref="agent-revision://proj-1@3",
    )
    deployment = manager.deploy(
        manifest=manifest,
        revision_payload=_revision_payload(),
        route="studio://p1/local",
        launch_process=True,
    )
    try:
        assert deployment.process_mode
        assert deployment.port is not None and deployment.base_url
        # 真实 HTTP：/health 返回 deploymentId。
        with urllib.request.urlopen(f"{deployment.base_url}/health", timeout=3) as r:
            payload = __import__("json").loads(r.read())
        assert payload["status"] == "ok"
        assert payload["deploymentId"] == deployment.deployment_id
        # /manifest 投影 Build Manifest。
        with urllib.request.urlopen(f"{deployment.base_url}/manifest", timeout=3) as r:
            manifest_payload = __import__("json").loads(r.read())
        assert manifest_payload["contentHash"].startswith("sha256:")
        assert deployment.check_health(), "进程形态 Health Check 走真实 HTTP"
    finally:
        manager.close()
    # 关闭后进程退出，HTTP 不再可达 → Health Check 诚实失败。
    assert deployment.http_health_check() is False
    assert deployment.status == LifecycleStatus.RUNTIME_UNHEALTHY


def test_active_process_executes_real_runs_endpoint(monkeypatch):
    """Revision → Build → Deploy → Activate → /runs is a real process boundary."""
    model_server, thread = _start_model_server()
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{model_server.server_port}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder-key")
    monkeypatch.setenv(
        "KSADK_MODEL_PROFILE_MAP",
        json.dumps({"model-profile://kimi-k3@1.0.0": "runtime-fixture-model"}),
    )
    manager = LocalLifecycleManager()
    try:
        manifest = manager.build(
            revision_payload=_revision_payload(),
            revision_ref="agent-revision://proj-runtime@1",
        )
        deployment = manager.deploy(
            manifest=manifest,
            revision_payload=_revision_payload(),
            route="studio://runtime-e2e/local",
            launch_process=True,
        )
        with pytest.raises(LifecycleError, match="no active deployment"):
            manager.invoke_run(
                route="studio://runtime-e2e/local",
                invocation_id="run-before-active",
                input="hello",
                user_id="user-1",
                session_id="session-1",
            )
        manager.activate("studio://runtime-e2e/local")
        result = manager.invoke_run(
            route="studio://runtime-e2e/local",
            invocation_id="run-process-e2e",
            input="hello from lifecycle",
            user_id="user-1",
            session_id="session-1",
        )
        assert result["status"] == "completed"
        assert result["runId"] == "run-process-e2e"
        assert result["events"][-1]["event_type"] == "run.completed"
        usage = [event for event in result["events"] if event["event_type"] == "usage.reported"]
        assert usage[-1]["payload"] == {
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
        }
        assert deployment.invocations == ["run-process-e2e"]
        assert _OpenAIHandler.requests[-1]["model"] == "runtime-fixture-model"

        # Restart uses the same private state directory. The new process is
        # activated again and exposes the previous RuntimeEvent v2 projection.
        previous_base_url = deployment.base_url
        deployment.restart_process()
        assert deployment.base_url != previous_base_url
        assert deployment.status == LifecycleStatus.ACTIVE
        with urllib.request.urlopen(
            f"{deployment.base_url}/runs/run-process-e2e", timeout=3
        ) as response:
            recovered = json.loads(response.read().decode("utf-8"))
        assert recovered == result
        with urllib.request.urlopen(f"{deployment.base_url}/health", timeout=3) as response:
            health = json.loads(response.read().decode("utf-8"))
        assert health["activated"] is True
    finally:
        manager.close()
        model_server.shutdown()
        model_server.server_close()
        thread.join(timeout=3)


def test_high_risk_mcp_approval_survives_runtime_restart_and_executes_once():
    """真实子进程：等待审批 → Runtime 重启 → 批准恢复 → 副作用仅一次。"""
    tool_server, thread = _start_tool_server()
    manager = LocalLifecycleManager()
    payload = _revision_payload()
    payload["capabilities"] = {
        "mcpBindings": [{"bindingRef": "mcp://finance-tools@1.0.0"}]
    }
    command = [
        sys.executable,
        "-m",
        "tests.harness.runtime_approval_fixture",
        "--spec-file",
        "{spec_file}",
        "--route",
        "{route}",
        "--deployment-id",
        "{deployment_id}",
        "--port",
        "{port}",
        "--build-id",
        "{build_id}",
        "--content-hash",
        "{content_hash}",
        "--state-dir",
        "{state_dir}",
        "--tool-url",
        f"http://127.0.0.1:{tool_server.server_port}/tool",
    ]
    try:
        manifest = manager.build(
            revision_payload=payload,
            revision_ref="agent-revision://approval-process-e2e@1",
        )
        deployment = manager.deploy(
            manifest=manifest,
            revision_payload=payload,
            route="studio://approval-process-e2e/local",
            launch_process=True,
            server_command=command,
        )
        manager.activate("studio://approval-process-e2e/local")
        interrupted = manager.invoke_run(
            route="studio://approval-process-e2e/local",
            invocation_id="run-approval-process-e2e",
            input="支付发票 INV-E2E-1，金额 88 元",
            user_id="user-e2e",
            session_id="session-e2e",
        )
        assert interrupted["status"] == "awaiting_approval"
        assert _ToolHandler.calls == [], "审批前绝不能触达真实 Tool"
        requested = [
            event
            for event in interrupted["events"]
            if event["event_type"] == "approval.requested"
        ]
        assert requested[-1]["payload"]["detail"]["args"]["tool_name"] == "pay_invoice"

        old_base_url = deployment.base_url
        deployment.restart_process()
        assert deployment.base_url != old_base_url

        completed = _post_json(
            f"{deployment.base_url}/runs/run-approval-process-e2e:resume",
            {"decision": "approved", "callId": "pay-invoice", "stream": False},
            timeout=30,
        )
        assert completed["status"] == "completed"
        assert _ToolHandler.calls == [
            {
                "name": "pay_invoice",
                "arguments": {"invoice_id": "INV-E2E-1", "amount": 88},
            }
        ]
        event_types = [event["event_type"] for event in completed["events"]]
        assert "approval.resolved" in event_types
        assert "run.resumed" in event_types
        assert event_types[-1] == "run.completed"

        # Terminal Run 已移除可恢复 Handle；重复批准被拒且不会重放副作用。
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post_json(
                f"{deployment.base_url}/runs/run-approval-process-e2e:resume",
                {"decision": "approved", "callId": "pay-invoice"},
            )
        assert exc_info.value.code == 404
        assert len(_ToolHandler.calls) == 1
    finally:
        manager.close()
        tool_server.shutdown()
        tool_server.server_close()
        thread.join(timeout=3)


def test_tool_receipt_replays_after_crash_before_graph_checkpoint(tmp_path):
    """Tool 已成功且 Receipt 已落盘，Graph 写回前崩溃；恢复不得重复副作用。"""
    tool_server, thread = _start_tool_server()
    manager = LocalLifecycleManager()
    payload = _revision_payload()
    payload["capabilities"] = {
        "mcpBindings": [{"bindingRef": "mcp://finance-tools@1.0.0"}]
    }
    crash_marker = tmp_path / "crash-after-receipt.marker"
    command = [
        sys.executable,
        "-m",
        "tests.harness.runtime_approval_fixture",
        "--spec-file",
        "{spec_file}",
        "--route",
        "{route}",
        "--deployment-id",
        "{deployment_id}",
        "--port",
        "{port}",
        "--build-id",
        "{build_id}",
        "--content-hash",
        "{content_hash}",
        "--state-dir",
        "{state_dir}",
        "--tool-url",
        f"http://127.0.0.1:{tool_server.server_port}/tool",
        "--crash-after-receipt",
        str(crash_marker),
    ]
    try:
        manifest = manager.build(
            revision_payload=payload,
            revision_ref="agent-revision://receipt-crash-e2e@1",
        )
        deployment = manager.deploy(
            manifest=manifest,
            revision_payload=payload,
            route="studio://receipt-crash-e2e/local",
            launch_process=True,
            server_command=command,
        )
        manager.activate("studio://receipt-crash-e2e/local")
        interrupted = manager.invoke_run(
            route="studio://receipt-crash-e2e/local",
            invocation_id="run-receipt-crash-e2e",
            input="支付发票 INV-E2E-1，金额 88 元",
            user_id="user-e2e",
            session_id="session-e2e",
        )
        assert interrupted["status"] == "awaiting_approval"

        crashing_process = deployment.process
        assert crashing_process is not None
        with pytest.raises((LifecycleError, urllib.error.URLError, ConnectionError)):
            _post_json(
                f"{deployment.base_url}/runs/run-receipt-crash-e2e:resume",
                {"decision": "approved", "callId": "pay-invoice", "stream": False},
                timeout=30,
            )
        assert _wait_for_process_exit(crashing_process) == 91
        assert crash_marker.read_text(encoding="utf-8") == "receipt-committed"
        assert len(_ToolHandler.calls) == 1

        deployment.restart_process()
        completed = _post_json(
            f"{deployment.base_url}/runs/run-receipt-crash-e2e:resume",
            {"decision": "approved", "callId": "pay-invoice", "stream": False},
            timeout=30,
        )
        assert completed["status"] == "completed"
        assert len(_ToolHandler.calls) == 1, "恢复必须回放 Receipt，不得重复外部副作用"
        replayed = [
            event
            for event in completed["events"]
            if event["event_type"] == "tool.call.end"
            and event["payload"].get("replayed") is True
        ]
        assert replayed
        assert completed["events"][-1]["event_type"] == "run.completed"
    finally:
        manager.close()
        tool_server.shutdown()
        tool_server.server_close()
        thread.join(timeout=3)


def test_transport_idempotency_closes_crash_before_receipt_window(tmp_path):
    """外部成功但 Receipt 前崩溃：恢复会重试请求，业务副作用仍只有一次。"""
    tool_server, thread = _start_tool_server()
    manager = LocalLifecycleManager()
    payload = _revision_payload()
    payload["capabilities"] = {
        "mcpBindings": [{"bindingRef": "mcp://finance-tools@1.0.0"}]
    }
    crash_marker = tmp_path / "crash-before-receipt.marker"
    command = [
        sys.executable,
        "-m",
        "tests.harness.runtime_approval_fixture",
        "--spec-file",
        "{spec_file}",
        "--route",
        "{route}",
        "--deployment-id",
        "{deployment_id}",
        "--port",
        "{port}",
        "--build-id",
        "{build_id}",
        "--content-hash",
        "{content_hash}",
        "--state-dir",
        "{state_dir}",
        "--tool-url",
        f"http://127.0.0.1:{tool_server.server_port}/tool",
        "--idempotent-transport",
        "--crash-before-receipt",
        str(crash_marker),
    ]
    try:
        manifest = manager.build(
            revision_payload=payload,
            revision_ref="agent-revision://idempotency-crash-e2e@1",
        )
        deployment = manager.deploy(
            manifest=manifest,
            revision_payload=payload,
            route="studio://idempotency-crash-e2e/local",
            launch_process=True,
            server_command=command,
        )
        manager.activate("studio://idempotency-crash-e2e/local")
        interrupted = manager.invoke_run(
            route="studio://idempotency-crash-e2e/local",
            invocation_id="run-idempotency-crash-e2e",
            input="支付发票 INV-E2E-1，金额 88 元",
            user_id="user-e2e",
            session_id="session-e2e",
        )
        assert interrupted["status"] == "awaiting_approval"

        crashing_process = deployment.process
        assert crashing_process is not None
        with pytest.raises((LifecycleError, urllib.error.URLError, ConnectionError)):
            _post_json(
                f"{deployment.base_url}/runs/run-idempotency-crash-e2e:resume",
                {"decision": "approved", "callId": "pay-invoice", "stream": False},
                timeout=30,
            )
        assert _wait_for_process_exit(crashing_process) == 92
        assert crash_marker.read_text(encoding="utf-8") == "external-success-before-receipt"
        assert len(_ToolHandler.calls) == 1
        assert len(_ToolHandler.attempts) == 1
        first_key = _ToolHandler.attempts[0]["idempotency_key"]
        assert first_key.startswith("ksadk-")

        deployment.restart_process()
        completed = _post_json(
            f"{deployment.base_url}/runs/run-idempotency-crash-e2e:resume",
            {"decision": "approved", "callId": "pay-invoice", "stream": False},
            timeout=30,
        )
        assert completed["status"] == "completed"
        assert len(_ToolHandler.attempts) == 2, "Receipt 前崩溃后 Harness 必须安全重试"
        assert _ToolHandler.attempts[1]["idempotency_key"] == first_key
        assert len(_ToolHandler.calls) == 1, "远端按稳定幂等键去重，业务副作用只能发生一次"
        assert completed["events"][-1]["event_type"] == "run.completed"
    finally:
        manager.close()
        tool_server.shutdown()
        tool_server.server_close()
        thread.join(timeout=3)


def test_activate_uses_real_http_and_supersede_terminates_old_process():
    manager = LocalLifecycleManager()
    first = manager.build(
        revision_payload=_revision_payload(),
        revision_ref="agent-revision://proj-1@3",
    )
    first_dep = manager.deploy(
        manifest=first,
        revision_payload=_revision_payload(),
        route="studio://p1/local",
        launch_process=True,
    )
    manager.activate("studio://p1/local")
    assert first_dep.status == LifecycleStatus.ACTIVE
    assert first_dep.process_mode

    second = manager.build(
        revision_payload=_revision_payload(),
        revision_ref="agent-revision://proj-1@4",
    )
    second_dep = manager.deploy(
        manifest=second,
        revision_payload=_revision_payload(),
        route="studio://p1/local",
        launch_process=True,
    )
    manager.activate("studio://p1/local")
    try:
        assert second_dep.status == LifecycleStatus.ACTIVE
        assert second_dep.check_health()
        # 旧 Active 被取代：进程真实下线。
        assert first_dep.status == LifecycleStatus.SUPERSEDED
        assert first_dep.process is None
        assert not first_dep.http_health_check()
    finally:
        manager.close()


def test_rollback_terminates_runtime_process():
    manager = LocalLifecycleManager()
    manifest = manager.build(
        revision_payload=_revision_payload(),
        revision_ref="agent-revision://proj-1@3",
    )
    manager.deploy(
        manifest=manifest,
        revision_payload=_revision_payload(),
        route="studio://p1/local",
        launch_process=True,
    )
    manager.activate("studio://p1/local")
    rolled = manager.rollback("studio://p1/local")
    assert rolled.status == LifecycleStatus.ROLLED_BACK
    assert rolled.process is None, "回滚必须真实下线 Runtime 子进程"


def test_deploy_fails_honestly_when_process_dies_immediately():
    """进程立即退出 → 诚实 DEPLOY_FAILED，而非伪装部署成功。"""
    manager = LocalLifecycleManager()
    manifest = manager.build(
        revision_payload=_revision_payload(),
        revision_ref="agent-revision://proj-1@3",
    )
    with pytest.raises(LifecycleError, match="health check 未通过"):
        manager.deploy(
            manifest=manifest,
            revision_payload=_revision_payload(),
            route="studio://p1/local",
            launch_process=True,
            health_timeout=5.0,
            server_command=[sys.executable, "-c", "raise SystemExit(3)"],
        )


def test_deploy_fails_honestly_when_health_never_passes():
    """进程活着但不监听端口 → 超时后诚实失败并清理。"""
    manager = LocalLifecycleManager()
    manifest = manager.build(
        revision_payload=_revision_payload(),
        revision_ref="agent-revision://proj-1@3",
    )
    with pytest.raises(LifecycleError, match="health check"):
        manager.deploy(
            manifest=manifest,
            revision_payload=_revision_payload(),
            route="studio://p1/local",
            launch_process=True,
            health_timeout=1.5,
            server_command=[sys.executable, "-c", "import time; time.sleep(60)"],
        )
