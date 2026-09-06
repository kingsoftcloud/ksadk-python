"""Browser E2E: real studio/api.py + real DSH capability host + sandbox frame.

Unlike ``dsh_ui_sandbox_browser_e2e`` (which uses a standalone fixture server),
this test mounts the canonical Studio API with a live DSH capability sidecar
running the fixture Cordis plugin.  The browser creates a UI session through
the real ``/ui-sessions`` endpoint, the sandbox frame renders, and the
``fixture_echo`` tool round-trips through the real ``/messages`` relay to the
sidecar and back — proving the end-to-end loop that §8.6 T2 requires.

Gated on ``KSADK_DSH_TOOLCHAIN_E2E=1`` (installs the pinned public npm
toolchain) and requires a Chromium executable on the host.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from playwright.async_api import async_playwright, expect

from ksadk.plugins.bridges.dsh import DshProfilePluginBridge
from ksadk.plugins.dsh_toolchain import DshToolchainManager
from ksadk.studio.api import create_studio_app
from ksadk.studio.dsh_capability_service import StudioDshCapabilityService
from ksadk.studio.dsh_ui_sandbox import DshUiSandboxSessionStore
from ksadk.studio.service import StudioService

FIXTURE_BUNDLE = Path(__file__).parents[2] / "fixtures" / "dsh-node-tool-plugin"
PLUGIN_ID = "@ksadk-test/dsh-node-tool-plugin"
PROFILE = "ksadk-ui-sandbox-e2e"

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KSADK_DSH_TOOLCHAIN_E2E") != "1",
    reason="set KSADK_DSH_TOOLCHAIN_E2E=1 to install the pinned public npm toolchain",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _fixture_client_digest() -> str:
    import hashlib

    content = (FIXTURE_BUNDLE / "client.mjs").read_bytes()
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


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


def _host_page_html(api_origin: str) -> str:
    """Minimal host page: create session, mount frame, relay messages to API.

    Replicates ``attachDshUiSandbox`` in vanilla JS so the test exercises the
    same protocol the react-ui component uses, against the real backend.
    """
    return f"""<!doctype html><meta charset=utf-8><title>DSH sandbox real-API host</title>
<body><div id="status">loading</div>
<iframe id="frame" style="width:600px;height:400px;border:1px solid #ccc"
  sandbox="allow-scripts" referrerpolicy="no-referrer" credentialless></iframe>
<script>
const API = {json.dumps(api_origin)};
const status = document.getElementById('status');
const frame = document.getElementById('frame');

async function run() {{
  // 1. Create a UI session through the real API.
  const digest = {json.dumps(_fixture_client_digest())};
  const resp = await fetch(API + '/api/v1/plugin-ecosystems/dsh/ui-sessions', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      pluginId: {json.dumps(PLUGIN_ID)},
      clientDigest: digest,
      toolIds: ['fixture_echo'],
    }}),
  }});
  if (!resp.ok) {{ status.textContent = 'session-create-failed:' + resp.status; return; }}
  const session = await resp.json();

  // 2. Mount the sandbox frame from the real API.
  frame.src = API + session.frame.url;

  // 3. MessageChannel handshake + relay, same as attachDshUiSandbox.
  frame.addEventListener('load', () => {{
    const channel = new MessageChannel();
    let ready = false;
    channel.port1.onmessage = async (event) => {{
      const msg = event.data;
      if (msg && msg.kind === 'ready' && msg.sessionId === session.uiSessionId) {{
        ready = true;
        status.textContent = 'ready';
        return;
      }}
      if (!msg || msg.kind !== 'request') return;
      // Relay to the real /messages endpoint.
      const relayResp = await fetch(
        API + '/api/v1/plugin-ecosystems/dsh/ui-sessions/' +
          encodeURIComponent(session.uiSessionId) + '/messages',
        {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{
            sourceId: session.sourceId,
            frameOrigin: 'null',
            message: msg,
          }}),
        }},
      );
      const reply = await relayResp.json();
      if (!reply.ok) {{
        status.textContent = 'relay-failed:' + JSON.stringify(reply.error || reply);
      }}
      channel.port1.postMessage(reply);
    }};
    channel.port1.start();
    // The iframe is opaque-origin; postMessage target must be '*'.
    frame.contentWindow.postMessage(session.handshake, '*', [channel.port2]);
  }});
}}
run().catch(err => {{ status.textContent = 'error:' + String(err); }});
</script>"""


class _HostServer(ThreadingHTTPServer):
    api_origin: str

    def __init__(self, api_origin: str) -> None:
        super().__init__(("127.0.0.1", 0), _HostHandler)
        self.api_origin = api_origin


class _HostHandler(BaseHTTPRequestHandler):
    server: _HostServer

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            body = _host_page_html(self.server.api_origin).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, _format: str, *_args) -> None:
        return


@pytest.mark.asyncio
async def test_real_api_sandbox_round_trip(tmp_path: Path) -> None:
    # --- 1. Set up the real DSH toolchain + bridge + capability host ---
    toolchain = DshToolchainManager(base_dir=tmp_path / "toolchains")
    toolchain.install()
    command = toolchain.require_command()
    dsh_home = tmp_path / "dsh-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    bridge = DshProfilePluginBridge(
        dsh_home=dsh_home,
        profile=PROFILE,
        dsh_command=command,
        cwd=workspace,
    )
    with bridge:
        bridge.install_plugin(str(FIXTURE_BUNDLE), accept_host_permissions=True)
        bridge.set_enabled(PLUGIN_ID, enabled=True)

    # --- 2. Mount the real Studio API with the live capability service ---
    capability_service = StudioDshCapabilityService(
        workspace,
        dsh_home=dsh_home,
        profile=PROFILE,
        dsh_command=command,
    )
    service = StudioService(
        workspace,
        dsh_capability_service=capability_service,
        dsh_ui_sessions=DshUiSandboxSessionStore(),
    )
    app = create_studio_app(workspace, service=service, security_enabled=False)

    api_port = _free_port()
    api_origin = f"http://127.0.0.1:{api_port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # --- 3. Serve the host page ---
    host_server = _HostServer(api_origin)
    host_origin = f"http://127.0.0.1:{host_server.server_port}"
    host_thread = threading.Thread(target=host_server.serve_forever, daemon=True)
    host_thread.start()

    try:
        # Wait for the API to be ready.
        async with httpx.AsyncClient(base_url=api_origin, timeout=30) as client:
            for _ in range(60):
                try:
                    r = await client.get("/api/v1/system/health")
                    if r.status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("studio API did not become ready")

        # --- 4. Drive the browser (async Playwright — no sync API in asyncio loop) ---
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, **_browser_launch_options())
            try:
                page = await browser.new_page()
                await page.goto(host_origin, wait_until="load")

                # The host page creates the session, mounts the frame, and
                # relays the fixture_echo call. The client bundle inside the
                # iframe sets data-fixture-status="passed" on success.
                frame = page.frame_locator("#frame")
                await expect(frame.locator("body")).to_have_attribute(
                    "data-fixture-status", "passed", timeout=30000
                )
                await expect(page.locator("#status")).to_have_text("ready")

                # Verify the capability token never appears in the DOM or URL.
                iframe = page.locator("#frame")
                assert "allow-same-origin" not in (await iframe.get_attribute("sandbox") or "")
                page_content = await page.content()
                # The host page should not leak any capability token.
                assert "capabilityToken" not in page_content
            finally:
                await browser.close()
    finally:
        host_server.shutdown()
        host_server.server_close()
        host_thread.join(timeout=5)
        server.should_exit = True
        server_thread.join(timeout=10)
        await capability_service.aclose()
