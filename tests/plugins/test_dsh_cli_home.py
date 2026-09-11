from types import SimpleNamespace

from click.testing import CliRunner

from ksadk.cli import cmd_plugin
from ksadk.plugins.dsh_home import dsh_home_diagnostic, studio_dsh_home


def test_cli_and_studio_share_versioned_home_and_fence_first_catalog_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KSADK_DSH_HOME", raising=False)
    monkeypatch.setattr(cmd_plugin, "_dsh_command", lambda: ("fake-dsh",))
    homes = []

    class Bridge:
        def __init__(self, **kwargs):
            homes.append(kwargs["dsh_home"])
            self.host = SimpleNamespace(host_id="dsh", version="0.1.5-rc.1")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def list_plugins(self):
            return []

    monkeypatch.setattr("ksadk.plugins.bridges.dsh.DshProfilePluginBridge", Bridge)
    result = CliRunner().invoke(cmd_plugin.dsh_plugins, ["list"])
    assert result.exit_code == 0, result.output
    assert homes == [studio_dsh_home(tmp_path)]
    assert dsh_home_diagnostic(homes[0])["status"] == "compatible"


def test_cli_legacy_catalog_stays_readable_but_mutation_preserves_old_home(tmp_path, monkeypatch):
    home = tmp_path / "legacy"
    home.mkdir()
    log = home / "session.jsonl"
    log.write_bytes(b'{"version":2}\n')
    monkeypatch.setenv("KSADK_DSH_HOME", str(home))
    monkeypatch.setattr(cmd_plugin, "_dsh_command", lambda: ("fake-dsh",))
    calls = []

    class Bridge:
        def __init__(self, **kwargs):
            self.host = SimpleNamespace(host_id="dsh", version="0.1.5-rc.1")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def list_plugins(self):
            calls.append("read")
            return []

        def set_enabled(self, *args, **kwargs):
            raise AssertionError("legacy mutation must not reach the bridge")

    monkeypatch.setattr("ksadk.plugins.bridges.dsh.DshProfilePluginBridge", Bridge)
    runner = CliRunner()
    assert runner.invoke(cmd_plugin.dsh_plugins, ["list"]).exit_code == 0
    refused = runner.invoke(cmd_plugin.dsh_plugins, ["disable", "example"])
    assert refused.exit_code != 0
    assert "DSH 目录版本未经验证" in refused.output
    assert "原版本工具链" in refused.output
    assert calls == ["read"]
    assert log.read_bytes() == b'{"version":2}\n'
    assert list(home.iterdir()) == [log]
