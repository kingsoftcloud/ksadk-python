"""Business session identity must survive native instrumentation and batching."""

import asyncio
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ksadk.runtime_context import platform_invocation_scope, session_invocation_context
from ksadk.tracing import setup


@pytest.fixture
def tracing(monkeypatch):
    monkeypatch.setattr(setup, "_managed_span_processors", [])
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    setup._register_span_processor(provider, SimpleSpanProcessor(exporter))
    try:
        yield provider.get_tracer(__name__), exporter
    finally:
        provider.shutdown()


@pytest.mark.asyncio
async def test_concurrent_sessions_keep_identity_after_context_exit(tracing):
    tracer, exporter = tracing

    async def run(session_id):
        with platform_invocation_scope(session_invocation_context({}, session_id=session_id)):
            span = tracer.start_span(session_id)
            await asyncio.sleep(0)
            # ADK can write its internal UUID immediately before ending a span.
            span.set_attributes(
                {
                    "gen_ai.conversation.id": "native-uuid",
                    "session.id": "native-uuid",
                    "langfuse.session_id": "native-uuid",
                }
            )
        # Export must use the identity captured at start, not the ending context.
        with platform_invocation_scope(session_invocation_context({}, session_id="other")):
            span.end()

    await asyncio.gather(run("sess-a"), run("sess-b"))
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        for key in ("session.id", "gen_ai.conversation.id", "langfuse.session_id"):
            assert span.attributes[key] == span.name


def test_child_inherits_business_session_before_parent_is_exported(tracing):
    tracer, exporter = tracing
    with tracer.start_span("root") as root:
        root.set_attribute("ksadk.session_id", "sess-business")
        parent_context = trace.set_span_in_context(root)
        with tracer.start_as_current_span("child", context=parent_context) as child:
            child.set_attribute("gen_ai.conversation.id", "native-uuid")
        (exported_child,) = exporter.get_finished_spans()
        assert exported_child.attributes["session.id"] == "sess-business"
        assert exported_child.attributes["gen_ai.conversation.id"] == "sess-business"
        assert exported_child.context == child.get_span_context()
        assert exported_child.parent == root.get_span_context()
        assert child.attributes["gen_ai.conversation.id"] == "native-uuid"
    assert exporter.get_finished_spans()[-1].attributes["gen_ai.conversation.id"] == "sess-business"


def test_span_without_business_session_keeps_native_attributes(tracing):
    tracer, exporter = tracing
    with tracer.start_as_current_span("unbound") as span:
        span.set_attribute("gen_ai.conversation.id", "native-only")
    assert dict(exporter.get_finished_spans()[0].attributes) == {
        "gen_ai.conversation.id": "native-only"
    }


