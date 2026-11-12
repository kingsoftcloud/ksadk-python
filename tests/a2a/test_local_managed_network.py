"""Opt-in local TCP/TLS gate across actual Server, Gateway and native ADK.

Model responses, root orchestration, cluster DNS/discovery and credential
issuance are explicit test fixtures. The callee's events and caller's child
facts come from production code; Prepare/permit, Gateway and wire are real.
"""

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import ssl
import uuid
from contextlib import nullcontext, suppress
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ksadk.a2a.bootstrap import AgentEngineA2ABootstrap, RuntimeA2AMetadata
from ksadk.a2a.context_store import PostgresA2AContextStore
from ksadk.a2a.control_plane import InternalA2AControlPlaneClient, WorkloadTokenProvider
from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.a2a.platform import JWTGatewayIdentityVerifier, PostgresA2ACheckpointStore
from ksadk.a2a.resume_store import PostgresA2AResumeStateStore
from ksadk.a2a.space_client import A2ASpaceClient
from ksadk.a2a.task_event_outbox import PostgresA2ATaskEventOutbox
from ksadk.a2a.task_store import build_a2a_task_store
from ksadk.a2a.tool_activity import PublicToolActivityPolicy, PublicToolRule
from ksadk.events.canonical import (
    ItemCompleted,
    ItemStarted,
    OutputRef,
    RunCompleted,
    RunStarted,
    SourceRef,
)
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.content import ContentSnapshot, TextContent, ToolCallContent
from ksadk.sessions import bind_session_service
from ksadk.sessions.local_service import LocalSessionService
from tests.a2a.test_invocation_service_r85 import request, trigger


