"""极简 codex 对话 webui(端到端验证用,非生产入口)。

绕过 ksadk web 的框架检测(它不认 codex):直接用 AsyncCodexClient + 一个
最小 FastAPI 提供 HTML 页 + /chat SSE,在浏览器里展示 codex runtime 经
model_proxy 代理跑星流 chat 模型的真实对话流。

用法:
  KSADK_CODEX_USE_PROXY=1 OPENAI_API_BASE=https://kspmas.ksyun.com/v1 \
  OPENAI_API_KEY=xxx CODEX_HOME=/tmp/codex_clean_runtime \
  uv run python -m ksadk.model_proxy.demo_webui --port 8877
"""

from __future__ import annotations

import json
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse

# ruff: noqa: E501  内联 HTML/CSS/JS 演示字面量,长行不拆

_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>codex runtime e2e</title>
<style>
body{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem}
#log{border:1px solid #ddd;border-radius:8px;padding:1rem;min-height:200px;white-space:pre-wrap;font-family:ui-monospace,monospace}
.u{color:#0969da}.a{color:#1a7f37}.t{color:#666}
input{width:70%;padding:.5rem}button{padding:.5rem 1rem}
</style></head><body>
<h3>codex runtime → model_proxy → 星流 chat</h3>
<form id="f"><input id="q" placeholder="说点什么..." autocomplete="off">
<button>发送</button></form>
<div id="log"></div>
<script>
const log=document.getElementById('log');
function add(cls,text){const d=document.createElement('div');d.className=cls;d.textContent=text;log.appendChild(d);log.scrollTop=log.scrollHeight;}
document.getElementById('f').onsubmit=async e=>{
  e.preventDefault();const q=document.getElementById('q').value;if(!q)return;
  add('u','你: '+q);document.getElementById('q').value='';const t=document.createElement('div');t.className='t';t.textContent='(流式中...)';log.appendChild(t);
  const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q})});
  const reader=r.body.getReader();const dec=new TextDecoder();t.remove();let buf='';
  while(true){const{value,done}=await reader.read();if(done)break;buf+=dec.decode(value);const lines=buf.split('\\n');buf=lines.pop();
    for(const ln of lines){if(!ln.startsWith('data:'))continue;try{const ev=JSON.parse(ln.slice(5).trim());
      if(ev.delta)add('a',ev.delta);if(ev.reply)add('a','✅ '+ev.reply);if(ev.error)add('t','❌ '+ev.error);}catch{}}}
};
</script></body></html>"""


def create_demo_app() -> FastAPI:
    from ksadk.codex.client import AsyncCodexClient

    app = FastAPI()
    client: dict = {"c": None, "tid": None}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _PAGE

    @app.post("/chat")
    async def chat(req: Request):
        body = await req.json()
        q = body.get("q", "")

        async def gen():
            if client["c"] is None:
                c = AsyncCodexClient()
                client["c"] = c
                client["tid"] = await c.start_thread(
                    config={
                        "model": os.environ.get("OPENAI_MODEL_NAME", "glm-5.2"),
                        "sandbox_read_only": True,
                    }
                )
            try:
                async for ev in client["c"].run_turn(client["tid"], q):
                    m = ev.get("method", "")
                    if m == "item/agentMessage/delta":
                        yield f"data: {json.dumps({'delta': ev['params'].get('delta', '')}, ensure_ascii=False)}\n\n"
                    elif m == "item/completed":
                        it = ev["params"]["item"]
                        if it.get("type") == "agentMessage" and it.get("text"):
                            yield f"data: {json.dumps({'reply': it['text']}, ensure_ascii=False)}\n\n"
                    elif m == "error":
                        yield f"data: {json.dumps({'error': json.dumps(ev.get('params', {}).get('error', {}), ensure_ascii=False)[:200]}, ensure_ascii=False)}\n\n"
            except Exception as e:  # noqa: BLE001
                yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.on_event("shutdown")
    async def shutdown():
        if client["c"] is not None:
            await client["c"].close()

    return app


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8877)
    args = p.parse_args()
    uvicorn.run(create_demo_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
