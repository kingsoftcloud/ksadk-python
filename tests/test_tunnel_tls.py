"""Real TLS listeners verify opt-out is explicit, scoped and preserved on reconnect."""

import asyncio
import datetime
import ipaddress
import socket
import ssl
from contextlib import asynccontextmanager

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import FastAPI

from ksadk.tunnel import LocalMCPService, TunnelConnector, TunnelError
from ksadk.tunnel.discovery import discover_tools
from ksadk.tunnel.server import TunnelAgentPolicy, mount_tunnel
from tests.test_mcp_tunnel import connected, eventually, local_mcp


@pytest.fixture
def certificates(tmp_path, monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    now = datetime.datetime.now(datetime.timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture-ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))

    def leaf(host_matches=True):
        server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        san = (
            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
            if host_matches
            else x509.DNSName("wrong.invalid")
        )
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture-server")]))
            .issuer_name(name)
            .public_key(server_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256())
        )
        cert_file = tmp_path / f"server-{host_matches}.pem"
        key_file = tmp_path / f"server-{host_matches}.key"
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(
            server_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        key_file.chmod(0o600)
        return cert_file, key_file

    return ca_file, leaf


@asynccontextmanager
async def tls_cloud(certificates, *, host_matches=True):
    _, leaf = certificates
    cert, key = leaf(host_matches)
    app = FastAPI()
    reg = mount_tunnel(app, policies=[TunnelAgentPolicy("root", "fixture-token", ("inventory",))])
    schemes = []

    class Capture:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "websocket":
                schemes.append(scope["scheme"])
            await app(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(
            Capture(), ssl_certfile=str(cert), ssl_keyfile=str(key), log_level="critical"
        )
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await eventually(lambda: server.started or task.done())
        if task.done():
            await task
        yield f"https://127.0.0.1:{sock.getsockname()[1]}", reg, schemes
    finally:
        await reg.close()
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            sock.close()


def client(url, endpoint="http://127.0.0.1:1/mcp", **kwargs):
    return TunnelConnector(
        url,
        token="fixture-token",
        services=[LocalMCPService("inventory", endpoint, tool_filter=("echo",))],
        reconnect_min=0.01,
        reconnect_max=0.05,
        **kwargs,
    )


async def test_default_verification_rejects_private_ca_without_retry(certificates):
    async with tls_cloud(certificates) as (url, reg, schemes):
        async with asyncio.timeout(5):
            with pytest.raises(TunnelError, match="tls_verification_failed"):
                await client(url).run()
        assert reg.connections("root") == []
        assert schemes == []  # Credentials aren't sent on an unverified connection.


@pytest.mark.parametrize("matches", [False, True])
async def test_opt_out_real_wss_tool_call_reconnect_and_isolation(certificates, matches):
    async with (
        local_mcp() as (endpoint, calls, _, _),
        tls_cloud(certificates, host_matches=matches) as (url, reg, schemes),
    ):
        connector = client(url, endpoint, verify_tls=False)
        async with connected(connector):
            (tool,) = await discover_tools("root", reg)
            assert (await tool.call({"text": "encrypted"}))["structuredContent"] == {
                "text": "encrypted"
            }
            # A second default client remains strict in the same process.
            with pytest.raises(TunnelError, match="tls_verification_failed"):
                await client(url).run(reconnect=False)
            old = connector._ws
            await old.close(code=1012)
            await eventually(
                lambda: connector._ws is not None
                and connector._ws is not old
                and connector.connected.is_set()
            )
            (tool,) = await discover_tools("root", reg)
            assert (await tool.call({"text": "reconnected"}))["structuredContent"] == {
                "text": "reconnected"
            }
            assert calls == ["encrypted", "reconnected"]
            assert schemes == ["wss", "wss"]
            assert ssl.create_default_context().verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("matches", [False, True])
async def test_trusted_ca_still_checks_hostname(certificates, monkeypatch, matches):
    ca_file, _ = certificates
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    async with tls_cloud(certificates, host_matches=matches) as (url, _, _):
        connector = client(url, verify_tls=True)
        connector._ssl_context.verify_flags |= ssl.VERIFY_X509_STRICT
        if matches:
            async with connected(connector):
                pass
        else:
            with pytest.raises(TunnelError, match="tls_verification_failed") as failure:
                await connector.run(reconnect=False)
            reason = failure.value.__context__
            assert isinstance(reason, ssl.SSLCertVerificationError)
            assert reason.verify_code == 64  # OpenSSL IP address mismatch, chain was trusted.


@pytest.mark.parametrize("value", ["false", None, 0, 1])
def test_verify_tls_requires_boolean(value):
    with pytest.raises(TypeError, match="verify_tls"):
        client("https://localhost", verify_tls=value)


@pytest.mark.parametrize("verify", [False, True])
def test_plain_ws_does_not_receive_ssl_context(verify):
    assert client("http://localhost", verify_tls=verify)._ssl_context is None
