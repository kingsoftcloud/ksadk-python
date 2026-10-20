from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


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


def _metadata_repo(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Open Source Contributor")
    _git(root, "config", "user.email", "contributor@example.test")


def test_history_audit_checks_message_only_root_commit_without_returning_values(tmp_path, capsys):
    _metadata_repo(tmp_path)
    endpoint = "fixture." + "internal.api.ksyun.com"
    _git(tmp_path, "commit", "--allow-empty", "-qm", "fix: remove " + endpoint)
    commit = _git(tmp_path, "rev-parse", "HEAD").strip()
    findings = _audit().audit_history(tmp_path)
    assert {"commit": commit, "field": "message", "rule": "internal-service-endpoint"} in findings
    assert endpoint not in str(findings)
    assert endpoint not in capsys.readouterr().out


def test_history_audit_checks_ancestor_messages_when_head_is_clean(tmp_path):
    _metadata_repo(tmp_path)
    _git(tmp_path, "commit", "--allow-empty", "-qm", "fixture root")
    private_path = ".kube/config-" + "cafe1234"
    _git(tmp_path, "commit", "--allow-empty", "-qm", "fix configuration " + private_path)
    ancestor = _git(tmp_path, "rev-parse", "HEAD").strip()
    _git(tmp_path, "commit", "--allow-empty", "-qm", "fixture clean follow-up")
    findings = _audit().audit_history(tmp_path)
    assert {"commit": ancestor, "field": "message", "rule": "personal-cluster-config"} in findings
    assert private_path not in str(findings)


@pytest.mark.parametrize("kind", ["scm", "provider", "credential"])
def test_history_audit_treats_commit_messages_as_public_documentation(tmp_path, kind):
    _metadata_repo(tmp_path)
    cases = {
        "scm": ("E" + "zone", "public-doc-internal-scm"),
        "provider": ("iam." + "inner.api.ksyun.com", "public-doc-internal-endpoint"),
        "credential": (
            "OPENAI_API_KEY=" + "00000000-0000-0000-0000-" + "000000000001",
            "uuid-secret-assignment",
        ),
    }
    value, expected_rule = cases[kind]
    _git(tmp_path, "commit", "--allow-empty", "-qm", "fixture message\n\n" + value)
    findings = _audit().audit_history(tmp_path)
    assert any(item.get("field") == "message" and item["rule"] == expected_rule for item in findings)
    assert value not in str(findings)


@pytest.mark.parametrize("field", ["author", "committer"])
@pytest.mark.parametrize("component", ["name", "email"])
def test_history_audit_checks_identity_endpoints_without_returning_attribution(
    tmp_path, monkeypatch, field, component,
):
    _metadata_repo(tmp_path)
    endpoint = "fixture." + "internal.api.ksyun.com"
    value = endpoint if component == "name" else "contributor@" + endpoint
    monkeypatch.setenv(f"GIT_{field.upper()}_{component.upper()}", value)
    _git(tmp_path, "commit", "--allow-empty", "-qm", "fixture public message")
    commit = _git(tmp_path, "rev-parse", "HEAD").strip()
    findings = _audit().audit_history(tmp_path)
    assert {"commit": commit, "field": field, "rule": "internal-service-endpoint"} in findings
    assert endpoint not in str(findings)
    assert value not in str(findings)


def test_history_audit_preserves_public_messages_and_normal_open_source_attribution(
    tmp_path, monkeypatch,
):
    _metadata_repo(tmp_path)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "开源 Contributor")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "123+contributor@users.noreply.github.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Release Bot")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "release@example.org")
    _git(
        tmp_path, "commit", "--allow-empty", "-qm",
        "fix: use https://aicp.api.ksyun.com\n\nCo-authored-by: "
        + "E" + "zone Contributor <peer@example.org>",
    )
    assert _audit().audit_history(tmp_path) == []


def test_history_audit_does_not_classify_author_handles_as_internal_prose(tmp_path, monkeypatch):
    _metadata_repo(tmp_path)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "E" + "zone Contributor")
    _git(tmp_path, "commit", "--allow-empty", "-qm", "fixture public message")
    assert _audit().audit_history(tmp_path) == []


def test_commit_message_audit_honors_declared_encoding():
    endpoint = "fixture." + "internal.api.ksyun.com"
    message = ("fixture " + endpoint).encode("utf-16")
    headers = (
        b"author Contributor <contributor@example.test> 0 +0000\n"
        b"committer Contributor <contributor@example.test> 0 +0000\n"
        b"encoding utf-16\n\n"
    )
    findings = _audit()._commit_metadata_findings("a" * 40, headers + message)
    assert findings == [{"commit": "a" * 40, "field": "message", "rule": "internal-service-endpoint"}]
    assert endpoint not in str(findings)



def test_history_audit_still_checks_concrete_endpoints_in_authorship_trailers(tmp_path):
    _metadata_repo(tmp_path)
    endpoint = "fixture." + "internal.api.ksyun.com"
    _git(
        tmp_path, "commit", "--allow-empty", "-qm",
        "fixture public message\n\nCo-authored-by: Contributor <peer@" + endpoint + ">",
    )
    findings = _audit().audit_history(tmp_path)
    assert any(item.get("field") == "message-trailer"
               and item["rule"] == "internal-service-endpoint" for item in findings)
    assert endpoint not in str(findings)
