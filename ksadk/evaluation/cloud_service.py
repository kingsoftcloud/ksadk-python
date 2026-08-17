"""Cloud EvalSet publication orchestration shared by CLI and Studio."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from pydantic import Field

from .cloud_binding import CloudBinding, CloudBindingStore
from .cloud_converter import (
    CloudDatasetSnapshot,
    evalset_from_dataset_snapshot,
    evalset_to_dataset_snapshot,
)
from .contracts import CloudDatasetRef, DataPolicy, EvalSetVersion, EvaluationModel


class CloudEvalSetPreviewError(ValueError):
    """Raised before an EvalSet body is allowed to leave the local process."""


class CloudEvalSetPublishResult(EvaluationModel):
    """Provider acknowledgement for one immutable Dataset snapshot."""

    dataset_id: str = Field(min_length=1)
    dataset_version: int = Field(ge=1)
    project_id: str | None = None
    schema_hash: str = Field(min_length=64, max_length=64)
    content_digest: str = Field(min_length=64, max_length=64)
    row_count: int = Field(ge=0)


class CloudEvalSetPullResult(EvaluationModel):
    """One validated immutable cloud snapshot and its normalized local form."""

    snapshot: CloudDatasetSnapshot
    evalset: EvalSetVersion
    cloud_dataset: CloudDatasetRef


class CloudEvalSetCatalogItem(EvaluationModel):
    """One immutable Dataset version exposed by the cloud catalog."""

    dataset_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    project_id: str | None = None
    version: int = Field(ge=1)
    schema_hash: str = Field(min_length=64, max_length=64)
    content_digest: str = Field(min_length=64, max_length=64)
    row_count: int = Field(ge=0)


class CloudDatasetClient(Protocol):
    """Minimal provider contract required before a snapshot can be published."""

    async def publish_snapshot(
        self,
        snapshot: CloudDatasetSnapshot,
        *,
        dataset_id: str | None,
        base_version: int | None,
        idempotency_key: str,
    ) -> CloudEvalSetPublishResult: ...

    async def read_snapshot(
        self,
        dataset_id: str,
        version: int,
        *,
        project_id: str | None = None,
    ) -> CloudDatasetSnapshot: ...

    async def list_datasets(
        self,
        *,
        project_id: str | None = None,
    ) -> list[CloudEvalSetCatalogItem]: ...


class CloudEvalSetService:
    """Validate, publish, and bind EvalSets without changing runtime execution."""

    def __init__(
        self,
        workspace_root: str | Path,
        client: CloudDatasetClient,
        *,
        provider: str = "agent-eval/evalsmith",
    ):
        self.bindings = CloudBindingStore(workspace_root)
        self.client = client
        self.provider = provider

    def preview(self, evalset: EvalSetVersion, *, data_policy: DataPolicy) -> CloudDatasetSnapshot:
        """Return the exact outgoing snapshot or fail before any network request."""

        if data_policy is DataPolicy.LOCAL_ONLY:
            raise CloudEvalSetPreviewError("DataPolicy=local_only 禁止上传 EvalSet 正文")
        if data_policy is DataPolicy.METADATA_ONLY:
            raise CloudEvalSetPreviewError(
                "DataPolicy=metadata_only 不能发布包含 Case 正文的 EvalSet"
            )
        if data_policy is not DataPolicy.FULL_TRACE:
            raise CloudEvalSetPreviewError("当前端云评测集发布仅支持显式 DataPolicy=full_trace")
        return evalset_to_dataset_snapshot(evalset)

    async def publish(
        self,
        evalset: EvalSetVersion,
        *,
        evalset_path: str,
        data_policy: DataPolicy,
        dataset_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CloudEvalSetPublishResult:
        """Publish a full snapshot to an existing Dataset and advance its binding."""

        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("Idempotency-Key 不能为空")
        snapshot = self.preview(evalset, data_policy=data_policy)
        existing = self.bindings.read(evalset_path)
        requested_dataset_id = str(dataset_id or "").strip() or None
        target_dataset_id = requested_dataset_id or (existing.dataset_id if existing else None)
        if target_dataset_id is None:
            raise ValueError("datasetId is required for the first publish of an EvalSet")
        resolved_idempotency_key = idempotency_key or self._idempotency_key(
            target_dataset_id,
            snapshot.content_digest,
        )
        result = await self.client.publish_snapshot(
            snapshot,
            dataset_id=target_dataset_id,
            base_version=None,
            idempotency_key=resolved_idempotency_key,
        )
        if result.dataset_id != target_dataset_id:
            raise CloudEvalSetPreviewError("云端返回的 datasetId 与目标 Dataset 不一致")
        if result.content_digest != snapshot.content_digest:
            raise CloudEvalSetPreviewError("云端返回的 contentDigest 与本地预检结果不一致")
        if result.schema_hash != snapshot.schema_hash:
            raise CloudEvalSetPreviewError("云端返回的 schemaHash 与本地预检结果不一致")
        if result.row_count != len(snapshot.rows):
            raise CloudEvalSetPreviewError("云端返回的 RowCount 与本地预检结果不一致")
        self.bindings.write(
            CloudBinding(
                evalset_path=evalset_path,
                content_digest=snapshot.content_digest,
                provider=self.provider,
                project_id=result.project_id,
                dataset_id=result.dataset_id,
                dataset_version=result.dataset_version,
                schema_hash=result.schema_hash,
            )
        )
        return result

    @staticmethod
    def _idempotency_key(dataset_id: str, content_digest: str) -> str:
        identity = f"{dataset_id}:{content_digest}".encode("utf-8")
        return f"ksadk-evalset-{hashlib.sha256(identity).hexdigest()}"

    async def pull(
        self,
        *,
        dataset_id: str,
        version: int,
        project_id: str | None = None,
    ) -> CloudEvalSetPullResult:
        """Read and validate one immutable Dataset version before execution."""

        if not dataset_id.strip() or version < 1:
            raise ValueError("datasetId 和 version 必须有效")
        snapshot = await self.client.read_snapshot(
            dataset_id,
            version,
            project_id=project_id,
        )
        evalset = evalset_from_dataset_snapshot(snapshot)
        if evalset.content_digest != snapshot.content_digest:
            raise CloudEvalSetPreviewError("云端 snapshot 的 contentDigest 校验失败")
        reference = CloudDatasetRef(
            provider=self.provider,
            project_id=project_id,
            dataset_id=dataset_id,
            version=version,
            schema_hash=snapshot.schema_hash,
            content_digest=snapshot.content_digest,
            row_count=len(snapshot.rows),
        )
        return CloudEvalSetPullResult(
            snapshot=snapshot,
            evalset=evalset,
            cloud_dataset=reference,
        )

    async def catalog(self, *, project_id: str | None = None) -> list[CloudEvalSetCatalogItem]:
        return await self.client.list_datasets(project_id=project_id)
