from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def _audit():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("public_history_audit", scripts / "public_history_audit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(scripts))


def _git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True)


def test_history_audit_detects_removed_private_files_without_returning_values(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.test")
    secret_path = tmp_path / "docs/internal/deploy.txt"
    secret_path.parent.mkdir(parents=True)
    private_value = ".kube/config-" + "deadbeef"
    secret_path.write_text(private_value)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "fixture initial")
    secret_path.unlink()
    _git(tmp_path, "add", "-u")
    _git(tmp_path, "commit", "-qm", "fixture remove")
    findings = _audit().audit_history(tmp_path)
    assert {item["rule"] for item in findings} >= {"internal-docs", "personal-cluster-config"}
    assert private_value not in str(findings)


def test_history_audit_accepts_supported_public_provider_hosts(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.test")
    (tmp_path / "client.py").write_text('ENDPOINT = "https://aicp.api.ksyun.com"\n')
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "fixture public")
    assert _audit().audit_history(tmp_path) == []


def test_history_audit_checks_every_path_for_identical_blobs(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.test")
    (tmp_path / "client.py").write_text("shared content\n")
    private = tmp_path / "docs/internal/deploy.py"
    private.parent.mkdir(parents=True)
    private.write_text("shared content\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "fixture same blob")
    assert any(item["path"] == "docs/internal/deploy.py" for item in _audit().audit_history(tmp_path))


def test_history_audit_checks_pure_rename_into_private_directory(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.test")
    public = tmp_path / "client.py"
    public.write_text("shared content\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "fixture public")
    private = tmp_path / "docs/internal/deploy.py"
    private.parent.mkdir(parents=True)
    public.rename(private)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "fixture rename")
    assert any(item["path"] == "docs/internal/deploy.py" for item in _audit().audit_history(tmp_path))
