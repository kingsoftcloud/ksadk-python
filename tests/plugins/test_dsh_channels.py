from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ksadk.plugins import dsh_channels
from ksadk.studio.entry import studio_entry_response


class _FakeBridge:
    instances: list["_FakeBridge"] = []
    fail_install = False
    initial_current = None

    def __init__(self, *, dsh_home, **_kwargs):
        self.profile_root = Path(dsh_home) / "profiles" / "web"
        self.current = self.__class__.initial_current
        self.install_calls = 0
        self.enabled = False
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @contextmanager
    def _profile_transaction(self, **_kwargs):
        yield

    def _snapshot(self):
        return None

    def _rollback(self, _snapshot, error):
        raise error

    def list_plugins(self):
        if self.current is None:
            return ()
        return (self.current,)

    def install_plugin(self, *_args, **_kwargs):
        self.install_calls += 1
        if self.fail_install:
            raise RuntimeError("install failed")
        self.current = SimpleNamespace(
            name=dsh_channels.CHANNEL_PLUGIN_PACKAGE,
            version=dsh_channels.CHANNEL_PLUGIN_VERSION,
            enabled=False,
        )
        return self.current

    def set_enabled(self, _name, *, enabled):
        self.enabled = enabled
        self.current.enabled = enabled

    def project_profile(self):
        return SimpleNamespace(config_digest="digest")


def _request(path: str = "/") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
            "scheme": "http",
        }
    )


def test_default_install_records_marker_and_does_not_reinstall_after_removal(tmp_path, monkeypatch):
    _FakeBridge.instances.clear()
    monkeypatch.setattr(dsh_channels, "DshProfilePluginBridge", _FakeBridge)
    home = tmp_path / "dsh"
    result = dsh_channels.configure_channel_profile(
        workspace=tmp_path, dsh_home=home, dsh_command=("dsh",)
    )
    assert result["enabled"] is True
    marker = home / "profiles" / "web" / ".ksadk-channel-default.json"
    assert json.loads(marker.read_text())["defaultDisabled"] is False
    _FakeBridge.instances.clear()
    result = dsh_channels.configure_channel_profile(
        workspace=tmp_path, dsh_home=home, dsh_command=("dsh",)
    )
    assert result["skipped"] == "explicitly_removed"
    assert _FakeBridge.instances[-1].install_calls == 0


def test_disabled_existing_bundle_is_persisted_and_failure_has_no_marker(tmp_path, monkeypatch):
    _FakeBridge.instances.clear()
    monkeypatch.setattr(dsh_channels, "DshProfilePluginBridge", _FakeBridge)
    home = tmp_path / "dsh"
    _FakeBridge.initial_current = SimpleNamespace(
        name=dsh_channels.CHANNEL_PLUGIN_PACKAGE,
        version=dsh_channels.CHANNEL_PLUGIN_VERSION,
        enabled=False,
    )
    result = dsh_channels.configure_channel_profile(
        workspace=tmp_path, dsh_home=home, dsh_command=("dsh",)
    )
    assert result["skipped"] == "explicitly_disabled"
    marker = home / "profiles" / "web" / ".ksadk-channel-default.json"
    assert json.loads(marker.read_text())["defaultDisabled"] is True
    _FakeBridge.initial_current = None

    _FakeBridge.instances.clear()
    _FakeBridge.fail_install = True
    with pytest.raises(RuntimeError, match="install failed"):
        dsh_channels.configure_channel_profile(
            workspace=tmp_path / "failed", dsh_home=tmp_path / "failed-dsh", dsh_command=("dsh",)
        )
    assert not (
        tmp_path / "failed-dsh" / "profiles" / "web" / ".ksadk-channel-default.json"
    ).exists()
    _FakeBridge.fail_install = False


@pytest.mark.asyncio
async def test_entry_probe_is_bounded_for_slow_core_start(tmp_path):
    class SlowCapabilities:
        async def has_enabled_profile_plugins(self):
            await asyncio.sleep(5)
            return True

    (tmp_path / "index.html").write_text("<html></html>")
    response = await studio_entry_response(
        SimpleNamespace(dsh_capabilities=SlowCapabilities()), _request(), tmp_path
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_lazy_entry_does_not_probe_core(tmp_path, monkeypatch):
    class FailingCapabilities:
        async def has_enabled_profile_plugins(self):
            raise AssertionError("lazy entry must not probe Core")

    (tmp_path / "index.html").write_text("<html></html>")
    monkeypatch.setenv("KSADK_STUDIO_LAZY_START", "1")
    started = time.monotonic()
    response = await studio_entry_response(
        SimpleNamespace(dsh_capabilities=FailingCapabilities()), _request(), tmp_path
    )
    assert response.status_code == 200
    assert time.monotonic() - started < 0.5
