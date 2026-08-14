from __future__ import annotations

from pathlib import Path

import pytest

from ksadk.evaluation import DataPolicy
from ksadk.evaluation.cloud_service import (
    CloudEvalSetPreviewError,
    CloudEvalSetPublishResult,
    CloudEvalSetService,
)
from ksadk.evaluation.contracts import CloudDatasetRef
from ksadk.evaluation.evalset import parse_evalset


class _FakeCloudClient:
    def __init__(self) -> None:
        self.calls = []
        self.result = CloudEvalSetPublishResult(
            dataset_id="dataset_001",
            dataset_version=2,
            project_id="project_001",
            schema_hash="b" * 64,
            content_digest="a" * 64,
            row_count=1,
        )

    async def publish_snapshot(self, snapshot, *, dataset_id, base_version, idempotency_key):
        self.calls.append((snapshot, dataset_id, base_version, idempotency_key))
        return self.result.model_copy(
            update={
                "content_digest": snapshot.content_digest,
                "schema_hash": snapshot.schema_hash,
            }
        )

    async def read_snapshot(self, dataset_id, version, *, project_id=None):
        del dataset_id, version, project_id
        return evalset_to_dataset_snapshot(_evalset())


def _evalset():
    return parse_evalset(
        {
            "schemaVersion": "ksadk.eval/v1",
            "name": "support-regression",
            "cases": [{"id": "case-001", "turns": [{"input": "如何修改密码？"}]}],
        }
    )


from ksadk.evaluation.cloud_converter import evalset_to_dataset_snapshot


@pytest.mark.asyncio
async def test_pull_reads_one_fixed_version_and_returns_traceable_ref(tmp_path: Path):
    service = CloudEvalSetService(tmp_path, _FakeCloudClient())

    result = await service.pull(
        dataset_id="dataset_001",
        version=4,
        project_id="project_001",
    )

    assert result.evalset.content_digest == _evalset().content_digest
    assert result.cloud_dataset == CloudDatasetRef(
        provider="agent-eval/evalsmith",
        project_id="project_001",
        dataset_id="dataset_001",
        version=4,
        schema_hash=result.snapshot.schema_hash,
        content_digest=result.snapshot.content_digest,
        row_count=1,
    )


def test_preview_refuses_local_only_without_contacting_cloud(tmp_path: Path):
    client = _FakeCloudClient()
    service = CloudEvalSetService(tmp_path, client)

    with pytest.raises(CloudEvalSetPreviewError, match="local_only"):
        service.preview(_evalset(), data_policy=DataPolicy.LOCAL_ONLY)

    assert client.calls == []


@pytest.mark.asyncio
async def test_publish_writes_binding_only_after_cloud_publish_succeeds(tmp_path: Path):
    client = _FakeCloudClient()
    service = CloudEvalSetService(tmp_path, client)
    evalset = _evalset()

    result = await service.publish(
        evalset,
        evalset_path="evaluations/support.yaml",
        data_policy=DataPolicy.FULL_TRACE,
        idempotency_key="publish-001",
    )

    assert result.dataset_id == "dataset_001"
    assert client.calls[0][1:] == (None, None, "publish-001")
    binding = service.bindings.read("evaluations/support.yaml")
    assert binding is not None
    assert binding.dataset_version == 2
    assert binding.content_digest == evalset.content_digest


@pytest.mark.asyncio
async def test_failed_publish_preserves_the_existing_binding(tmp_path: Path):
    client = _FakeCloudClient()
    service = CloudEvalSetService(tmp_path, client)
    evalset = _evalset()
    await service.publish(
        evalset,
        evalset_path="evaluations/support.yaml",
        data_policy=DataPolicy.FULL_TRACE,
        idempotency_key="publish-001",
    )
    existing = service.bindings.read("evaluations/support.yaml")

    async def fail(*args, **kwargs):
        raise RuntimeError("network unavailable")

    client.publish_snapshot = fail
    changed = parse_evalset(
        {
            "schemaVersion": "ksadk.eval/v1",
            "name": "support-regression-v2",
            "cases": [{"id": "case-001", "turns": [{"input": "如何重置密码？"}]}],
        }
    )

    with pytest.raises(RuntimeError, match="network unavailable"):
        await service.publish(
            changed,
            evalset_path="evaluations/support.yaml",
            data_policy=DataPolicy.FULL_TRACE,
            idempotency_key="publish-002",
        )

    assert service.bindings.read("evaluations/support.yaml") == existing
