"""缺口 5：ArtifactStore——版本化存储与读取。"""

from __future__ import annotations

import pytest

from ksadk.harness.artifact_store import (
    ArtifactBudgetExceeded,
    ArtifactStore,
    BudgetedArtifactStore,
)


def test_save_versions_and_roundtrip(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.save(run_id="r1", name="report.md", content=b"v1 body", mime="text/markdown")
    second = store.save(run_id="r1", name="report.md", content=b"v2 body", mime="text/markdown")
    assert first.version == 1 and second.version == 2
    assert first.content_hash != second.content_hash

    records = store.list("r1")
    assert [r.version for r in records] == [1, 2]
    assert store.read(second) == b"v2 body"
    assert store.read_latest("r1", "report.md") == b"v2 body"
    # 旧版本仍可读（历史保留）。
    assert store.read(first) == b"v1 body"


def test_runs_are_isolated(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    store.save(run_id="r1", name="x.txt", content=b"a")
    store.save(run_id="r2", name="x.txt", content=b"b")
    assert store.read_latest("r1", "x.txt") == b"a"
    assert store.read_latest("r2", "x.txt") == b"b"
    assert store.list("r3") == []


def test_list_uses_same_run_id_canonicalization_as_save(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    store.save(run_id="parent:sub:child", name="x.txt", content=b"a")
    assert len(store.list("parent:sub:child")) == 1


def test_unsafe_names_are_sanitized(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    record = store.save(run_id="r1", name="../../etc/passwd", content=b"nope")
    assert "/" not in record.name
    assert record.uri.startswith("artifact://r1/")
    assert store.read(record) == b"nope"


def test_missing_content_raises(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(FileNotFoundError):
        store._read_uri("artifact://r1/none@v1")


def test_budgeted_store_rejects_before_file_or_index_side_effect(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    guarded = BudgetedArtifactStore(store, run_id="r1", max_artifacts=0)

    with pytest.raises(ArtifactBudgetExceeded, match="before write"):
        guarded.save(run_id="r1", name="blocked.txt", content=b"must-not-exist")

    assert store.list("r1") == []
    assert not (tmp_path / "artifacts" / "r1").exists()