@pytest.mark.parametrize("streaming", [False, True], ids=["invoke", "stream"])
@pytest.mark.parametrize("routing", ["static", "routed"])
def test_real_adk_preserves_native_binding_and_exports_business_session(
    tmp_path, streaming, routing
):
    received = {"/langfuse": [], "/cloud_monitor": []}

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            request = ExportTraceServiceRequest()
            request.ParseFromString(self.rfile.read(int(self.headers["Content-Length"])))
            for resource in request.resource_spans:
                for scope in resource.scope_spans:
                    received[self.path].extend(scope.spans)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Isolate OTel globals; use real ADK tracing and session services, replacing
    # only the network model. Every span is exported in a separate batch.
    script = r"""
import asyncio
import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace

from google.adk.agents import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService as NativeSessions
from google.genai import types

from ksadk.runners.adk_runner import ADKRunner
from ksadk.sessions import bind_session_service
from ksadk.sessions.continuity import ConversationSessionCore
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.tracing import (
    bind_observability_route, setup_routed_tracing, setup_tracing, shutdown_tracing,
)

class FixtureModel(BaseLlm):
    model: str = "fixture-model"

    async def generate_content_async(self, llm_request, stream=False):
        yield LlmResponse(content=types.Content(
            role="model", parts=[types.Part(text="fixture answer")]
        ))

base, streaming, routing = sys.argv[1:]
route = nullcontext()
if routing == "routed":
    setup_routed_tracing(enable_adk_instrumentation=False)
    route = bind_observability_route({
        "route_id": "test-route", "project_id": "test-project", "account_id": "test-account",
        "experiment_id": "test-experiment", "run_id": "test-run",
        "targets": [{
            "backend": backend, "binding_id": backend, "binding_version": 1,
            "endpoint": base + "/" + backend, "protocol": "http/protobuf",
            "headers": {"Authorization": "Bearer fixture"}, "credential_version": "1",
        } for backend in ("langfuse", "cloud_monitor")],
    })
else:
    setup_tracing(enable_inmemory=False, enable_adk_instrumentation=False)

async def main():
    native = NativeSessions()
    canonical = InMemorySessionService()
    await canonical.create_session("fixture_agent", "test-user", "sess-business")
    root = Agent(name="fixture_agent", model=FixtureModel())
    bindings = []
    with bind_session_service(canonical), route:
        for turn in range(2):
            # A fresh adapter must recover the persisted native mapping.
            adapter = ADKRunner(SimpleNamespace(name="fixture_agent"), ".")
            adapter._agent = root
            adapter._session_service = native
            adapter._runner = Runner(agent=root, app_name=root.name, session_service=native)
            request = {"input": "hello", "session_id": "sess-business", "run_id": f"run-{turn}"}
            if streaming == "True":
                events = [event async for event in adapter.stream_canonical_events(request)]
                assert any(event.event_type == "run.completed" for event in events), events
                assert not any(event.event_type == "run.failed" for event in events), events
            else:
                assert (await adapter.invoke(request))["output"] == "fixture answer"
            binding = await ConversationSessionCore(canonical).get_binding_by_session_id(
                "sess-business", "adk"
            )
            assert binding["internal_session_id"] != "sess-business", binding
            bindings.append(binding)
    for key in ("external_session_id", "internal_session_id", "native_user_id", "owner_scope_ref"):
        assert bindings[0][key] == bindings[1][key], bindings
    session = await native.get_session(app_name=root.name, user_id="ksadk_user",
                                       session_id=bindings[0]["internal_session_id"])
    assert sum(event.author == "user" for event in session.events) == 2
    print(json.dumps(bindings[0]))

try:
    asyncio.run(main())
finally:
    shutdown_tracing()
"""
    repo_root = Path(__file__).resolve().parents[1]
    base = f"http://127.0.0.1:{server.server_port}"
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(repo_root),
        "NO_PROXY": "127.0.0.1,localhost",
        "KSADK_OTLP_MAX_EXPORT_BATCH_SIZE": "1",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": base + "/langfuse",
        "CLOUD_MONITOR_OTLP_TRACES_ENDPOINT": base + "/cloud_monitor",
        "CLOUD_MONITOR_OTLP_TRACES_HEADERS": "Ksc-Appkey=fixture",
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, base, str(streaming), routing],
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
    binding = json.loads(completed.stdout)
    assert binding["external_session_id"] == "sess-business"
    assert binding["internal_session_id"] != "sess-business"
    identities = []
    for spans in received.values():
        assert any(span.name.startswith("invoke_agent") for span in spans)
        assert any(span.name.startswith("generate_content") for span in spans)
        ids = [(span.trace_id, span.span_id, span.parent_span_id) for span in spans]
        assert len(ids) == len(set(ids)), "No duplicate exports"
        identities.append(sorted(ids))
        for span in spans:
            attributes = {item.key: item.value.string_value for item in span.attributes}
            assert attributes["session.id"] == "sess-business", (span.name, attributes)
            assert attributes["gen_ai.conversation.id"] == "sess-business", (span.name, attributes)
            for key in ("langfuse.session.id", "langfuse.session_id"):
                if key in attributes:
                    assert attributes[key] == "sess-business"
    assert identities[0] == identities[1]
