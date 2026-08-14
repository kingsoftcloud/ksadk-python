"""HTTP adapter for the EvalSmith-backed agent-eval dataset API."""

from __future__ import annotations

from typing import Any

import httpx

from .cloud_converter import CloudDatasetColumn, CloudDatasetRow, CloudDatasetSnapshot
from .cloud_service import CloudEvalSetCatalogItem, CloudEvalSetPublishResult


class AgentEvalCloudClientError(RuntimeError):
    """The agent-eval cloud dataset API rejected or could not process a request."""


class AgentEvalCloudDatasetClient:
    """Publish immutable KsADK EvalSet snapshots through agent-eval and EvalSmith."""

    _PUBLISH_PATH = "/agentengine/eval/api/v1/PublishEvaluationSetSnapshot"
    _READ_PATH = "/agentengine/eval/api/v1/DescribeEvaluationSetSnapshot"
    _LIST_PATH = "/agentengine/eval/api/v1/ListEvaluationSet"

    def __init__(
        self,
        base_url: str,
        *,
        api_token: str | None = None,
        account_id: str | None = None,
        timeout_seconds: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        normalized_base_url = str(base_url or "").strip().rstrip("/")
        if not normalized_base_url:
            raise ValueError("agent-eval base URL cannot be empty")
        self._base_url = normalized_base_url
        self._api_token = str(api_token or "").strip()
        self._account_id = str(account_id or "").strip()
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client

    async def publish_snapshot(
        self,
        snapshot: CloudDatasetSnapshot,
        *,
        dataset_id: str | None,
        base_version: int | None,
        idempotency_key: str,
    ) -> CloudEvalSetPublishResult:
        headers = {"Content-Type": "application/json"}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        if self._account_id:
            headers["X-Ksc-Account-Id"] = self._account_id
        payload: dict[str, Any] = {
            "Name": snapshot.name,
            "Description": snapshot.description,
            "Columns": [
                {
                    "Key": column.name,
                    "Name": column.name,
                    "ValueType": column.value_type,
                    "Required": column.required,
                    "Description": column.description,
                }
                for column in snapshot.columns
            ],
            "Rows": [
                {"values": row.values, "split": "default", "source": "ksadk"}
                for row in snapshot.rows
            ],
            "ContentDigest": snapshot.content_digest,
            "SchemaHash": snapshot.schema_hash,
            "IdempotencyKey": idempotency_key,
        }
        if dataset_id:
            payload["DatasetId"] = dataset_id
        if base_version is not None:
            payload["BaseVersion"] = base_version

        try:
            if self._http_client is not None:
                response = await self._http_client.post(
                    f"{self._base_url}{self._PUBLISH_PATH}", headers=headers, json=payload
                )
            else:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self._timeout_seconds),
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    response = await client.post(
                        f"{self._base_url}{self._PUBLISH_PATH}", headers=headers, json=payload
                    )
        except httpx.HTTPError as exc:
            raise AgentEvalCloudClientError("agent-eval snapshot publish request failed") from exc

        if response.status_code >= 400:
            raise AgentEvalCloudClientError(
                f"agent-eval snapshot publish failed with HTTP {response.status_code}"
            )
        try:
            envelope = response.json()
        except ValueError as exc:
            raise AgentEvalCloudClientError(
                "agent-eval snapshot publish returned invalid JSON"
            ) from exc
        if not isinstance(envelope, dict) or envelope.get("Code") != 0:
            raise AgentEvalCloudClientError("agent-eval snapshot publish was rejected")
        data = envelope.get("Data")
        if not isinstance(data, dict):
            raise AgentEvalCloudClientError("agent-eval snapshot publish returned no result")
        try:
            return CloudEvalSetPublishResult(
                dataset_id=data["DatasetId"],
                dataset_version=data["DatasetVersion"],
                project_id=data.get("ProjectId"),
                schema_hash=data["SchemaHash"],
                content_digest=data["ContentDigest"],
                row_count=data["RowCount"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentEvalCloudClientError(
                "agent-eval snapshot publish returned an invalid result"
            ) from exc

    async def read_snapshot(
        self,
        dataset_id: str,
        version: int,
        *,
        project_id: str | None = None,
    ) -> CloudDatasetSnapshot:
        if not dataset_id.strip() or version < 1:
            raise ValueError("datasetId and version must be valid")
        payload: dict[str, Any] = {
            "DatasetId": dataset_id,
            "Version": version,
        }
        if project_id:
            payload["ProjectId"] = project_id
        envelope = await self._request(self._READ_PATH, payload)
        data = envelope.get("Data")
        if not isinstance(data, dict):
            raise AgentEvalCloudClientError(
                "agent-eval snapshot read returned no result"
            )
        try:
            raw_rows = data["Rows"]
            rows = [
                CloudDatasetRow(values=dict(row.get("Values", row.get("values", {}))))
                for row in raw_rows
                if isinstance(row, dict)
            ]
            if len(rows) != len(raw_rows):
                raise ValueError("invalid row")
            expected_row_count = data.get("RowCount")
            if expected_row_count is not None and int(expected_row_count) != len(rows):
                raise ValueError("row count mismatch")
            columns = [
                CloudDatasetColumn(
                    name=column.get("name") or column.get("Key") or column.get("Name"),
                    value_type=column.get("valueType") or column.get("ValueType"),
                    required=column.get("required", column.get("Required", False)),
                    description=column.get("description") or column.get("Description"),
                )
                for column in data["Columns"]
            ]
            source_format = data.get("SourceFormat") or data.get("sourceFormat")
            if not source_format and rows:
                source_format = rows[0].values.get("source_format", "native")
            return CloudDatasetSnapshot(
                name=data["Name"],
                description=data.get("Description"),
                content_digest=data["ContentDigest"],
                source_format=source_format or "native",
                evalset_metadata=data.get("Metadata", data.get("metadata", {})),
                columns=columns,
                rows=rows,
                schema_hash=data["SchemaHash"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentEvalCloudClientError(
                "agent-eval snapshot read returned an invalid result"
            ) from exc

    async def list_datasets(
        self,
        *,
        project_id: str | None = None,
    ) -> list[CloudEvalSetCatalogItem]:
        payload: dict[str, Any] = {}
        if project_id:
            payload["ProjectId"] = project_id
        envelope = await self._request(self._LIST_PATH, payload)
        data = envelope.get("Data")
        if not isinstance(data, dict):
            raise AgentEvalCloudClientError("agent-eval dataset list returned no result")
        raw_items = data.get("Items", data.get("items", data.get("EvaluationSets", [])))
        if not isinstance(raw_items, list):
            raise AgentEvalCloudClientError("agent-eval dataset list returned invalid items")
        try:
            return [
                CloudEvalSetCatalogItem(
                    dataset_id=item.get("DatasetId", item.get("datasetId")),
                    name=item.get("Name", item.get("name")),
                    project_id=item.get("ProjectId", item.get("projectId")),
                    version=item.get("Version", item.get("version")),
                    schema_hash=item.get("SchemaHash", item.get("schemaHash")),
                    content_digest=item.get("ContentDigest", item.get("contentDigest")),
                    row_count=item.get("RowCount", item.get("rowCount", 0)),
                )
                for item in raw_items
                if isinstance(item, dict)
            ]
        except (TypeError, ValueError) as exc:
            raise AgentEvalCloudClientError("agent-eval dataset list returned invalid items") from exc

    async def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        if self._account_id:
            headers["X-Ksc-Account-Id"] = self._account_id
        try:
            if self._http_client is not None:
                response = await self._http_client.post(
                    f"{self._base_url}{path}", headers=headers, json=payload
                )
            else:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self._timeout_seconds),
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    response = await client.post(
                        f"{self._base_url}{path}", headers=headers, json=payload
                    )
        except httpx.HTTPError as exc:
            raise AgentEvalCloudClientError("agent-eval snapshot request failed") from exc
        if response.status_code >= 400:
            raise AgentEvalCloudClientError(
                f"agent-eval snapshot request failed with HTTP {response.status_code}"
            )
        try:
            envelope = response.json()
        except ValueError as exc:
            raise AgentEvalCloudClientError("agent-eval snapshot request returned invalid JSON") from exc
        if not isinstance(envelope, dict) or envelope.get("Code") != 0:
            raise AgentEvalCloudClientError("agent-eval snapshot request was rejected")
        return envelope
