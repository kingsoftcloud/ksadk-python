"""Execution contract for the source-free Studio AgentKit Bundle Runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ksadk.detection import FrameworkDetector, FrameworkType
from ksadk.runtime import (
    RuntimeLaunchContext,
    RuntimeServices,
    StartRequest,
    create_runtime_adapter,
)
from ksadk.runtime.agentkit_bundle import AgentkitBundleRuntimeAdapter


def _write_bundle(root: Path, *, capabilities: dict | None = None) -> None:
    root.joinpath("agentengine.yaml").write_text(
        "\n".join(
            [
                "name: yaml-agent",
                "framework: agentkit",
                "bundle: resolved-agent-spec.json",
                "bundle_format: agentkit.bundle/v2",
            ]
        ),
        encoding="utf-8",
    )
    root.joinpath("resolved-agent-spec.json").write_text(
        json.dumps(
            {
                "schemaVersion": "agentkit.resolved/v1",
                "agentId": "yaml-agent",
                "sourceRevision": 1,
                "instructions": {"system": "Be brief."},
                "model": {
                    "provider": "openai-compatible",
                    "model": "test-model",
                    "endpointUrl": "https://model.example/v1/chat/completions",
                    "credentialRef": "env://MODEL_API_KEY",
                    "parameters": {"temperature": 0.2, "maxTokens": 32},
                },
                "capabilities": capabilities or {},
                "execution": {"timeoutSeconds": 30},
                "context": {"maxInputTokens": 4096, "reserveOutputTokens": 512},
                "security": {"network": {"mode": "open"}},
                "evaluation": {},
                "sourceDigest": "sha256:source",
                "resolvedDigest": "sha256:resolved",
            }
        ),
        encoding="utf-8",
    )


def _context(root: Path) -> RuntimeLaunchContext:
    return RuntimeLaunchContext(
        runtime_type="agentkit",
        project_dir=root,
        detection=FrameworkDetector(str(root)).detect(),
        config=FrameworkDetector(str(root)).detect().raw_config,
        services=RuntimeServices(agentkit_completion=lambda _model, _payload: "Bundle reply"),
    )


def test_detector_and_default_factory_select_source_free_agentkit_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    _write_bundle(tmp_path)

    detection = FrameworkDetector(str(tmp_path)).detect()
    adapter = create_runtime_adapter(_context(tmp_path))

    assert detection.type is FrameworkType.AGENTKIT
    assert detection.entry_point == ""
    assert detection.package_path == str(tmp_path)
    assert isinstance(adapter, AgentkitBundleRuntimeAdapter)


@pytest.mark.asyncio
async def test_agentkit_bundle_runtime_streams_canonical_model_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    _write_bundle(tmp_path)
    adapter = create_runtime_adapter(_context(tmp_path))

    await adapter.preflight()
    handle = await adapter.start(
        StartRequest(input="hello", user_id="user-1", session_id="session-1")
    )
    events = [event async for event in adapter.stream(handle)]

    assert [event.event_type for event in events] == [
        "run.started",
        "item.started",
        "item.completed",
        "run.completed",
    ]
    assert events[2].snapshot.parts[0].text == "Bundle reply"


@pytest.mark.asyncio
async def test_agentkit_bundle_runtime_rejects_uninstalled_tool_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    _write_bundle(
        tmp_path,
        capabilities={"tools": [{"name": "builtin.echo", "version": "1.0.0"}]},
    )

    with pytest.raises(ValueError, match="no installed capability provider"):
        await create_runtime_adapter(_context(tmp_path)).preflight()
