"""Plugin scopes remain protected at the original HTTP surfaces."""

import json

import httpx
import pytest

from ksadk.studio.api import create_studio_app
from ksadk.studio.service import StudioService


@pytest.mark.asyncio
async def test_reserved_session_and_run_cannot_use_unscoped_http_routes(tmp_path):
    studio = StudioService(tmp_path)
    # Server-owned reservation fixture; none of these records can be posted by
    # a browser. No DSH/model execution is required to exercise route admission.
    studio.execution_host._db.execute(
        "INSERT INTO plugin_session_scopes VALUES (?,?)",
        ("member-session", json.dumps({})),
    )
    studio.execution_host._db.execute(
        "INSERT INTO execution_policy_refs VALUES (?,?,?,?,?)",
        ("policy", "{}", "{}", "command", "member-run"),
    )
    app = create_studio_app(tmp_path, service=studio, security_enabled=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http:
            for method, path, body in [
                ("DELETE", "/api/v1/sessions/member-session", None),
                ("GET", "/api/v1/sessions/member-session/events", None),
                ("GET", "/api/v1/sessions/member-session/events/stream", None),
                ("POST", "/api/v1/runs/member-run:cancel", None),
                ("POST", "/api/v1/runs/member-run:pause", None),
                ("POST", "/api/v1/runs/member-run:resume", None),
                ("POST", "/agentengine/api/v1/GetSession", {"SessionId": "member-session"}),
                (
                    "POST",
                    "/agentengine/api/v1/ListSessionMessages",
                    {"SessionId": "member-session"},
                ),
                ("POST", "/agentengine/api/v1/ListSessionEvents", {"SessionId": "member-session"}),
                ("POST", "/agentengine/api/v1/CancelRun", {"InvocationId": "member-run"}),
                (
                    "POST",
                    "/agentengine/api/v1/SubmitInteraction",
                    {
                        "RunId": "member-run",
                        "InteractionId": "approval",
                    },
                ),
            ]:
                response = await http.request(method, path, json=body)
                assert response.status_code == 403, (path, response.status_code, response.text)
                assert "plugin_" in response.text
    finally:
        await studio.aclose()
