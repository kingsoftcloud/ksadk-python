"""Live sandbox-tab verification: real Studio API + fixture UI plugin + Chrome.

Boots a throwaway Studio instance on a free port with a fixture Cordis plugin
that ships a sandbox-compatible client bundle, then drives a real browser to
confirm the plugin's workspace tab renders inside an opaque-origin iframe and
the fixture_echo tool round-trips through the /ui-sessions relay.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import uvicorn
from playwright.async_api import async_playwright, expect

from ksadk.plugins.bridges.dsh import DshProfilePluginBridge
from ksadk.plugins.dsh_toolchain import DshToolchainManager
from ksadk.studio.api import create_studio_app
from ksadk.studio.dsh_capability_service import StudioDshCapabilityService
from ksadk.studio.dsh_ui_sandbox import DshUiSandboxSessionStore
from ksadk.studio.service import StudioService

FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "dsh-node-tool-plugin"
PLUGIN_ID = "@ksadk-test/dsh-node-tool-plugin"
PROFILE = "studio-sandbox-verify"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fixture_digest() -> str:
    import hashlib
    return f"sha256:{hashlib.sha256((FIXTURE / 'client.mjs').read_bytes()).hexdigest()}"


def host_page(api: str) -> str:
    return f"""<!doctype html><meta charset=utf-8><title>sandbox verify</title>
<body><div id=status>loading</div>
<iframe id=frame style="width:700px;height:500px;border:1px solid #ccc"
 sandbox="allow-scripts" referrerpolicy="no-referrer" credentialless></iframe>
<script>
const API={json.dumps(api)};
const status=document.getElementById('status');
const frame=document.getElementById('frame');
async function run(){{
  const r=await fetch(API+'/api/v1/plugin-ecosystems/dsh/ui-sessions',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{pluginId:{json.dumps(PLUGIN_ID)},clientDigest:{json.dumps(fixture_digest())}}})}});
  if(!r.ok){{status.textContent='session-fail:'+r.status+' '+await r.text();return;}}
  const session=await r.json();
  status.textContent='session:'+session.uiSessionId;
  frame.src=API+session.frame.url;
  frame.addEventListener('load',()=>{{
    const ch=new MessageChannel();
    let ready=false;
    ch.port1.onmessage=async(e)=>{{
      const m=e.data;
      if(m&&m.kind==='ready'){{ready=true;status.textContent='ready';return;}}
      if(!m||m.kind!=='request')return;
      const rr=await fetch(API+'/api/v1/plugin-ecosystems/dsh/ui-sessions/'+session.uiSessionId+'/messages',{{
        method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{sourceId:session.sourceId,frameOrigin:'null',message:m}})}});
      const reply=await rr.json();
      ch.port1.postMessage(reply);
    }};
    ch.port1.start();
    frame.contentWindow.postMessage(session.handshake,'*',[ch.port2]);
  }});
}}
run().catch(e=>status.textContent='err:'+e);
</script>"""


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sandboxverify_"))
    tc = DshToolchainManager(base_dir=tmp / "tc")
    tc.install()
    cmd = tc.require_command()
    dsh_home = tmp / "dsh-home"
    ws = tmp / "ws"
    ws.mkdir()
    bridge = DshProfilePluginBridge(dsh_home=dsh_home, profile=PROFILE, dsh_command=cmd, cwd=ws)
    with bridge:
        bridge.install_plugin(str(FIXTURE), accept_host_permissions=True)
        bridge.set_enabled(PLUGIN_ID, enabled=True)

    cap = StudioDshCapabilityService(ws, dsh_home=dsh_home, profile=PROFILE, dsh_command=cmd)
    service = StudioService(ws, dsh_capability_service=cap, dsh_ui_sessions=DshUiSandboxSessionStore())
    # The route handlers' call_dsh reads dsh_home/profile from env, not the
    # capability service instance — align them so ui-sessions finds the plugin.
    os.environ["KSADK_DSH_HOME"] = str(dsh_home)
    os.environ["KSADK_DSH_PROFILE"] = PROFILE
    app = create_studio_app(ws, service=service, security_enabled=False)

    # Serve the host page from the same origin as the API to avoid cross-origin fetch.
    from fastapi import Response

    @app.get("/sandbox-verify-host")
    def _host() -> Response:
        return Response(content=host_page(f"http://127.0.0.1:{api_port}"), media_type="text/html")

    api_port = free_port()
    api = f"http://127.0.0.1:{api_port}"
    cfg = uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    host = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (BaseHTTPRequestHandler,), {"do_GET": lambda s: s.send_response(200) or s.send_header("Content-Type", "text/html") or s.end_headers() or s.wfile.write(host_page(api).encode()) or None, "log_message": lambda *a: None}))
    host_port = host.server_port
    host_origin = f"http://127.0.0.1:{host_port}"
    ht = threading.Thread(target=host.serve_forever, daemon=True)
    ht.start()

    try:
        async with httpx.AsyncClient(base_url=api, timeout=30) as c:
            for _ in range(60):
                try:
                    if (await c.get("/api/v1/system/health")).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=True, executable_path=chrome)
            page = await b.new_page()
            errors: list[str] = []
            page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
            page.on("requestfailed", lambda r: errors.append(f"reqfail: {r.url} {r.failure}"))
            await page.goto(f"{api}/sandbox-verify-host", wait_until="load")
            # Probe the capabilities endpoint to see what tools the descriptor exposes.
            async with httpx.AsyncClient() as c:
                cap = await c.get(f"{api}/api/v1/plugin-ecosystems/dsh/capabilities", timeout=15)
                print("CAP status:", cap.status_code)
                if cap.status_code == 200:
                    tools = cap.json().get("tools", [])
                    print("descriptor tools:", [t.get("name") for t in tools])
            await asyncio.sleep(3)
            status = await page.locator("#status").text_content()
            print("STATUS:", status)
            if errors:
                print("CONSOLE ERRORS:", errors[:5])
            frame = page.frame_locator("#frame")
            try:
                await expect(frame.locator("body")).to_have_attribute("data-fixture-status", "passed", timeout=30000)
                print("RESULT: PASS — sandbox tab rendered, fixture_echo round-tripped")
            except Exception as e:
                print("RESULT: FAIL —", str(e)[:200])
                try:
                    detail = await frame.locator("body").get_attribute("data-fixture-detail", timeout=2000)
                    print("DETAIL:", detail)
                except Exception:
                    pass
            await b.close()
    finally:
        host.shutdown()
        server.should_exit = True
        await cap.aclose()


if __name__ == "__main__":
    asyncio.run(main())
