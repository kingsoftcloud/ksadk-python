"""Deterministic IAM/MCP simulator. Never mount this router in the product."""

import asyncio
import base64
import hashlib
import html
import secrets
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response


def create_provider(base_url):
    app = FastAPI()
    clients, codes, tokens, refresh = {}, {}, {}, {}
    stats = {"registrations": 0, "exchanges": 0, "refreshes": 0, "calls": 0, "key_calls": 0}
    app.state.stats = stats
    app.state.failure = None

    @app.post("/fixture/control")
    async def fixture_control(request: Request):
        body = await request.json()
        if body.get("failure") not in {
            None,
            "token",
            "permission",
            "empty",
            "disconnect",
            "slow_tool",
            "lost_tool_reply",
            "scope",
            "invalid_token",
        }:
            return JSONResponse({"error": "invalid_fixture_mode"}, status_code=400)
        app.state.failure = body.get("failure")
        return {"stats": stats}

    @app.get("/.well-known/oauth-authorization-server")
    async def metadata():
        return {
            "issuer": base_url,
            "authorization_endpoint": base_url + "/authorize",
            "token_endpoint": base_url + "/token",
            "registration_endpoint": base_url + "/register",
            "revocation_endpoint": base_url + "/revoke",
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["mcp:read"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True,
        }

    @app.get("/.well-known/oauth-protected-resource")
    async def resource():
        return {
            "resource": base_url + "/mcp",
            "authorization_servers": [base_url],
            "scopes_supported": ["mcp:read"],
        }

    @app.post("/register")
    async def register(request: Request):
        body = await request.json()
        cid = secrets.token_hex(12)
        clients[cid] = body
        stats["registrations"] += 1
        return JSONResponse({**body, "client_id": cid}, status_code=201)

    @app.get("/authorize")
    async def authorize(request: Request):
        fields = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
            for k, v in request.query_params.items()
        )
        return HTMLResponse(
            '<!doctype html><meta charset="utf-8"><title>模拟 IAM 授权</title>'
            '<h1>模拟 IAM</h1><p>仅用于本地 MCP OAuth 验证</p>'
            '<form method="post" action="/consent">'
            + fields
            + '<label>测试用户<select name="user">'
            '<option>alice</option><option>bob</option></select></label>'
            '<button name="decision" value="allow">允许只读访问</button>'
            '<button name="decision" value="deny">拒绝</button></form>'
        )

    @app.post("/consent")
    async def consent(request: Request):
        body = dict(await request.form())
        client = clients.get(body.get("client_id"))
        requested_redirect_uri = body.get("redirect_uri")
        if not client or requested_redirect_uri not in client["redirect_uris"]:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        redirect_uri = next((uri for uri in client["redirect_uris"] if uri == requested_redirect_uri), None)
        if not redirect_uri:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        values = {"state": body["state"], "iss": base_url}
        if body.get("decision") != "allow":
            values["error"] = "access_denied"
        else:
            code = secrets.token_urlsafe(24)
            codes[code] = body
            values["code"] = code
        return RedirectResponse(redirect_uri + "?" + urlencode(values), status_code=303)

    @app.post("/token")
    async def token(request: Request):
        body = dict(await request.form())
        if app.state.failure == "token":
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if body.get("grant_type") == "authorization_code":
            saved = codes.pop(body.get("code"), None)
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(body.get("code_verifier", "").encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            if (
                not saved
                or challenge != saved["code_challenge"]
                or saved["code_challenge_method"] != "S256"
                or any(
                    body.get(k) != saved.get(k) for k in ("client_id", "redirect_uri", "resource")
                )
            ):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            user = saved["user"]
            stats["exchanges"] += 1
        else:
            saved = refresh.pop(body.get("refresh_token"), None)
            if (
                not saved
                or saved["client_id"] != body.get("client_id")
                or body.get("resource") != base_url + "/mcp"
            ):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            user = saved["user"]
            stats["refreshes"] += 1
        access, renewal = (
            "fixture-access-" + secrets.token_hex(16),
            "fixture-refresh-" + secrets.token_hex(16),
        )
        tokens[access] = user
        refresh[renewal] = {"user": user, "client_id": body["client_id"]}
        return {
            "access_token": access,
            "token_type": "Bearer",
            "refresh_token": renewal,
            "expires_in": 3600,
            "scope": "mcp:read",
        }

    @app.post("/revoke")
    async def revoke(request: Request):
        body = await request.form()
        tokens.pop(body.get("token"), None)
        refresh.pop(body.get("token"), None)
        return {}

    @app.api_route("/key-mcp", methods=["GET", "POST", "DELETE"])
    @app.api_route("/public-mcp", methods=["GET", "POST", "DELETE"])
    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    async def mcp(request: Request):
        key_mode = request.url.path == "/key-mcp"
        public_mode = request.url.path == "/public-mcp"
        if key_mode:
            if request.headers.get("Authorization") != "Bearer fixture-static-key":
                return JSONResponse({"error": "invalid_key"}, status_code=401)
            user = "key-fixture"
        elif public_mode:
            user = "public-fixture"
        else:
            user = tokens.get(request.headers.get("Authorization", "").removeprefix("Bearer "))
        if not user or app.state.failure == "invalid_token":
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{base_url}'
                        '/.well-known/oauth-protected-resource", scope="mcp:read"'
                    )
                },
            )
        if app.state.failure == "permission":
            return JSONResponse({"error": "business_permission_denied"}, status_code=403)
        if app.state.failure == "scope":
            return JSONResponse(
                {"error": "insufficient_scope"},
                status_code=403,
                headers={"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="mcp:read"'},
            )
        if app.state.failure == "disconnect":
            return Response(status_code=503)
        if request.method != "POST":
            return Response(status_code=405 if request.method == "GET" else 200)
        body = await request.json()
        if "id" not in body:
            return Response(status_code=202)
        method = body.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "local-oauth-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": []
                if app.state.failure == "empty"
                else [
                    {
                        "name": "read_snapshot",
                        "description": "Read the current user's fixture data",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        elif method == "tools/call":
            stats["key_calls" if key_mode else "calls"] += 1
            if app.state.failure == "lost_tool_reply":
                return Response(status_code=503)  # The call ran, but no business result arrived.
            if app.state.failure == "slow_tool":
                await asyncio.sleep(30)
            result = {
                "content": [{"type": "text", "text": f"private snapshot for {user}"}],
                "isError": False,
            }
        elif method == "ping":
            result = {}
        else:
            return {
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32601, "message": "Unknown method"},
            }
        return {"jsonrpc": "2.0", "id": body["id"], "result": result}

    return app
