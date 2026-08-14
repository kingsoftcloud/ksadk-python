"""Cloud EvalSet publication orchestration shared by CLI and Studio."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import Field

from .cloud_binding import CloudBinding, CloudBindingStore
from .cloud_converter import CloudDatasetSnapshot, evalset_to_dataset_snapshot
from .contracts import DataPolicy, EvalSetVersion, EvaluationModel


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
        idempotency_key: str,
    ) -> CloudEvalSetPublishResult:
        """Publish one immutable snapshot and atomically advance its local binding."""

        if not idempotency_key.strip():
            raise ValueError("Idempotency-Key 不能为空")
        snapshot = self.preview(evalset, data_policy=data_policy)
        existing = self.bindings.read(evalset_path)
        result = await self.client.publish_snapshot(
            snapshot,
            dataset_id=existing.dataset_id if existing else None,
            base_version=existing.dataset_version if existing else None,
            idempotency_key=idempotency_key,
        )
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
