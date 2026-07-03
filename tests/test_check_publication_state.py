from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_publication_state.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_publication_state", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_main(monkeypatch, module, *, phase: str, version_exists: dict[tuple[str, str], bool]):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_publication_state.py",
            "--phase",
            phase,
            "--version",
            "0.6.4",
        ],
    )
    monkeypatch.setattr(module, "_expect_http_ok", lambda name, url: None)
    monkeypatch.setattr(
        module,
        "_github_release_tags",
        lambda url: {"v0.6.1", "v0.6.2", "v0.6.3"},
    )
    monkeypatch.setattr(
        module,
        "_pypi_project_version",
        lambda project: {"ksadk": "0.6.3", "agentengine-sdk-python": "0.6.2"}[project],
    )
    monkeypatch.setattr(
        module,
        "_pypi_version_exists",
        lambda project, version: version_exists.get((project, version), False),
    )

    return module.main()


def test_pre_publish_fails_when_alias_package_version_already_exists(monkeypatch):
    module = _load_module()

    with pytest.raises(RuntimeError, match="agentengine-sdk-python==0.6.4"):
        _run_main(
            monkeypatch,
            module,
            phase="pre-publish",
            version_exists={
                ("ksadk", "0.6.4"): False,
                ("agentengine-sdk-python", "0.6.4"): True,
            },
        )


def test_post_publish_fails_when_alias_package_version_is_missing(monkeypatch):
    module = _load_module()

    with pytest.raises(RuntimeError, match="agentengine-sdk-python==0.6.4"):
        _run_main(
            monkeypatch,
            module,
            phase="post-publish",
            version_exists={
                ("ksadk", "0.6.4"): True,
                ("agentengine-sdk-python", "0.6.4"): False,
            },
        )


def test_publication_state_fails_when_historical_github_release_is_missing(monkeypatch):
    module = _load_module()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_publication_state.py",
            "--phase",
            "pre-publish",
            "--version",
            "0.6.4",
        ],
    )
    monkeypatch.setattr(module, "_expect_http_ok", lambda name, url: None)
    monkeypatch.setattr(module, "_github_release_tags", lambda url: {"v0.6.1", "v0.6.3"})

    with pytest.raises(RuntimeError, match="v0.6.2"):
        module.main()


def test_github_api_request_uses_available_token(monkeypatch):
    module = _load_module()
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def read(self):
            return b"[]"

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("GH_TOKEN", "gh-test-token")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    status, body = module._open("https://api.github.com/repos/kingsoftcloud/ksadk-python/releases")

    assert status == 200
    assert body == b"[]"
    assert captured["headers"]["Authorization"] == "Bearer gh-test-token"
    assert captured["timeout"] == 20


def test_github_token_is_not_sent_to_url_containing_github_api_as_query(monkeypatch):
    module = _load_module()
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def read(self):
            return b"ok"

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        return FakeResponse()

    monkeypatch.setenv("GH_TOKEN", "gh-test-token")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    status, body = module._open("https://example.com/status?next=api.github.com")

    assert status == 200
    assert body == b"ok"
    assert "Authorization" not in captured["headers"]


def test_github_release_tags_falls_back_to_gh_cli_on_rate_limit(monkeypatch):
    module = _load_module()

    def fake_open(_url):
        raise module.urllib.error.HTTPError(
            url="https://api.github.com/repos/kingsoftcloud/ksadk-python/releases",
            code=403,
            msg="rate limit exceeded",
            hdrs=None,
            fp=None,
        )

    def fake_run(argv, check, text, stdout, stderr):
        assert argv == [
            "gh",
            "release",
            "list",
            "--repo",
            "kingsoftcloud/ksadk-python",
            "--limit",
            "200",
            "--json",
            "tagName",
        ]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='[{"tagName":"v0.6.5"},{"tagName":"v0.6.4"}]',
            stderr="",
        )

    monkeypatch.setattr(module, "_open", fake_open)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module._github_release_tags(
        "https://api.github.com/repos/kingsoftcloud/ksadk-python/releases?per_page=100"
    ) == {"v0.6.5", "v0.6.4"}


def test_github_release_tags_falls_back_to_gh_cli_on_transient_server_error(monkeypatch):
    module = _load_module()

    def fake_open(_url):
        raise module.urllib.error.HTTPError(
            url="https://api.github.com/repos/kingsoftcloud/ksadk-python/releases",
            code=502,
            msg="Bad Gateway",
            hdrs=None,
            fp=None,
        )

    def fake_run(argv, check, text, stdout, stderr):
        assert argv[:5] == ["gh", "release", "list", "--repo", "kingsoftcloud/ksadk-python"]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='[{"tagName":"v0.6.5"},{"tagName":"v0.6.4"}]',
            stderr="",
        )

    monkeypatch.setattr(module, "_open", fake_open)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module._github_release_tags(
        "https://api.github.com/repos/kingsoftcloud/ksadk-python/releases?per_page=100"
    ) == {"v0.6.5", "v0.6.4"}
