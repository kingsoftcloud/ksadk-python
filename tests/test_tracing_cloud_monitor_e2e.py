import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_cloud_monitor_otlp_local_http_e2e():
    repo_root = Path(__file__).resolve().parents[1]
    script = r"""
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for key in list(os.environ):
    if (
        key.startswith("OTEL_EXPORTER_OTLP")
        or key.startswith("CLOUD_MONITOR_")
        or key.startswith("LANGFUSE_")
    ):
        os.environ.pop(key, None)

received = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        received.append(
            {
                "path": self.path,
                "app_key": self.headers.get("Ksc-Appkey"),
                "content_type": self.headers.get("Content-Type"),
                "body_len": len(body),
            }
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):
        return


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()

os.environ["OTEL_SERVICE_NAME"] = "ar-cloudmonitor-e2e"
os.environ["CLOUD_MONITOR_APP_KEY"] = "app-key-e2e"
os.environ["CLOUD_MONITOR_OTLP_ENDPOINT"] = f"http://127.0.0.1:{server.server_port}"

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from ksadk.tracing import setup_tracing
from opentelemetry import trace

setup_tracing(enable_inmemory=False, enable_langfuse=False, enable_adk_instrumentation=False)
tracer = trace.get_tracer("ksadk-cloudmonitor-e2e")

with tracer.start_as_current_span("cloudmonitor-e2e-span") as span:
    span.set_attribute("ksadk.e2e", True)
    span.set_attribute("ksadk.agent_id", "ar-cloudmonitor-e2e")

provider = trace.get_tracer_provider()
flush_ok = provider.force_flush(timeout_millis=5000) if hasattr(provider, "force_flush") else True
if hasattr(provider, "shutdown"):
    provider.shutdown()

deadline = time.time() + 5
while not received and time.time() < deadline:
    time.sleep(0.05)

server.shutdown()
server.server_close()

print(
    json.dumps(
        {
            "flush_ok": bool(flush_ok),
            "received": len(received),
            "first": received[0] if received else None,
        },
        sort_keys=True,
    )
)
"""
    env = os.environ.copy()
    for key in list(env):
        if (
            key.startswith("OTEL_EXPORTER_OTLP")
            or key.startswith("CLOUD_MONITOR_")
            or key.startswith("LANGFUSE_")
        ):
            env.pop(key, None)
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"

    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["flush_ok"] is True
    assert payload["received"] == 1
    assert payload["first"]["path"] == "/v1/traces"
    assert payload["first"]["app_key"] == "app-key-e2e"
    assert payload["first"]["content_type"] == "application/x-protobuf"
    assert payload["first"]["body_len"] > 0
    assert "CloudMonitor OTLP config resolved" in completed.stderr
    assert "CloudMonitor OTLP exporter enabled" in completed.stderr
    assert "CloudMonitor OTLP export started" in completed.stderr
    assert "CloudMonitor OTLP export result" in completed.stderr


def test_standard_otlp_dual_write_preserves_exact_span_identity():
    repo_root = Path(__file__).resolve().parents[1]
    script = r"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for key in list(os.environ):
    if (
        key.startswith("OTEL_EXPORTER_OTLP")
        or key.startswith("CLOUD_MONITOR_")
        or key.startswith("LANGFUSE_")
    ):
        os.environ.pop(key, None)

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


def make_receiver():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            request = ExportTraceServiceRequest()
            request.ParseFromString(self.rfile.read(length))
            spans = []
            for resource_spans in request.resource_spans:
                resource_attrs = {
                    item.key: item.value.string_value
                    for item in resource_spans.resource.attributes
                }
                for scope_spans in resource_spans.scope_spans:
                    for span in scope_spans.spans:
                        attrs = {
                            item.key: (
                                item.value.string_value
                                if item.value.HasField("string_value")
                                else item.value.bool_value
                            )
                            for item in span.attributes
                        }
                        spans.append(
                            {
                                "trace_id": span.trace_id.hex(),
                                "span_id": span.span_id.hex(),
                                "name": span.name,
                                "attrs": attrs,
                                "resource": resource_attrs,
                            }
                        )
            received.extend(spans)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, received


primary, primary_spans = make_receiver()
secondary, secondary_spans = make_receiver()
os.environ["OTEL_SERVICE_NAME"] = "ar-dual-e2e"
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{primary.server_port}"
os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "Authorization=Bearer%20primary"
os.environ["CLOUD_MONITOR_OTLP_ENDPOINT"] = f"http://127.0.0.1:{secondary.server_port}"
os.environ["CLOUD_MONITOR_OTLP_HEADERS"] = "Ksc-Appkey=secondary"

from ksadk.tracing import setup_tracing, shutdown_tracing
from opentelemetry import trace

setup_tracing(enable_inmemory=False, enable_adk_instrumentation=False)
tracer = trace.get_tracer("ksadk-dual-e2e")
with tracer.start_as_current_span("ksadk-dual-e2e-known-span") as span:
    span.set_attribute("ksadk.e2e.marker", "same-object")
    span.set_attribute("ksadk.e2e.enabled", True)
    context = span.get_span_context()
    expected = {
        "trace_id": f"{context.trace_id:032x}",
        "span_id": f"{context.span_id:016x}",
    }

shutdown_tracing()
deadline = time.time() + 5
while (not primary_spans or not secondary_spans) and time.time() < deadline:
    time.sleep(0.05)

for server in (primary, secondary):
    server.shutdown()
    server.server_close()

known = lambda spans: next(
    (item for item in spans if item["name"] == "ksadk-dual-e2e-known-span"),
    None,
)
print(
    json.dumps(
        {
            "expected": expected,
            "primary": known(primary_spans),
            "secondary": known(secondary_spans),
        },
        sort_keys=True,
    )
)
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(repo_root),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }

    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    primary = payload["primary"]
    secondary = payload["secondary"]
    assert primary is not None
    assert secondary is not None
    assert payload["expected"]["trace_id"] != "0" * 32
    assert payload["expected"]["span_id"] != "0" * 16
    assert primary["trace_id"] == secondary["trace_id"] == payload["expected"]["trace_id"]
    assert primary["span_id"] == secondary["span_id"] == payload["expected"]["span_id"]
    assert primary["attrs"] == secondary["attrs"]
    assert primary["resource"] == secondary["resource"]
    assert primary["attrs"]["ksadk.e2e.marker"] == "same-object"
    assert primary["resource"]["service.name"] == "ar-dual-e2e"
