"""服务端 Build → Deploy → Activate → Invoke → Rollback 联合 E2E。

真实进程形态：模型请求打到本地 mock 网关，Runtime 是真实 spawn 的
uvicorn 子进程，Conformance 全链路走 ``run_lifecycle_conformance``。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ksadk.harness.lifecycle import LocalLifecycleManager
from ksadk.harness.lifecycle_conformance import (
    LifecycleConformanceCase,
    run_lifecycle_conformance,
)
from ksadk.harness.lifecycle_local_plane import LocalLifecycleControlPlane


class _OpenAIHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        request_payload = json.loads(self.rfile.read(length))
        response_payload = {
            "id": "chatcmpl-conformance-e2e",
            "object": "chat.completion",
            "created": 1,
            "model": request_payload["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "conformance-ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }
        body = json.dumps(response_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args):
        return


def _revision_payload(name: str = "finance-analyst") -> dict:
    return {
        "role": {
            "name": name,
            "objective": "财务分析",
            "instructionsRef": "skill://finance-instructions@1",
        },
        "model": {
            "profileRef": "model-profile://kimi-k3@1.0.0",
            "fallbackProfileRefs": ["model-profile://glm@1.0.0"],
        },
    }


def test_full_server_lifecycle_joint_e2e(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder-key")
    monkeypatch.setenv(
        "KSADK_MODEL_PROFILE_MAP",
        json.dumps({"model-profile://kimi-k3@1.0.0": "conformance-fixture-model"}),
    )

    payloads = {
        "agent-revision://conformance-e2e@1": _revision_payload("finance-analyst"),
        "agent-revision://conformance-e2e@2": _revision_payload("finance-analyst-v2"),
    }
    manager = LocalLifecycleManager()
    try:
        plane = LocalLifecycleControlPlane(
            manager=manager,
            revision_payload_resolver=lambda ref: payloads[ref],
        )
        report = __import__("asyncio").run(
            run_lifecycle_conformance(
                control_plane_id="local-process-plane",
                control_plane=plane,
                case=LifecycleConformanceCase(
                    revision_ref="agent-revision://conformance-e2e@2",
                    route="studio://conformance-e2e/local",
                    invocation_id="run-conformance-joint-e2e",
                    rollback_revision_ref="agent-revision://conformance-e2e@1",
                ),
            )
        )

        assert report.status == "ready", report.to_dict()
        rules = {item.rule: item.status for item in report.findings}
        assert rules["revision.immutable"] == "passed"
        assert rules["build.revision_pinned"] == "passed"
        assert rules["deploy.build_pinned"] == "passed"
        assert rules["deployment.approved"] == "passed"
        assert rules["route.activated"] == "passed"
        assert rules["route.invoke_effective_revision"] == "passed"
        assert rules["route.rollback_effective_revision"] == "passed"

        # 回滚后 Route 真实指向 v1 进程，且能继续 Invoke。
        rollback_result = __import__("asyncio").run(
            plane.invoke("studio://conformance-e2e/local", "run-after-rollback")
        )
        assert rollback_result["revisionRef"] == "agent-revision://conformance-e2e@1"
        assert rollback_result["status"] == "completed"
    finally:
        manager.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_local_plane_reports_not_configured_when_plane_absent():
    report = __import__("asyncio").run(
        run_lifecycle_conformance(
            control_plane_id="remote-plane",
            control_plane=None,
            case=LifecycleConformanceCase(
                revision_ref="agent-revision://x@1",
                route="studio://x/prod",
                invocation_id="run-x",
            ),
        )
    )
    assert report.status == "not_configured"
    assert report.findings[0].status == "skipped"


@pytest.mark.parametrize(
    ("status_value", "expected"),
    [("deployed", "approved"), ("active", "approved"), ("failed_build", "rejected")],
)
def test_local_plane_approval_follows_deployment_state(status_value, expected):
    class _FakeDeployment:
        class _Status:
            value = status_value

        status = _Status()

    from ksadk.harness.lifecycle_local_plane import LocalLifecycleControlPlane as Plane

    plane = Plane.__new__(Plane)
    plane._deployments = {"dep-1": _FakeDeployment()}
    import asyncio

    result = asyncio.run(plane.approve("dep-1"))
    assert result["status"] == expected
