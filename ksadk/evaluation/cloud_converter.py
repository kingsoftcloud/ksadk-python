"""Lossless conversion between local EvalSets and cloud Dataset snapshots."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import Field, model_validator

from .contracts import EvalCase, EvalSetVersion, EvaluationModel


class EvalSetCloudConversionError(ValueError):
    """Raised when a cloud Dataset cannot be represented as an EvalSet."""


class CloudDatasetColumn(EvaluationModel):
    """A fixed cloud Dataset field used by the EvalSet converter."""

    name: str = Field(min_length=1)
    value_type: str = Field(min_length=1)
    required: bool = False
    description: str | None = None
    text_schema: dict[str, Any] | None = None


class CloudDatasetRow(EvaluationModel):
    """One cloud Dataset row with its server-independent field values."""

    values: dict[str, Any]


class CloudDatasetSnapshot(EvaluationModel):
    """A portable fixed Dataset version before it is sent to a cloud provider."""

    name: str = Field(min_length=1, max_length=256)
    description: str | None = None
    content_digest: str = Field(min_length=64, max_length=64)
    source_format: str = Field(min_length=1)
    evalset_metadata: dict[str, Any] = Field(default_factory=dict)
    columns: list[CloudDatasetColumn] = Field(min_length=1)
    rows: list[CloudDatasetRow] = Field(min_length=1)
    schema_hash: str = ""

    @model_validator(mode="after")
    def validate_schema_hash(self) -> "CloudDatasetSnapshot":
        expected = _schema_hash(self.columns)
        if self.schema_hash and self.schema_hash != expected:
            raise ValueError("schemaHash 与列定义不一致")
        self.schema_hash = expected
        return self


_COLUMNS = (
    CloudDatasetColumn(
        name="case_id", value_type="String", required=True, description="KsADK Case ID"
    ),
    CloudDatasetColumn(
        name="turns",
        value_type="Array",
        required=True,
        description="Ordered Eval turns",
        text_schema={
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    ),
    CloudDatasetColumn(
        name="assertions",
        value_type="Array",
        required=True,
        description="Eval assertions",
        text_schema={
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    ),
    CloudDatasetColumn(
        name="case_metadata",
        value_type="Object",
        description="Case metadata",
        text_schema={"type": "object", "additionalProperties": True},
    ),
    CloudDatasetColumn(
        name="source_format", value_type="String", required=True, description="Source format"
    ),
    CloudDatasetColumn(
        name="ksadk_content_digest",
        value_type="String",
        required=True,
        description="Normalized EvalSet content digest",
    ),
)
_COLUMN_NAMES = tuple(column.name for column in _COLUMNS)


def evalset_to_dataset_snapshot(evalset: EvalSetVersion) -> CloudDatasetSnapshot:
    """Convert a normalized EvalSet into the single supported cloud Dataset schema."""

    return CloudDatasetSnapshot(
        name=evalset.name,
        content_digest=evalset.content_digest,
        source_format=evalset.source_format,
        evalset_metadata=evalset.metadata,
        columns=list(_COLUMNS),
        rows=[
            CloudDatasetRow(
                values={
                    "case_id": case.id,
                    "turns": [turn.model_dump(mode="json", by_alias=True) for turn in case.turns],
                    "assertions": [
                        assertion.model_dump(mode="json", by_alias=True)
                        for assertion in case.assertions
                    ],
                    "case_metadata": case.metadata,
                    "source_format": evalset.source_format,
                    "ksadk_content_digest": evalset.content_digest,
                }
            )
            for case in evalset.cases
        ],
    )


def evalset_from_dataset_snapshot(snapshot: CloudDatasetSnapshot) -> EvalSetVersion:
    """Restore an EvalSet from one fixed Dataset snapshot without losing supported data."""

    _validate_columns(snapshot.columns)
    cases: list[EvalCase] = []
    source_formats: set[str] = set()
    row_digests: set[str] = set()
    for index, row in enumerate(snapshot.rows, start=1):
        values = row.values
        _validate_row(values, index)
        source_formats.add(str(values["source_format"]))
        row_digests.add(str(values["ksadk_content_digest"]))
        try:
            cases.append(
                EvalCase.model_validate(
                    {
                        "id": values["case_id"],
                        "turns": values["turns"],
                        "assertions": values["assertions"],
                        "metadata": values["case_metadata"],
                    }
                )
            )
        except ValueError as exc:
            raise EvalSetCloudConversionError(f"第 {index} 行不能转换为 EvalCase") from exc

    if len(source_formats) != 1 or snapshot.source_format not in source_formats:
        raise EvalSetCloudConversionError("Rows 中的 source_format 必须与 Dataset snapshot 一致")
    if row_digests != {snapshot.content_digest}:
        raise EvalSetCloudConversionError(
            "Rows 中的 ksadk_content_digest 必须与 Dataset snapshot 一致"
        )
    try:
        return EvalSetVersion(
            name=snapshot.name,
            cases=cases,
            metadata=snapshot.evalset_metadata,
            source_format=snapshot.source_format,
            content_digest=snapshot.content_digest,
        )
    except ValueError as exc:
        raise EvalSetCloudConversionError("Dataset snapshot 内容无效") from exc


def _validate_columns(columns: list[CloudDatasetColumn]) -> None:
    if [column.name for column in columns] != list(_COLUMN_NAMES):
        raise EvalSetCloudConversionError("Dataset 列定义必须匹配 KsADK EvalSet 固定 schema")
    for actual, expected in zip(columns, _COLUMNS):
        if actual.value_type != expected.value_type or actual.required != expected.required:
            raise EvalSetCloudConversionError(f"Dataset 列 {actual.name} 的类型或必填属性不匹配")


def _validate_row(values: dict[str, Any], index: int) -> None:
    missing = [name for name in _COLUMN_NAMES if name not in values]
    if missing:
        raise EvalSetCloudConversionError(f"第 {index} 行缺少字段: {', '.join(missing)}")
    unknown = sorted(set(values) - set(_COLUMN_NAMES))
    if unknown:
        raise EvalSetCloudConversionError(f"第 {index} 行包含未知字段: {', '.join(unknown)}")


def _schema_hash(columns: list[CloudDatasetColumn]) -> str:
    payload = [
        column.model_dump(mode="json", by_alias=True, exclude_none=True) for column in columns
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