def _port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _trust(root):
    keys = {
        name: rsa.generate_private_key(public_exponent=65537, key_size=2048)
        for name in ("ca", "server", "client", "workload", "permit", "forward")
    }
    for name, key in keys.items():
        (root / f"{name}.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        (root / f"{name}.pub").write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "r85-local-ca")])
    for name in ("ca", "server", "client"):
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(
                ca_name
                if name == "ca"
                else x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
            )
            .issuer_name(ca_name)
            .public_key(keys[name].public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=name == "ca", path_length=None), True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(keys[name].public_key()), False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(keys["ca"].public_key()),
                False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=name != "ca",
                    content_commitment=False,
                    key_encipherment=name == "server",
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=name == "ca",
                    crl_sign=name == "ca",
                    encipher_only=False,
                    decipher_only=False,
                ),
                True,
            )
        )
        if name != "ca":
            cert = cert.add_extension(
                x509.ExtendedKeyUsage(
                    [
                        ExtendedKeyUsageOID.CLIENT_AUTH
                        if name == "client"
                        else ExtendedKeyUsageOID.SERVER_AUTH
                    ]
                ),
                True,
            )
        if name == "server":
            cert = cert.add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                False,
            )
        filename = "ca.pem" if name == "ca" else f"{name}.crt"
        (root / filename).write_bytes(
            cert.sign(keys["ca"], hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
        )


@pytest.mark.parametrize("untrusted_peer", [None, "server", "client"])
def test_local_trust_strict_mutual_tls(tmp_path, untrusted_peer):
    """The shared fixture must pass strict chain validation on both peers."""
    _trust(tmp_path)
    untrusted = tmp_path / "untrusted"
    if untrusted_peer:
        untrusted.mkdir()
        _trust(untrusted)
    client_context = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
    server_context = ssl.create_default_context(
        ssl.Purpose.CLIENT_AUTH, cafile=str(tmp_path / "ca.pem")
    )
    server_context.verify_mode = ssl.CERT_REQUIRED
    for name, context in (("client", client_context), ("server", server_context)):
        context.verify_flags |= ssl.VERIFY_X509_STRICT
        root = untrusted if name == untrusted_peer else tmp_path
        context.load_cert_chain(str(root / f"{name}.crt"), str(root / f"{name}.key"))
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(
        client_in, client_out, server_hostname="127.0.0.1"
    )
    server = server_context.wrap_bio(server_in, server_out, server_side=True)

    def handshake():
        completed = set()
        for _ in range(20):
            for name, peer, outgoing, incoming in (
                ("client", client, client_out, server_in),
                ("server", server, server_out, client_in),
            ):
                if name not in completed:
                    try:
                        peer.do_handshake()
                        completed.add(name)
                    except ssl.SSLWantReadError:
                        pass
                incoming.write(outgoing.read())
            if len(completed) == 2:
                return
        pytest.fail("strict mutual TLS handshake did not complete")

    if untrusted_peer:
        with pytest.raises(ssl.SSLCertVerificationError):
            handshake()
    else:
        handshake()
        assert client.getpeercert()["subject"]
        assert server.getpeercert()["subject"]
        client.write(b"strict mutual TLS")
        server_in.write(client_out.read())
        assert server.read() == b"strict mutual TLS"


class _Tokens(WorkloadTokenProvider):
    def __init__(self, root):
        self.root = root

    def get_token(self, audience):
        import time

        import jwt

        now = int(time.time())
        return jwt.encode(
            dict(
                iss="test-workload",
                aud=audience,
                sub="workload-1",
                iat=now,
                exp=now + 300,
                account_id="account-1",
                sub_account_id="runtime-owner",
                caller_agent_id="ar-caller",
                caller_runtime_id="runtime-1",
            ),
            (self.root / "workload.key").read_text(),
            algorithm="RS256",
        )


class _LocalGatewayTransport(httpx.AsyncBaseTransport):
    """Replace fixture DNS only; requests still use real TLS and the real Host."""

    def __init__(self, root, port):
        self.port = port
        self.root = root
        self.methods = []
        self.inner = httpx.AsyncHTTPTransport(
            verify=ssl.create_default_context(cafile=str(root / "ca.pem")), trust_env=False
        )

    async def handle_async_request(self, req):
        assert req.url.host.endswith(".r85.test")
        self.methods.append(json.loads(await req.aread())["method"])
        req.url = req.url.copy_with(host="127.0.0.1", port=self.port)
        response = await self.inner.handle_async_request(req)
        if response.status_code >= 400:
            body = await response.aread()
            (self.root / "gateway-error.json").write_text(
                json.dumps(
                    {
                        "status": response.status_code,
                        "body": body.decode(),
                        "request": (await req.aread()).decode(),
                    },
                    indent=2,
                )
            )
        return response

    async def aclose(self):
        await self.inner.aclose()


async def _native_runtime(root, public_sessions, calls):
    from google.adk.agents import LlmAgent
    from google.adk.apps import App
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import Runner
    from google.adk.sessions import DatabaseSessionService
    from google.genai import types

    from ksadk.runners.adk_runner import ADKRunner
    from ksadk.runtime.framework_adapters import ADKRuntimeAdapter

    def lookup(city: str) -> dict:
        """Read a deterministic local value."""
        calls.append(city)
        return {"value": 42, "accessToken": "fake-private-network-value"}

    class Model(BaseLlm):
        model: str = "local-network-fixture"

        async def generate_content_async(self, llm_request, stream=False):
            result = any(
                p.function_response is not None for c in llm_request.contents for p in c.parts
            )
            parts = [types.Part(text="second answer" if result else "first answer")]
            if not result:
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            id="call_1", name="lookup", args={"city": "Paris"}
                        )
                    )
                )
            yield LlmResponse(content=types.Content(role="model", parts=parts))

    agent = LlmAgent(name="tool_callee", model=Model(), tools=[lookup])
    native_app = App(name=agent.name, root_agent=agent)
    native = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{root}/native.db")

    async def create_session(**kwargs):
        # Supply the SDK session side of the short-term-memory fixture too.
        await public_sessions.create_session(
            agent_id=agent.name, user_id="anonymous", session_id=kwargs["session_id"]
        )
        return await native.create_session(**kwargs)

    runner = ADKRunner(
        SimpleNamespace(entry_point="agent.py", agent_variable="root_agent", name=agent.name), "."
    )
    runner._agent = agent
    runner._module = SimpleNamespace(app=native_app)
    runner._runner = Runner(app=native_app, session_service=native)
    runner._session_service = native
    runner._short_term_memory = SimpleNamespace(
        backend="database", session_service=native, create_session=create_session
    )
    await runner.prepare_runtime_capabilities()
    return ADKRuntimeAdapter(runner)


async def _wait_port(port, process=None):
    for _ in range(200):
        if process and process.returncode is not None:
            raise AssertionError(f"local worker exited: {process.returncode}")
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise AssertionError(f"local worker did not listen on {port}")


@pytest_asyncio.fixture
async def network_engine():
    if not os.getenv("A2A_TEST_POSTGRES_DSN"):
        pytest.skip("dedicated PostgreSQL required")
    admin = create_async_engine(os.environ["A2A_TEST_POSTGRES_DSN"])
    schema = "r85_network_" + uuid.uuid4().hex
    async with admin.begin() as db:
        await db.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        os.environ["A2A_TEST_POSTGRES_DSN"],
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as db:
            await db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()


