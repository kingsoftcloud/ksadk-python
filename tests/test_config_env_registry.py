from __future__ import annotations

import subprocess
from pathlib import Path

from ksadk.configs.env_registry import ENV_VAR_REGISTRY


def _source_ksadk_env_names() -> set[str]:
    result = subprocess.run(
        ["rg", "-o", "KSADK_[A-Z0-9_]+", "ksadk"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        line.rsplit(":", 1)[-1].strip()
        for line in result.stdout.splitlines()
        if line.strip()
    }


def test_env_registry_has_unique_sorted_names():
    names = [item.name for item in ENV_VAR_REGISTRY]

    assert len(names) == len(set(names))
    assert names == sorted(names)


def test_env_registry_covers_ksadk_env_vars_in_source():
    registry_names = {item.name for item in ENV_VAR_REGISTRY}

    assert _source_ksadk_env_names() <= registry_names


def test_env_registry_docs_cover_registered_names():
    doc_text = Path("docs/ksadk环境变量参考.md").read_text(encoding="utf-8")

    for item in ENV_VAR_REGISTRY:
        assert item.name in doc_text
