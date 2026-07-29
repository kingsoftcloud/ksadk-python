from __future__ import annotations

import sys
import types

import pytest

from ksadk.managed_runtime import (
    ManagedRuntimeError,
    ResolvedRuntime,
    extract_bootstrap_runtime,
    resolve_local_managed_runtime,
    resolve_managed_runtime,
    validate_installed_runtime,
    validate_runtime_binary,
)


def test_extract_bootstrap_runtime_contract():
    resolved = extract_bootstrap_runtime(
        {
            "configs": {
                "runtime.default_version": "0.144.4",
                "runtime.package": "openai-codex==0.144.4",
            }
        },
        "codex",
    )

    assert resolved is not None
    assert resolved.name == "codex"
    assert resolved.version == "0.144.4"
    assert resolved.source == "server"
    assert resolved.package_requirement == "openai-codex==0.144.4"


@pytest.mark.asyncio
async def test_explicit_runtime_version_wins_without_server():
    resolved = await resolve_managed_runtime(
        {"runtime": {"name": "codex", "version": "0.144.4"}},
        region="cn-beijing-6",
    )

    assert resolved.version == "0.144.4"
    assert resolved.source == "manifest"


@pytest.mark.asyncio
async def test_unlocked_runtime_fails_when_server_has_no_default():
    with pytest.raises(ManagedRuntimeError, match="默认 Runtime 版本"):
        await resolve_managed_runtime(
            {"runtime": {"name": "codex"}},
            region="cn-beijing-6",
            bootstrap={},
        )


def test_validate_installed_runtime_rejects_version_mismatch(monkeypatch):
    monkeypatch.setattr(
        "ksadk.managed_runtime.installed_runtime_version",
        lambda _name: "0.100.0",
    )
    resolved = extract_bootstrap_runtime(
        {"configs": {"runtime.default_version": "0.144.4"}},
        "codex",
    )

    assert resolved is not None
    with pytest.raises(ManagedRuntimeError, match="0.100.0"):
        validate_installed_runtime(resolved)


@pytest.mark.asyncio
async def test_local_runtime_uses_installed_version_when_offline(monkeypatch):
    async def unavailable(*_args, **_kwargs):
        raise ManagedRuntimeError("offline")

    monkeypatch.setattr("ksadk.managed_runtime.resolve_managed_runtime", unavailable)
    monkeypatch.setattr(
        "ksadk.managed_runtime.installed_runtime_version",
        lambda _name: "0.144.4",
    )
    monkeypatch.setattr(
        "ksadk.managed_runtime.validate_runtime_binary",
        lambda _resolved: "codex-cli 0.144.4",
    )

    resolved = await resolve_local_managed_runtime(
        {"runtime": {"name": "codex"}},
        region="cn-beijing-6",
    )

    assert resolved.version == "0.144.4"
    assert resolved.source == "installed-unlocked"


def test_validate_codex_binary_checks_effective_version(monkeypatch):
    class Completed:
        stdout = "codex-cli 0.144.4\n"
        stderr = ""

    fake_module = types.ModuleType("codex_cli_bin")
    fake_module.bundled_codex_path = lambda: "/native/platform/codex"
    monkeypatch.setitem(
        sys.modules,
        "codex_cli_bin",
        fake_module,
    )
    monkeypatch.setattr(
        "ksadk.managed_runtime.subprocess.run",
        lambda *_args, **_kwargs: Completed(),
    )

    output = validate_runtime_binary(
        ResolvedRuntime(name="codex", version="0.144.4", source="manifest")
    )

    assert output == "codex-cli 0.144.4"
