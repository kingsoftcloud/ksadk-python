"""Standalone browser fixture for the credentialless DSH UI sandbox shell.

This does not mount routes into ``studio/api.py``.  It proves the generated
document and MessageChannel protocol in a tiny local server so the canonical
Studio source checkout can reuse the same fixture when it wires the host UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from ksadk.studio.dsh_ui_sandbox import (
    DshUiSandboxSessionStore,
    dsh_ui_client_bundle_headers,
    render_dsh_ui_sandbox_document,
)

FIXTURE_CLIENT = Path(__file__).with_name("fixtures") / "dsh_ui_sandbox_client.js"


class _BrowserFixture:
    def __init__(self, origin: str) -> None:
        self.client = FIXTURE_CLIENT.read_bytes()
        self.digest = f"sha256:{hashlib.sha256(self.client).hexdigest()}"
        self.store = DshUiSandboxSessionStore()
        self.grant = self.store.create_session(
            plugin_id="@example/safe-ui",
            extension_id="fixture.tab",
            client_digest=self.digest,
            descriptor_digest=self.digest,
            generation_id="dshgen_" + "g" * 32,
            parent_origin=origin,
            allowed_tool_ids=("@example/read",),
        )
        bundle_url = "/api/v1/plugin-ecosystems/dsh/sandbox-fixture-client" f"?digest={self.digest}"
        self.document = render_dsh_ui_sandbox_document(
            self.grant,
            client_bundle_url=bundle_url,
            title="DSH UI sandbox fixture",
        )
        self.received: list[dict] = []
        self.probe_hits = 0
        self.bundle_cookie: str | None = None

    def host_html(self) -> bytes:
        handshake = json.dumps(self.grant.host_handshake(), separators=(",", ":")).replace(
            "<", "\\u003c"
        )
        origin = json.dumps(self.grant.parent_origin)
        session_id = json.dumps(self.grant.session_id)
        source_id = json.dumps(self.grant.source_id)
        token = json.dumps(self.grant.capability_token)
        return (
            "<!doctype html><meta charset=utf-8><title>DSH sandbox host fixture</title>"
            '<iframe id="plugin" src="/frame" sandbox="allow-scripts" '
            'referrerpolicy="no-referrer" credentialless></iframe>'
            '<output id="status">loading</output><script>'
            f"const handshake={handshake};const expectedOrigin={origin};"
            f"const expectedSession={session_id};const expectedSource={source_id};"
            f"const expectedToken={token};"
            "const frame=document.getElementById('plugin');"
            "const status=document.getElementById('status');"
            "const methods=[];frame.addEventListener('load',()=>{"
            "const channel=new MessageChannel();"
            "channel.port1.onmessage=(event)=>{const message=event.data;"
            "if(message.kind==='ready'){if(message.sessionId!==expectedSession||"
            "message.sourceId!==expectedSource){status.textContent='invalid-ready';return;}"
            "status.textContent='ready';return;}"
            "if(!message||message.protocolVersion!=='agentkit.dsh-ui/v1'||"
            "message.kind!=='request'||message.sessionId!==expectedSession||"
            "message.sourceId!==expectedSource||message.capabilityToken!==expectedToken){"
            "status.textContent='invalid-request';return;}methods.push(message.method);"
            "const response={protocolVersion:'agentkit.dsh-ui/v1',kind:'response',"
            "sessionId:expectedSession,requestId:message.requestId,ok:true};"
            "if(message.method==='listTools'){response.result={tools:[{id:'@example/read'}]};"
            "channel.port1.postMessage(response);return;}"
            "if(message.method==='callTool'){window.pendingCall=message;return;}"
            "if(message.method==='cancelTool'){response.result={cancelled:true};"
            "channel.port1.postMessage(response);const pending=window.pendingCall;"
            "channel.port1.postMessage({protocolVersion:'agentkit.dsh-ui/v1',kind:'response',"
            "sessionId:expectedSession,requestId:pending.requestId,ok:true,"
            "result:{cancelled:true}});status.textContent=methods.join(',');return;}"
            "status.textContent='forbidden-method';};channel.port1.start();"
            # A sandbox without allow-same-origin has an opaque target origin,
            # so the sender must use "*".  The receiver still authenticates
            # event.source, the exact parent origin, session, and nonce.
            "frame.contentWindow.postMessage(handshake,'*',[channel.port2]);});"
            "</script>"
        ).encode("utf-8")


class _FixtureServer(ThreadingHTTPServer):
    fixture: _BrowserFixture


class _Handler(BaseHTTPRequestHandler):
    server: _FixtureServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._respond(
                self.server.fixture.host_html(),
                {
                    "Content-Type": "text/html; charset=utf-8",
                    "Cache-Control": "no-store",
                    "Set-Cookie": (
                        "agentkit_studio_session=top-secret; HttpOnly; SameSite=Strict; Path=/"
                    ),
                },
            )
            return
        if path == "/frame":
            document = self.server.fixture.document
            self._respond(document.html.encode("utf-8"), dict(document.response_headers))
            return
        if path == "/api/v1/plugin-ecosystems/dsh/sandbox-fixture-client":
            self.server.fixture.bundle_cookie = self.headers.get("Cookie")
            self._respond(
                self.server.fixture.client,
                dict(dsh_ui_client_bundle_headers(self.server.fixture.digest)),
            )
            return
        if path == "/probe":
            self.server.fixture.probe_hits += 1
            self._respond(b"probe should be blocked", {"Content-Type": "text/plain"})
            return
        self.send_error(404)

    def _respond(self, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(200)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        return


def _browser_launch_options() -> dict[str, str]:
    configured = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()
    candidates = (
        configured,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return {"executable_path": candidate}
    return {}


def main() -> None:
    server = _FixtureServer(("127.0.0.1", 0), _Handler)
    origin = f"http://127.0.0.1:{server.server_port}"
    server.fixture = _BrowserFixture(origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, **_browser_launch_options())
            try:
                page = browser.new_page()
                page.goto(origin, wait_until="load")
                frame = page.frame_locator("#plugin")
                expect(frame.locator("body")).to_have_attribute("data-fixture-status", "passed")
                expect(page.locator("#status")).to_have_text("listTools,callTool,cancelTool")
                iframe = page.locator("#plugin")
                expect(iframe).to_have_attribute("sandbox", "allow-scripts")
                assert "allow-same-origin" not in (iframe.get_attribute("sandbox") or "")
                assert server.fixture.probe_hits == 0
                # crossorigin=anonymous plus credentialless prevents the bearer
                # Studio cookie from accompanying the executable bundle fetch.
                assert server.fixture.bundle_cookie is None
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
