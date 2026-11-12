"""Actual Gateway RS256 issuer -> SDK verifier, using each repository's venv."""

import json
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from starlette.requests import Request

from ksadk.a2a.platform import JWTGatewayIdentityVerifier

PRODUCER = """
import json, hashlib
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from app.services.a2a_forward_identity import (
    A2AProductKeyForwardIdentity,
    JWTForwardAssertionIssuer,
    build_a2a_forward_headers,
)

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
private = key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
public = (
    key.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)
claims = A2AProductKeyForwardIdentity(
    platform_task_id="a2a-task-cross-contract",
    account_id="account-test",
    tenant_id="tenant-test",
    caller_principal_id="user-test",
    key_id="key-test", sub_account_id="sub-test",
    target_agent_id="ar-target",
    target_runtime_id="runtime-target",
    target_a2a_agent_id="a2a-agent-123456781234423482341234567890ab",
    target_version_id="version-test",
    operation="send_message",
    body_sha256=hashlib.sha256(b"{}").hexdigest(),
    backend_origin_sha256="1" * 64,
)
headers = build_a2a_forward_headers(
    {"Authorization": "Bearer fake-test-key"},
    identity=claims,
    issuer=JWTForwardAssertionIssuer(private_key=private),
)
assert "Authorization" not in headers
print(json.dumps({"headers": headers, "public": public}))
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("network", [False, True])
async def test_gateway_product_forward_contract(tmp_path, network):
    configured = os.environ.get("A2A_TEST_GATEWAY_REPO")
    if not configured:
        pytest.skip("A2A_TEST_GATEWAY_REPO required for cross-repository test")
    repo = Path(configured).resolve()
    result = json.loads(
        subprocess.check_output(
            [str(repo / ".venv/bin/python"), "-c", PRODUCER],
            cwd=repo,
            text=True,
            timeout=15,
        )
    )

    def request(body):
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "https",
                "path": "/a2a/jsonrpc",
                "query_string": b"",
                "headers": [(k.lower().encode(), v.encode()) for k, v in result["headers"].items()],
            },
            receive,
        )

    verifier = JWTGatewayIdentityVerifier(result["public"])
    identity = await verifier.verify(request(b"{}"))
    assert identity.caller_principal_type == "user"
    assert identity.sub_account_id == "sub-test"
    assert identity.caller_runtime_id is None
    assert identity.task_binding.platform_task_id == "a2a-task-cross-contract"
    with pytest.raises(PermissionError):
        await verifier.verify(request(b'{"changed":true}'))

    # Exercise the actual inbound middleware, including target validation and
    # absence of direct product-Key authentication at the Runtime boundary.
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from ksadk.a2a.identity import A2AGatewayIdentityMiddleware, A2ATrustedIdentityResolver

    reached = []

    async def endpoint(inbound):
        trusted = A2ATrustedIdentityResolver().resolve(inbound)
        reached.append(trusted)
        return JSONResponse({"task": trusted.task_binding.platform_task_id})

    for runtime, expected in [("runtime-target", 200), ("different-runtime", 401)]:
        app = Starlette(routes=[Route("/a2a/jsonrpc", endpoint, methods=["POST"])])
        app.add_middleware(
            A2AGatewayIdentityMiddleware,
            verifier=verifier,
            expected_account_id="account-test",
            expected_tenant_id="tenant-test",
            expected_target_agent_id="ar-target",
            expected_target_runtime_id=runtime,
        )
        async with _runtime_client(app, tmp_path, network) as client:
            response = await client.post("/a2a/jsonrpc", content=b"{}", headers=result["headers"])
            assert response.status_code == expected
            for headers in ({}, {"Authorization": "Bearer fake-test-key"}):
                direct = await client.post("/a2a/jsonrpc", content=b"{}", headers=headers)
                assert direct.status_code == 401
    assert len(reached) == 1


@asynccontextmanager
async def _runtime_client(app, root, network):
    import asyncio
    import ssl
    from contextlib import nullcontext

    import httpx
    import uvicorn

    if not network:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://runtime.test"
        ) as client:
            yield client
        return
    from tests.a2a.test_local_managed_network import _port, _trust

    _trust(root)
    port = _port()
    listener = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            lifespan="off",
            access_log=False,
            log_level="error",
            ssl_keyfile=str(root / "server.key"),
            ssl_certfile=str(root / "server.crt"),
            ssl_ca_certs=str(root / "ca.pem"),
            ssl_cert_reqs=ssl.CERT_REQUIRED,
        )
    )
    listener.capture_signals = nullcontext
    task = asyncio.create_task(listener.serve())
    try:
        async with asyncio.timeout(5):
            while not listener.started:
                if task.done():
                    await task
                    raise AssertionError("TLS listener exited before startup")
                await asyncio.sleep(0.01)
        origin = f"https://127.0.0.1:{port}"
        context = ssl.create_default_context(cafile=str(root / "ca.pem"))
        async with httpx.AsyncClient(verify=context, trust_env=False, base_url=origin) as direct:
            with pytest.raises(httpx.TransportError):
                await direct.post("/a2a/jsonrpc", content=b"{}")
        untrusted = root / "untrusted"
        untrusted.mkdir(exist_ok=True)
        _trust(untrusted)
        wrong_context = ssl.create_default_context(cafile=str(root / "ca.pem"))
        wrong_context.load_cert_chain(str(untrusted / "client.crt"), str(untrusted / "client.key"))
        async with httpx.AsyncClient(
            verify=wrong_context, trust_env=False, base_url=origin
        ) as wrong_client:
            with pytest.raises(httpx.TransportError):
                await wrong_client.post("/a2a/jsonrpc", content=b"{}")
        context.load_cert_chain(str(root / "client.crt"), str(root / "client.key"))
        async with httpx.AsyncClient(verify=context, trust_env=False, base_url=origin) as client:
            yield client
    finally:
        listener.should_exit = True
        await asyncio.wait_for(task, 5)
