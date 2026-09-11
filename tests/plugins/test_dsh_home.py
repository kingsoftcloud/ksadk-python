from __future__ import annotations

import json

import pytest

from ksadk.plugins.dsh_home import (
    DshHomeVersionError,
    default_studio_dsh_home,
    dsh_home_diagnostic,
    prepare_studio_dsh_home,
    studio_dsh_home,
)
from ksadk.plugins.dsh_toolchain import DSH_VERSION


def test_new_default_never_modifies_legacy_home_or_session_logs(tmp_path, monkeypatch):
    monkeypatch.delenv("KSADK_DSH_HOME", raising=False)
    legacy = tmp_path / ".agentkit/dsh-home"
    legacy.mkdir(parents=True)
    log = legacy / "session.jsonl"
    log.write_bytes(b'{"version":2,"event":"old-session"}\n')
    expected = log.read_bytes()
    home = studio_dsh_home(tmp_path)
    assert home == tmp_path / ".agentkit/dsh-homes" / DSH_VERSION
    assert not home.exists()
    prepare_studio_dsh_home(home)
    prepare_studio_dsh_home(home)
    assert dsh_home_diagnostic(home, workspace=tmp_path) == {
        "expectedVersion": DSH_VERSION, "status": "compatible", "writeAllowed": True,
        "reason": "receipt_matches", "legacyHomePreserved": True,
    }
    assert log.read_bytes() == expected
    assert list(legacy.iterdir()) == [log]
    assert not (home / "session.jsonl").exists()


def test_existing_explicit_unreceipted_home_is_inspectable_but_not_writable(tmp_path, monkeypatch):
    home = tmp_path / "explicit"
    home.mkdir()
    manifest = home / "package.json"
    manifest.write_text('{"name":"old-profile"}')
    monkeypatch.setenv("KSADK_DSH_HOME", str(home))
    assert studio_dsh_home(tmp_path) == home
    diagnostic = dsh_home_diagnostic(home)
    assert diagnostic["reason"] == "receipt_missing"
    assert diagnostic["writeAllowed"] is False
    assert "原版本工具链" in diagnostic["recovery"]
    with pytest.raises(DshHomeVersionError):
        prepare_studio_dsh_home(home)
    assert list(home.iterdir()) == [manifest]
    assert manifest.read_text() == '{"name":"old-profile"}'


def test_fresh_explicit_home_receipt_must_remain_exact(tmp_path, monkeypatch):
    home = tmp_path / "custom"
    monkeypatch.setenv("KSADK_DSH_HOME", str(home))
    prepare_studio_dsh_home(studio_dsh_home(tmp_path))
    receipt = home / ".ksadk-dsh-home.json"
    data = json.loads(receipt.read_text())
    assert data["dshVersion"] == DSH_VERSION
    data["dshVersion"] = "0.1.2-rc.1"
    receipt.write_text(json.dumps(data))
    with pytest.raises(DshHomeVersionError) as error:
        prepare_studio_dsh_home(home)
    assert error.value.diagnostic["reason"] == "receipt_mismatch"
    assert error.value.diagnostic["observedVersion"] == "0.1.2-rc.1"
    assert json.loads(receipt.read_text()) == data
    assert default_studio_dsh_home(tmp_path) != home
