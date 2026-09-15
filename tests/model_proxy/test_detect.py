"""能力矩阵探测单测(httpx MockTransport 假上游,不打网络)。"""

import json

import httpx

from ksadk.model_proxy.detect import (
    _classify_responses_error,
    probe_responses_capability,
)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_probe_supported_valid_structure():
    requests = []

    def h(req):
        requests.append(req.read())
        return httpx.Response(200, json={"id": "r", "output": [], "status": "completed"})

    c = probe_responses_capability(_client(h), "https://x/v1", "k", "m")
    assert c.verdict == "supported"
    assert c.responses_supported is True
    assert c.tool_types == {"namespace", "custom", "web_search"}
    assert c.preferred_protocol == "responses"
    assert len(requests) == 4


def test_probe_responses_without_codex_namespace_prefers_chat_proxy():
    requests = []

    def h(req):
        payload = json.loads(req.read())
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json={"id": "r", "output": [], "status": "completed"})
        return httpx.Response(
            400,
            text=(
                "Invalid value: namespace, Supported values are: function, mcp, knowledge_search"
            ),
        )

    c = probe_responses_capability(_client(h), "https://x/v1", "k", "m")

    assert c.verdict == "supported"
    assert c.responses_supported is True
    assert c.tool_types == set()
    assert c.preferred_protocol == "chat"
    assert requests[1]["input"][0] == {
        "type": "additional_tools",
        "role": "developer",
        "tools": [
            {
                "type": "namespace",
                "name": "functions",
                "description": "KsADK Codex capability probe",
                "tools": [
                    {
                        "type": "function",
                        "name": "probe",
                        "description": "Probe Codex namespace tool support",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    }
                ],
            }
        ],
    }


def test_probe_namespace_failure_in_responses_envelope_prefers_chat_proxy():
    requests = 0

    def h(req):
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, json={"output": [], "status": "completed"})
        return httpx.Response(
            200,
            json={
                "output": [],
                "status": "failed",
                "error": {"message": "Invalid value: namespace"},
            },
        )

    c = probe_responses_capability(_client(h), "https://x/v1", "k", "m")

    assert c.verdict == "supported"
    assert c.responses_supported is True
    assert c.tool_types == set()
    assert c.preferred_protocol == "chat"


def test_probe_glm_envelope_without_web_search_keeps_native_responses():
    """真实 GLM 形态:namespace/custom 可用,但 Codex web_search 枚举被拒。"""

    requests = []

    def h(req):
        payload = json.loads(req.read())
        requests.append(payload)
        if len(requests) < 4:
            return httpx.Response(200, json={"output": [], "status": "completed"})
        return httpx.Response(
            400,
            text=(
                "Invalid value: web_search, Supported values are: function, mcp, knowledge_search"
            ),
        )

    c = probe_responses_capability(_client(h), "https://x/v1", "k", "glm-5.1")

    assert c.verdict == "supported"
    assert c.responses_supported is True
    assert c.tool_types == {"namespace", "custom"}
    assert c.preferred_protocol == "responses"
    assert requests[3]["input"][0] == {
        "type": "additional_tools",
        "role": "developer",
        "tools": [{"type": "web_search"}],
    }


def test_probe_200_but_not_responses_structure_gateway_fake_ok():
    # 200 但 body 是错误页(无 output/status) -> 不算 supported
    def h(req):
        return httpx.Response(200, json={"error": "something"})

    c = probe_responses_capability(_client(h), "https://x/v1", "k", "m")
    assert c.verdict == "unsupported"
    assert c.responses_supported is False
    assert c.preferred_protocol == "chat"


def test_probe_404_unsupported():
    def h(req):
        return httpx.Response(404, text="not found")

    c = probe_responses_capability(_client(h), "https://x/v1", "k", "m")
    assert c.verdict == "unsupported"
    assert c.preferred_protocol == "chat"


def test_probe_400_unknown_endpoint_unsupported():
    def h(req):
        return httpx.Response(400, text='{"error":"unknown endpoint"}')

    c = probe_responses_capability(_client(h), "https://x/v1", "k", "m")
    assert c.verdict == "unsupported"


def test_probe_400_generic_is_unknown_not_unsupported():
    # 400 但不像协议级否定(如字段问题) -> unknown,不固化
    def h(req):
        return httpx.Response(400, text='{"error":"invalid field foo"}')

    c = probe_responses_capability(_client(h), "https://x/v1", "k", "m")
    assert c.verdict == "unknown"
    assert c.responses_supported is None
    assert c.preferred_protocol == "chat"  # 保守走转换层


def test_probe_403_auth_is_unknown_not_unsupported():
    # glm-5.1 实测:403 unauthorized consumer 是 key 权限问题,非 endpoint 不存在
    def h(req):
        return httpx.Response(403, text="unauthorized consumer")

    c = probe_responses_capability(_client(h), "https://x/v1", "k", "m")
    assert c.verdict == "unknown"
    assert c.responses_supported is None


def test_probe_timeout_is_unknown():
    def h(req):
        raise httpx.ConnectTimeout("slow")

    c = probe_responses_capability(_client(h), "https://x/v1", "k", "m", timeout=0.5)
    assert c.verdict == "unknown"
    assert c.preferred_protocol == "chat"


def test_probe_500_is_unknown():
    def h(req):
        return httpx.Response(500, text="boom")

    c = probe_responses_capability(_client(h), "https://x/v1", "k", "m")
    assert c.verdict == "unknown"


def test_classify_error_table():
    assert _classify_responses_error(404, "") == "unsupported"
    assert _classify_responses_error(405, "") == "unsupported"
    assert _classify_responses_error(400, "Unknown endpoint") == "unsupported"
    assert _classify_responses_error(400, "bad field") == "unknown"
    assert _classify_responses_error(401, "") == "unknown"
    assert _classify_responses_error(403, "") == "unknown"
    assert _classify_responses_error(500, "") == "unknown"
    assert _classify_responses_error(429, "") == "unknown"


def test_probe_async_supported():
    async def h(req):
        return httpx.Response(200, json={"output": [], "status": "completed"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(h))

    async def run():
        return await probe_responses_capability(client, "https://x/v1", "k", "m")

    import asyncio

    c = asyncio.run(run())
    assert c.verdict == "supported"
    assert c.preferred_protocol == "responses"


def test_probe_async_responses_without_namespace_prefers_chat():
    requests = 0

    async def h(req):
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, json={"output": [], "status": "completed"})
        return httpx.Response(400, text="Invalid value: namespace")

    client = httpx.AsyncClient(transport=httpx.MockTransport(h))

    async def run():
        return await probe_responses_capability(client, "https://x/v1", "k", "m")

    import asyncio

    c = asyncio.run(run())
    assert c.verdict == "supported"
    assert c.responses_supported is True
    assert c.tool_types == set()
    assert c.preferred_protocol == "chat"
