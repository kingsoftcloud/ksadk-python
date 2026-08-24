"""HTTP adapter for the EvalSmith-backed agent-eval dataset API."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Protocol

import httpx

from ksadk.common.kop_client import KOPClient, KOPError

from .cloud_converter import CloudDatasetColumn, CloudDatasetRow, CloudDatasetSnapshot
from .cloud_service import CloudEvalSetCatalogItem, CloudEvalSetPublishResult
from .service_env import resolve_agent_eval_direct_url, resolve_agent_eval_kop_connection


class AgentEvalCloudClientError(RuntimeError):
    """The agent-eval cloud dataset API rejected or could not process a request."""


class _KOPActionClient(Protocol):
    def post_action(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]: ...


class AgentEvalCloudDatasetClient:
    """Publish immutable KsADK EvalSet snapshots through agent-eval and EvalSmith."""

    _PUBLISH_PATH = "/agentengine/eval/api/v1/PublishEvaluationSetSnapshot"
    _READ_PATH = "/agentengine/eval/api/v1/DescribeEvaluationSet"
    _LIST_PATH = "/agentengine/eval/api/v1/ListEvaluationSet"
    _READ_PAGE_SIZE = 200
    _LIST_PAGE_SIZE = 100

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_token: str | None = None,
        account_id: str | None = None,
        timeout_seconds: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
        kop_client: _KOPActionClient | None = None,
    ) -> None:
        explicit_base_url = str(base_url).strip().rstrip("/") if base_url is not None else None
        if explicit_base_url is None:
            explicit_base_url = resolve_agent_eval_direct_url()
        self._base_url = explicit_base_url or ""
        self._api_token = str(
            api_token if api_token is not None else os.environ.get("AGENT_EVAL_API_TOKEN", "")
        ).strip()
        self._account_id = str(
            account_id
            or os.environ.get("AGENT_EVAL_ACCOUNT_ID")
            or os.environ.get("KSYUN_ACCOUNT_ID")
            or ""
        ).strip()
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client
        self._kop_client: _KOPActionClient | None = None
        if not self._base_url:
            if http_client is not None:
                raise ValueError("http_client requires AGENT_EVAL_BASE_URL direct mode")
            connection = resolve_agent_eval_kop_connection()
            self._kop_client = kop_client or KOPClient(
                base_url=connection["base_url"],
                access_key=os.environ.get("KSYUN_ACCESS_KEY"),
                secret_key=os.environ.get("KSYUN_SECRET_KEY"),
                account_id=os.environ.get("KSYUN_ACCOUNT_ID"),
                region=connection["region"],
                timeout=timeout_seconds,
            )
        elif kop_client is not None:
            raise ValueError("kop_client cannot be used with AGENT_EVAL_BASE_URL direct mode")

    @property
    def uses_kop(self) -> bool:
        return self._kop_client is not None

    async def publish_snapshot(
        self,
        snapshot: CloudDatasetSnapshot,
        *,
        dataset_id: str | None,
        base_version: int | None,
        idempotency_key: str,
    ) -> CloudEvalSetPublishResult:
        columns: list[dict[str, Any]] = []
        for column in snapshot.columns:
            item: dict[str, Any] = {
                "Key": column.name,
                "Name": column.name,
                "ValueType": column.value_type,
                "Required": column.required,
                "Description": column.description,
            }
            if column.text_schema is not None:
                item["TextSchema"] = column.text_schema
            columns.append(item)

        payload: dict[str, Any] = {
            "Name": snapshot.name,
            "Description": snapshot.description,
            "Columns": columns,
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
        data = await self._request(
            "PublishEvaluationSetSnapshot",
            self._PUBLISH_PATH,
            payload,
        )
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
        del project_id  # DescribeEvaluationSet resolves the project from the account context.
        page = 1
        first_page: dict[str, Any] | None = None
        raw_items: list[dict[str, Any]] = []
        while True:
            data = await self._request(
                "DescribeEvaluationSet",
                self._READ_PATH,
                {
                    "DatasetId": dataset_id,
                    "DatasetVersion": version,
                    "Page": page,
                    "PageSize": self._READ_PAGE_SIZE,
                },
            )
            if first_page is None:
                first_page = data
            page_items = data.get("Items")
            if not isinstance(page_items, list) or not all(
                isinstance(item, dict) for item in page_items
            ):
                raise AgentEvalCloudClientError("agent-eval snapshot read returned invalid items")
            raw_items.extend(page_items)
            if not data.get("HasMore"):
                break
            page += 1

        assert first_page is not None
        try:
            current_version = int(first_page["CurrentVersion"])
            if current_version != version:
                raise ValueError("version mismatch")
            raw_rows = [item["Row"] for item in raw_items]
            rows = [CloudDatasetRow(values=dict(row)) for row in raw_rows if isinstance(row, dict)]
            if len(rows) != len(raw_rows):
                raise ValueError("invalid row")
            expected_row_count = first_page.get("RowCount")
            if expected_row_count is not None and int(expected_row_count) != len(rows):
                raise ValueError("row count mismatch")
            columns = [self._column_from_remote(column) for column in first_page["Columns"]]
            content_digests = {
                str(row.values.get("ksadk_content_digest") or "").strip() for row in rows
            }
            content_digests.discard("")
            if len(content_digests) != 1:
                raise ValueError("content digest mismatch")
            source_formats = {str(row.values.get("source_format") or "").strip() for row in rows}
            source_formats.discard("")
            if len(source_formats) != 1:
                raise ValueError("source format mismatch")
            return CloudDatasetSnapshot(
                name=first_page["Name"],
                description=first_page.get("Description"),
                content_digest=content_digests.pop(),
                source_format=source_formats.pop(),
                evalset_metadata={},
                columns=columns,
                rows=rows,
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
        payload: dict[str, Any] = {
            "DatasetType": "Manual",
            "Page": 1,
            "PageSize": self._LIST_PAGE_SIZE,
        }
        if project_id:
            payload["ProjectId"] = project_id
        raw_items: list[dict[str, Any]] = []
        while True:
            data = await self._request("ListEvaluationSet", self._LIST_PATH, payload)
            page_items = data.get("Items", data.get("items", data.get("EvaluationSets", [])))
            if not isinstance(page_items, list):
                raise AgentEvalCloudClientError("agent-eval dataset list returned invalid items")
            raw_items.extend(item for item in page_items if isinstance(item, dict))

            page = data.get("Page", payload["Page"])
            page_size = data.get("PageSize", payload["PageSize"])
            total = data.get("Total")
            try:
                has_next_page = bool(data.get("HasMore")) or (
                    total is not None and int(page) * int(page_size) < int(total)
                )
            except (TypeError, ValueError):
                has_next_page = False
            if not has_next_page:
                break
            payload["Page"] = int(payload["Page"]) + 1
        items: list[CloudEvalSetCatalogItem] = []
        for item in raw_items:
            dataset_id = str(item.get("DatasetId", item.get("datasetId", ""))).strip()
            version = item.get(
                "Version",
                item.get("version", item.get("CurrentVersion", item.get("currentVersion"))),
            )
            try:
                version = int(version)
            except (TypeError, ValueError):
                continue
            if not dataset_id or version < 1:
                continue

            schema_hash = item.get("SchemaHash", item.get("schemaHash"))
            content_digest = item.get("ContentDigest", item.get("contentDigest"))
            row_count = item.get("RowCount", item.get("rowCount"))
            name = item.get("Name", item.get("name"))
            item_project_id = item.get("ProjectId", item.get("projectId", project_id))
            # Standard ListEvaluationSet omits KsADK's digest fields. Recover them
            # from the immutable version and omit unrelated product datasets.
            if not (
                isinstance(schema_hash, str)
                and len(schema_hash) == 64
                and isinstance(content_digest, str)
                and len(content_digest) == 64
            ):
                try:
                    snapshot = await self.read_snapshot(
                        dataset_id,
                        version,
                        project_id=item_project_id,
                    )
                except AgentEvalCloudClientError:
                    continue
                schema_hash = snapshot.schema_hash
                content_digest = snapshot.content_digest
                row_count = len(snapshot.rows)
                name = name or snapshot.name
            try:
                items.append(
                    CloudEvalSetCatalogItem(
                        dataset_id=dataset_id,
                        name=name,
                        project_id=item_project_id,
                        version=version,
                        schema_hash=schema_hash,
                        content_digest=content_digest,
                        row_count=row_count,
                    )
                )
            except (TypeError, ValueError):
                continue
        return items

    async def _request(
        self,
        action: str,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self._kop_client is not None:
            try:
                data = await asyncio.to_thread(self._kop_client.post_action, action, payload)
            except KOPError as exc:
                raise AgentEvalCloudClientError(
                    f"agent-eval KOP action {action} failed: {exc.message}"
                ) from exc
            except Exception as exc:
                raise AgentEvalCloudClientError(
                    f"agent-eval KOP action {action} failed"
                ) from exc
            if not isinstance(data, dict):
                raise AgentEvalCloudClientError(
                    f"agent-eval KOP action {action} returned invalid data"
                )
            return data

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
            raise AgentEvalCloudClientError(
                "agent-eval snapshot request returned invalid JSON"
            ) from exc
        if not isinstance(envelope, dict) or envelope.get("Code") != 0:
            raise AgentEvalCloudClientError("agent-eval snapshot request was rejected")
        data = envelope.get("Data")
        if not isinstance(data, dict):
            raise AgentEvalCloudClientError("agent-eval request returned no result")
        return data

    @staticmethod
    def _parse_text_schema(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        raise ValueError("invalid text schema")

    @staticmethod
    def _normalize_value_type(value: Any) -> str:
        normalized = str(value or "").strip()
        if normalized.lower().startswith("array<"):
            return "Array"
        return normalized

    @classmethod
    def _column_from_remote(cls, column: dict[str, Any]) -> CloudDatasetColumn:
        name = column.get("name") or column.get("Key") or column.get("Name")
        text_schema = cls._parse_text_schema(
            column.get("textSchema")
            or column.get("TextSchema")
            or column.get("textSchemaRaw")
            or column.get("TextSchemaRaw")
        )
        value_type = cls._normalize_value_type(column.get("valueType") or column.get("ValueType"))
        if text_schema == {"type": "string", "title": name}:
            text_schema = None
        return CloudDatasetColumn(
            name=name,
            value_type=value_type,
            required=column.get("required", column.get("Required", False)),
            description=column.get("description") or column.get("Description"),
            text_schema=text_schema,
        )
