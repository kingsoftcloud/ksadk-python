from __future__ import annotations

from typing import Any

import httpx

from ksadk.sandbox.base import (
    BaseSandbox,
    ExecutionConfig,
    ExecutionResult,
    ExecutionStatus,
    Language,
)


class RemoteCodeSandbox(BaseSandbox):
    def __init__(
        self,
        api_url: str,
        api_key: str = "",
        timeout: float = 60.0,
        default_config: ExecutionConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(default_config=default_config)
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport

    async def execute(
        self,
        code: str,
        language: Language = Language.PYTHON,
        config: ExecutionConfig | None = None,
    ) -> ExecutionResult:
        config = self.resolve_config(config)

        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                response = await client.post(
                    f"{self.api_url}/execute",
                    json={
                        "code": code,
                        "language": language.value,
                        "timeout_seconds": config.timeout_seconds,
                        "max_memory_mb": config.max_memory_mb,
                        "max_output_bytes": config.max_output_bytes,
                        "allow_network": config.allow_network,
                    },
                    headers=self._headers(),
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                stderr=f"Remote execution request failed: {exc}",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                stderr=f"Remote sandbox returned invalid JSON: {exc}",
            )

        return self._parse_result(payload)

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return kwargs

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _parse_result(self, payload: dict[str, Any]) -> ExecutionResult:
        raw_status = payload.get("status", ExecutionStatus.ERROR.value)
        try:
            status = ExecutionStatus(raw_status)
        except ValueError:
            status = ExecutionStatus.ERROR

        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        files_created = payload.get("files_created")
        if not isinstance(files_created, list):
            files_created = []

        return ExecutionResult(
            status=status,
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
            return_value=payload.get("return_value"),
            execution_time_ms=float(payload.get("execution_time_ms", 0.0) or 0.0),
            files_created=[str(item) for item in files_created],
            metadata=metadata,
        )
