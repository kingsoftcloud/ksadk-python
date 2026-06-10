from __future__ import annotations

import importlib.util
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