@pytest.mark.asyncio
async def test_local_server_gateway_native_callee_stream_and_get(
    tmp_path, network_engine, monkeypatch
):
    engine = network_engine
    required = (
        "R85_SERVER_REPO",
        "R85_GATEWAY_REPO",
        "A2A_TEST_POSTGRES_DSN",
        "A2A_TEST_REDIS_URL",
    )
    if not all(os.getenv(name) for name in required):
        pytest.skip("explicit local repository paths and dedicated PostgreSQL/Redis required")
    _trust(tmp_path)
    ports = {f"{name}_port": _port() for name in ("server", "gateway", "runtime")}
    from ksadk.sessions import reset_session_service

    monkeypatch.setenv("KSADK_SESSION_BACKEND", "local")
    monkeypatch.setenv("KSADK_SESSION_PATH", str(tmp_path / "public.db"))
    await reset_session_service()
    public = LocalSessionService(tmp_path / "public.db")
    calls = []
    runtime = await _native_runtime(tmp_path, public, calls)
    original_stream = runtime.stream
    native_events = []

    async def record_runtime(handle):
        try:
            async for event in original_stream(handle):
                native_events.append(event.model_dump(mode="json"))
                (tmp_path / "native-canonical.json").write_text(json.dumps(native_events, indent=2))
                yield event
        except Exception:
            import traceback

            (tmp_path / "runtime-error.txt").write_text(traceback.format_exc())
            raise

    runtime.stream = record_runtime
    verifier = JWTGatewayIdentityVerifier((tmp_path / "forward.pub").read_text())
    bootstrap = AgentEngineA2ABootstrap.from_platform(
        runtime_metadata=RuntimeA2AMetadata(
            account_id="account-1",
            tenant_id="account-1",
            agent_id="ar-callee",
            runtime_id="runtime-callee",
            a2a_agent_id="a2a-agent-00000000000040008000000000000002",
            internal_base_url=f"https://127.0.0.1:{ports['runtime_port']}",
            name="tool_callee",
            version="1.0.0",
            skills=(),
        ),
        task_store=build_a2a_task_store(engine=engine, create_table=True),
        context_store=PostgresA2AContextStore(engine),
        checkpoint_store=PostgresA2ACheckpointStore(engine),
        resume_state_store=PostgresA2AResumeStateStore(engine),
        gateway_identity_verifier=verifier,
        gateway_probe_verifier=verifier,
        public_tool_policy=PublicToolActivityPolicy(
            {
                "lookup": PublicToolRule(
                    "weather", frozenset({"city"}), frozenset({"value", "accessToken"})
                )
            }
        ),
        outbound_enabled=False,
    )
    app = FastAPI()
    server = bootstrap.mount(app, runtime_adapter=runtime, runtime_type="adk")
    await bootstrap.start()
    card = MessageToDict(server.agent_card)
    config = {
        **ports,
        "card": card,
        "card_sha256": hashlib.sha256(
            json.dumps(card, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    (tmp_path / "network.json").write_text(json.dumps(config))
    listener = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=ports["runtime_port"],
            lifespan="off",
            access_log=False,
            log_level="warning",
            ssl_keyfile=str(tmp_path / "server.key"),
            ssl_certfile=str(tmp_path / "server.crt"),
            ssl_ca_certs=str(tmp_path / "ca.pem"),
            ssl_cert_reqs=ssl.CERT_REQUIRED,
        )
    )
    # This in-process listener must not replace pytest process signal handlers.
    # Otherwise sse-starlette discovers its shutdown and poisons later ASGI tests.
    listener.capture_signals = nullcontext
    workers, logs = [], []
    runtime_task = None
    try:
        with bind_session_service(public):
            runtime_task = asyncio.create_task(listener.serve())
            for kind, module in (
                ("server", "tests.e2e.r85_control_plane_worker"),
                ("gateway", "tests.r85_gateway_worker"),
            ):
                log = (tmp_path / f"{kind}.log").open("w")
                logs.append(log)
                process = await asyncio.create_subprocess_exec(
                    "uv",
                    "run",
                    "python",
                    "-m",
                    module,
                    str(tmp_path),
                    cwd=os.environ[f"R85_{kind.upper()}_REPO"],
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                )
                workers.append(process)
                await _wait_port(ports[f"{kind}_port"], process)
            gateway_transport = _LocalGatewayTransport(tmp_path, ports["gateway_port"])
            async with (
                httpx.AsyncClient(trust_env=False) as control_http,
                httpx.AsyncClient(
                    transport=gateway_transport,
                    timeout=30,
                    trust_env=False,
                ) as wire_http,
            ):
                control = InternalA2AControlPlaneClient(
                    f"http://127.0.0.1:{ports['server_port']}",
                    token_provider=_Tokens(tmp_path),
                    httpx_client=control_http,
                )
                outbox = PostgresA2ATaskEventOutbox(engine)
                wire = A2ASpaceClient(
                    request().spec.space_id, control, httpx_client=wire_http, event_outbox=outbox
                )
                await public.create_session(agent_id="caller", user_id="u", session_id="s")
                from ksadk.a2a.invocation_policy import (
                    A2AInvocationPolicy,
                    A2AInvocationPolicyDecision,
                )

                store = RuntimeEventStore(public)
                root_env = dict(
                    schema_version=2,
                    seq=0,
                    timestamp=datetime.now(timezone.utc).timestamp(),
                    run_id="public-run",
                    scope_id="root",
                    source=SourceRef(framework="ksadk", metadata={"test_driver": True}),
                )
                call_snapshot = ContentSnapshot(
                    parts=(
                        ToolCallContent(
                            part_id="root-call-part", call_id="call_1", name="expert", arguments={}
                        ),
                    )
                )
                await store.append(
                    "s",
                    [
                        RunStarted(**root_env, event_id="driver-start", status="running"),
                        ItemStarted(
                            **root_env,
                            event_id="driver-call-start",
                            item_id="root-call-item",
                            item_kind="tool_call",
                            initial=call_snapshot,
                        ),
                    ],
                )
                service = A2AInvocationService.from_space_client(
                    wire,
                    event_store=store,
                    policy=A2AInvocationPolicy(
                        evaluator=lambda *_: A2AInvocationPolicyDecision("allow")
                    ),
                )
                network_version = "a2a-version-00000000000040008000000000000003"
                req = request()
                req = req.model_copy(
                    update={
                        "spec": req.spec.model_copy(
                            update={
                                "target_agent_id": "a2a-agent-00000000000040008000000000000002",
                                "expected_version_id": network_version,
                            }
                        ),
                        "handle": req.handle.model_copy(
                            update={"target_agent_id": "a2a-agent-00000000000040008000000000000002"}
                        ),
                    }
                )
                events = [event async for event in service.invoke(req, trigger_ref=trigger())]
                payload = [event.model_dump(mode="json") for event in events]
                (tmp_path / "canonical.json").write_text(json.dumps(payload, indent=2))
                assert calls == ["Paris"], payload
                assert "fake-private-network-value" not in json.dumps(payload)
                descriptor = payload[-1]["snapshot"]["parts"][0]["data"]
                assert descriptor["status"] == "completed", payload
                outputs = [
                    e
                    for e in payload
                    if e["event_type"] == "item.completed"
                    and e.get("item_kind") in {"message", "artifact"}
                ]
                assert [e["snapshot"]["parts"][0]["text"] for e in outputs] == [
                    "first answer",
                    "second answer",
                ]
                first = next(i for i, e in enumerate(payload) if "first answer" in json.dumps(e))
                call = next(i for i, e in enumerate(payload) if e.get("item_kind") == "tool_call")
                result = next(
                    i
                    for i, e in enumerate(payload)
                    if e.get("item_kind") == "tool_result" and e["event_type"] == "item.completed"
                )
                second = next(i for i, e in enumerate(payload) if "second answer" in json.dumps(e))
                assert first < call < result < second
                assert "first answer" in json.dumps(payload)
                assert "second answer" in json.dumps(payload)
                replay = [event async for event in service.invoke(req, trigger_ref=trigger())]
                assert calls == ["Paris"]
                assert replay
                await wire.event_dispatcher.drain(raise_on_error=True)
                assert not await outbox.pending()
                handle = service.handle(req.scope_id)
                prepared = await control.prepare_task_operation(
                    platform_task_id=handle.platform_task_id, operation="get_task"
                )
                task = await wire.get_prepared_task(prepared)
                task_payload = MessageToDict(task)
                (tmp_path / "get-task.json").write_text(json.dumps(task_payload, indent=2))
                assert task_payload["status"]["state"] == "TASK_STATE_COMPLETED"
                assert [
                    part.text
                    for artifact in task.artifacts
                    for part in artifact.parts
                    if part.HasField("text")
                ] == ["first answer", "second answer"]
                assert calls == ["Paris"]
                assert "fake-private-network-value" not in json.dumps(task_payload)
                server_schema = (tmp_path / "server-schema").read_text()
                registry_engine = create_async_engine(
                    os.environ["A2A_TEST_POSTGRES_DSN"],
                    connect_args={"server_settings": {"search_path": server_schema}},
                )
                try:
                    async with registry_engine.connect() as db:
                        row = (
                            await db.execute(
                                text(
                                    "SELECT status, canonical_event_refs "
                                    "FROM a2a_tasks WHERE id=:id"
                                ),
                                {"id": handle.platform_task_id},
                            )
                        ).one()
                    assert row.status == "completed"
                    assert len(row.canonical_event_refs) == len(events)
                finally:
                    await registry_engine.dispose()
                # Invocation terminal reconciliation and the independent durable read
                # each obtain a fresh GetTask permit; neither repeats Send.
                assert gateway_transport.methods == ["SendStreamingMessage", "GetTask", "GetTask"]
                root_env["timestamp"] = datetime.now(timezone.utc).timestamp()
                answer = ContentSnapshot(
                    parts=(TextContent(part_id="root-answer-part", text="root continued"),)
                )
                await store.append(
                    "s",
                    [
                        ItemCompleted(
                            **root_env,
                            event_id="driver-call-done",
                            item_id="root-call-item",
                            item_kind="tool_call",
                            snapshot=call_snapshot,
                        ),
                        ItemStarted(
                            **root_env,
                            event_id="driver-answer-start",
                            item_id="root-answer",
                            item_kind="message",
                            initial=answer,
                        ),
                        ItemCompleted(
                            **root_env,
                            event_id="driver-answer-done",
                            item_id="root-answer",
                            item_kind="message",
                            snapshot=answer,
                        ),
                        RunCompleted(
                            **root_env,
                            event_id="driver-done",
                            status="completed",
                            output_refs=(
                                OutputRef(
                                    scope_id="root",
                                    item_id="root-answer",
                                    part_id="root-answer-part",
                                ),
                            ),
                        ),
                    ],
                )
                from ksadk.conversations.runtime_projection import RuntimeConversationProjection

                projection = RuntimeConversationProjection("s", profile="agent-block-v1")
                all_events = await store.list("s", run_id="public-run")
                items = []
                for event in all_events:
                    projected = projection.apply(event)
                    if projected is not None:
                        items.append(
                            {
                                "seq": event.seq,
                                "conversationItem": projected.model_dump(
                                    mode="json", by_alias=True
                                ),
                            }
                        )
                child_items = [
                    row["conversationItem"]
                    for row in items
                    if row["conversationItem"]["nativeRef"].get("parentScopeId")
                ]
                texts = [
                    item["payload"]["text"]
                    for item in child_items
                    if item["kind"] == "assistant_text" and item["lifecycle"] == "completed"
                ]
                assert texts == ["first answer", "second answer"]
                live_calls = [
                    item
                    for item in child_items
                    if item["kind"] == "tool_call"
                    and item["payload"].get("tool") == "weather"
                    and item["lifecycle"] == "streaming"
                ]
                assert live_calls and live_calls[0]["payload"]["executionStatus"] == "running"
                assert all(item["kind"] != "unknown" for item in child_items)
                (tmp_path / "conversation-items.json").write_text(json.dumps(items, indent=2))
                (tmp_path / "full-canonical.json").write_text(
                    json.dumps([event.model_dump(mode="json") for event in all_events], indent=2)
                )


                from tests.a2a.network_native_caller import run_native_caller

                native_caller = await run_native_caller(wire, req.spec, public)
                assert calls == ["Paris", "Paris"]
                assert gateway_transport.methods[3:] == ["SendStreamingMessage", "GetTask"]
                assert not await outbox.pending()
                (tmp_path / "native-caller.json").write_text(json.dumps(native_caller, indent=2))
                await wire.aclose()

    finally:
        for worker in reversed(workers):
            if worker.returncode is None:
                worker.terminate()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(worker.wait(), 10)
                if worker.returncode is None:
                    worker.kill()
                    await worker.wait()
        for log in logs:
            log.close()
        listener.should_exit = True
        if runtime_task:
            await runtime_task
        await bootstrap.stop()
        await reset_session_service()
