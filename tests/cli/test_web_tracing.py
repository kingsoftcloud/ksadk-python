"""Web startup must enable tracing without initialization in customer code."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


@pytest.mark.parametrize("tracing_failure", [False, True], ids=["dual-write", "init-failure"])
def test_web_tracing_startup(tmp_path: Path, tracing_failure: bool):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            request = ExportTraceServiceRequest()
            request.ParseFromString(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, {key.lower(): value for key, value in self.headers.items()}, request))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    project = tmp_path / "agent-project"
    project.mkdir()
    (project / "agentengine.yaml").write_text(
        "name: web-tracing-test\nframework: langgraph\n"
        "entry_point: agent.py\nagent_variable: root_agent\n",
        encoding="utf-8",
    )
    (project / "agent.py").write_text(
        textwrap.dedent("""\
        from langchain_core.messages import AIMessage
        from langgraph.graph import END, START, MessagesState, StateGraph
        from opentelemetry import trace

        with trace.get_tracer("web-startup-test").start_as_current_span("agent-module-load"):
            builder = StateGraph(MessagesState)
            builder.add_node("respond", lambda state: {
                "messages": [AIMessage(content="web tracing ok")]
            })
            builder.add_edge(START, "respond")
            builder.add_edge("respond", END)
            root_agent = builder.compile()
        """),
        encoding="utf-8",
    )
    (project / ".env").write_text(
        f"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:{server.server_port}/primary/v1/traces\n"
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS=Authorization=Bearer%20primary-test\n"
        f"CLOUD_MONITOR_OTLP_TRACES_ENDPOINT=http://127.0.0.1:{server.server_port}/cloud/v1/traces\n"
        "CLOUD_MONITOR_OTLP_TRACES_HEADERS=Ksc-Appkey=cloud-test\n",
        encoding="utf-8",
    )
    # A fresh process isolates OTel globals and instrumentation from other tests.
    script = textwrap.dedent("""\
        import sys
        from contextlib import nullcontext
        from unittest.mock import patch

        from click.testing import CliRunner
        from fastapi.testclient import TestClient
        from ksadk.cli.cmd_web import web
        from ksadk.tracing import shutdown_tracing

        def serve(app, **kwargs):
            assert kwargs == {"host": "0.0.0.0", "port": 8080}
            with TestClient(app) as client:
                response = client.post("/v1/responses", json={
                    "input": "hello", "user": "test-user", "stream": False,
                })
                assert response.status_code == 200, response.text
                assert response.json()["output_text"] == "web tracing ok", response.text

        failure = sys.argv[2] == "failure"
        tracing_patch = patch("ksadk.tracing.setup_tracing", side_effect=RuntimeError(
            "fixture tracing unavailable"
        )) if failure else nullcontext()
        try:
            with tracing_patch, patch("ksadk.cli.cmd_web.uvicorn.run", side_effect=serve):
                result = CliRunner().invoke(web, [
                    sys.argv[1], "--host", "0.0.0.0", "--port", "8080", "--no-open",
                ])
            assert result.exit_code == 0, (result.output, result.exception)
            if failure:
                assert "fixture tracing unavailable" in result.output, result.output
        finally:
            shutdown_tracing()
        """)
    repo_root = Path(__file__).resolve().parents[2]
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(repo_root),
        "NO_PROXY": "127.0.0.1,localhost",
        "OPENAI_API_BASE": "http://127.0.0.1:1/v1",
        "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
        "OPENAI_MODEL_NAME": "fixture-model",
        "KSADK_CHECKPOINT_BACKEND": "memory",
        "KSADK_SESSION_BACKEND": "memory",
        "KSADK_STM_BACKEND": "memory",
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, str(project), "failure" if tracing_failure else "ok"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    if tracing_failure:
        assert received == []
        return

    exported = {"/primary/v1/traces": [], "/cloud/v1/traces": []}
    for path, headers, request in received:
        assert path in exported
        if path == "/primary/v1/traces":
            assert headers["authorization"] == "Bearer primary-test"
        else:
            assert headers["ksc-appkey"] == "cloud-test"
        assert headers["content-type"] == "application/x-protobuf"
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                exported[path].extend(scope_spans.spans)

    primary = exported["/primary/v1/traces"]
    secondary = exported["/cloud/v1/traces"]
    assert {"agent-module-load", "respond"} <= {span.name for span in primary}
    identities = [(span.trace_id, span.span_id, span.parent_span_id) for span in primary]
    assert len(identities) == len(set(identities)), "Spans must not be exported twice"
    assert sorted(identities) == sorted(
        (span.trace_id, span.span_id, span.parent_span_id) for span in secondary
    )
