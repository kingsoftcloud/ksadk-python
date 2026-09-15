"""真实 Codex 链路的集成测试（opt-in，串行）。

需要 ``KSADK_CODEX_AUTHORING_E2E=1`` 且进程环境里有 kspmas 凭证
（``OPENAI_API_KEY`` / ``OPENAI_API_BASE`` / ``OPENAI_MODEL_NAME``）。
kspmas 有 TPM 限流，本模块所有用例串行执行。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KSADK_CODEX_AUTHORING_E2E") != "1",
        reason="set KSADK_CODEX_AUTHORING_E2E=1 with kspmas credentials to run",
    ),
]


def _kspmas_ready() -> bool:
    return bool(
        os.environ.get("OPENAI_API_KEY")
        and os.environ.get("OPENAI_API_BASE")
        and os.environ.get("OPENAI_MODEL_NAME")
    )


@pytest.fixture
def studio(tmp_path: Path):
    if not _kspmas_ready():
        pytest.skip("kspmas credentials not configured")
    from ksadk.studio.contracts import ModelSpec
    from ksadk.studio.service import StudioService

    service = StudioService(tmp_path)
    profile = service.catalog.create_model_profile(
        name=os.environ["OPENAI_MODEL_NAME"],
        display_name=os.environ["OPENAI_MODEL_NAME"],
        version="1.0.0",
        description="",
        spec=ModelSpec(
            provider="openai-compatible",
            model=os.environ["OPENAI_MODEL_NAME"],
            endpoint_url=f"{os.environ['OPENAI_API_BASE'].rstrip('/')}/chat/completions",
            credential_ref="env://AGENTKIT_MODEL_API_KEY",
        ),
    )
    return service, profile.resource_id


async def test_real_codex_authoring_produces_valid_proposal(studio) -> None:
    service, profile_id = studio
    service.authoring.codex_authoring = None
    result = await service.compose_agent_conversation(
        messages=[
            {
                "role": "user",
                "content": "帮我创建一个代码评审 agent，重点检查空指针和未处理异常，用中文回复。",
            }
        ],
        model_profile_id=profile_id,
        request_id=f"e2e-{uuid.uuid4().hex[:8]}",
    )
    assert result["authoringMode"] == "codex"
    proposal = result["proposal"]
    assert proposal["slug"]
    assert proposal["runtimeType"] in {"codex", "adk", "langgraph"}
    assert proposal["spec"]["instructions"]["system"]
    json.dumps(result["proposal"], ensure_ascii=False)


async def test_real_codex_authoring_incremental_turn(studio) -> None:
    service, profile_id = studio
    service.authoring.codex_authoring = None
    result = await service.compose_agent_conversation(
        messages=[
            {
                "role": "user",
                "content": "创建一个天气查询 agent，name 叫天气助手。",
            },
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "name": "天气助手",
                        "slug": "weather-assistant",
                        "runtimeType": "codex",
                        "description": "查询天气",
                        "spec": {
                            "instructions": {
                                "system": "你是天气助手。",
                                "task": "查询用户指定城市的天气。",
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
            },
            {
                "role": "user",
                "content": "改成代码评审方向，名字叫代码评审助手。",
            },
        ],
        model_profile_id=profile_id,
        request_id=f"e2e-{uuid.uuid4().hex[:8]}",
    )
    assert result["authoringMode"] == "codex"
    proposal = result["proposal"]
    assert "评审" in proposal["name"] or "review" in proposal["slug"]
