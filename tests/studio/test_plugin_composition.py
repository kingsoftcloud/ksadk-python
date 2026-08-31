"""Studio composition uses internal contracts without a third package format."""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from ksadk.plugins.providers.harness_dsh import shipped_harness_dsh_bundle
from ksadk.plugins.providers.legacy_catalog import builtin_agent_provider_manifests
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    CapabilityBinding,
    Instructions,
    MCPServerRef,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService


def _model() -> ModelSpec:
    return ModelSpec(
        model="fixture-model",
        endpoint_url="https://model.example.test/v1/chat/completions",
        credential_ref="env://MODEL_API_KEY",
    )


def _security() -> SecuritySpec:
    return SecuritySpec(
        network=NetworkPolicy(allowed_hosts=["model.example.test", "mcp.example.test"])
    )


def _skill_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: concise-report\ndescription: Be concise.\n"
            "version: 1.0.0\n---\nBe concise.\n",
        )
    return output.getvalue()


def _managed_harness_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "dsh-home"
    profile = home / "profiles" / "studio"
    installed = profile / "node_modules" / "@kingsoftcloud" / "ksadk-harness-provider"
    installed.parent.mkdir(parents=True)
    shutil.copytree(shipped_harness_dsh_bundle().root, installed)
    (profile / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"@kingsoftcloud/ksadk-harness-provider": "1.0.0"},
                "dsh": {"profile": {"bundles": ["@kingsoftcloud/ksadk-harness-provider"]}},
            }
        ),
        encoding="utf-8",
    )
    executable = tmp_path / "dsh-fixture"
    executable.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *--version*) echo 0.1.1-rc.2;;\n"
        "  *--dump-config*) echo 'profile: studio; harness: 1.0.0';;\n"
        "  *) exit 2;;\n"
        "esac\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("KSADK_DSH_HOME", str(home))
    monkeypatch.setenv("KSADK_DSH_PROFILE", "studio")
    monkeypatch.setenv("KSADK_DSH_BIN", str(executable))


def test_no_agent_provider_bypasses_the_registered_dsh_profile() -> None:
    assert builtin_agent_provider_manifests() == ()


@pytest.mark.asyncio
async def test_harness_build_writes_profile_lock_and_catalog_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _managed_harness_profile(tmp_path, monkeypatch)
    studio = StudioService(tmp_path)
    mcp = studio.catalog.create_mcp_server(
        display_name="Fixture MCP",
        description="",
        server=MCPServerRef(
            name="fixture-mcp",
            version="1.0.0",
            transport="http",
            endpoint_url="https://mcp.example.test/rpc",
        ),
    )
    skill = studio.catalog.import_skill_zip(_skill_zip(), filename="skill.zip")
    studio.create_agent(
        agent_id="harness-agent",
        name="Harness Agent",
        spec=AgentSpec(
            runtime=RuntimeRef(type="harness"),
            instructions=Instructions(system="Use locked capabilities."),
            model=_model(),
            bindings=AgentBindings(
                mcp_servers=[CapabilityBinding(resource_id=mcp.resource_id)],
                skills=[CapabilityBinding(resource_id=skill.resource_id)],
            ),
            security=_security().model_copy(update={"allowed_permissions": ["process:host-user"]}),
        ),
    )
    record = await studio.ensure_current_build("harness-agent")
    archive = studio.workspace.resolve(record.artifact_path, must_exist=True)
    with zipfile.ZipFile(archive) as bundle:
        profile = json.loads(bundle.read("composition-profile.json"))
        lock = json.loads(bundle.read("plugin-lock.json"))
    assert profile["agentProvider"]["ref"] == "plugin://io.ksadk.harness-provider@1.0.0"
    assert "io.ksadk.mcp.workspace" in {item["id"] for item in lock["plugins"]}
    await studio.aclose()


@pytest.mark.asyncio
async def test_unregistered_provider_fails_closed_without_native_package_store(
    tmp_path: Path,
) -> None:
    studio = StudioService(tmp_path)
    studio.create_agent(
        agent_id="dsh-pending",
        name="DSH pending",
        spec=AgentSpec(
            runtime=RuntimeRef(type="plugin", provider_ref="plugin://io.example.dsh@1.0.0"),
            instructions=Instructions(system="Fail closed."),
            model=_model(),
            security=_security(),
        ),
    )
    with pytest.raises(StudioError) as captured:
        await studio.ensure_current_build("dsh-pending")
    assert captured.value.code == "AGENT_PROVIDER_NOT_REGISTERED"
