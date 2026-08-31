"""Shared launcher for credential-free tests that exercise the real Codex App Server."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from ksadk.codex.client import AsyncCodexClient


class RealCodexFactory:
    """Launch a real App Server against a deterministic local Responses endpoint."""

    def __init__(self, *, responses_url: str) -> None:
        self._responses_url = responses_url
        self.processes: list[Any] = []

    def __call__(self, config: Any = None) -> AsyncCodexClient:
        assert config is not None
        environment = dict(getattr(config, "env", None) or {})
        codex_home = Path(environment["CODEX_HOME"])
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "config.toml").write_text(
            f'''model_provider = "ksadk_provider_stub"
approval_policy = "never"
sandbox_mode = "read-only"

[model_providers.ksadk_provider_stub]
name = "KsADK provider deterministic E2E"
base_url = "{self._responses_url}"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
requires_openai_auth = false
''',
            encoding="utf-8",
        )
        environment.update(
            {
                "CODEX_APP_SERVER_DISABLE_MANAGED_CONFIG": "1",
                "RUST_LOG": "warn",
            }
        )
        client = AsyncCodexClient(dataclasses.replace(config, env=environment))
        transport = client._codex._client._sync
        original_close = transport.close

        def recording_close() -> None:
            process = transport._proc
            try:
                original_close()
            finally:
                if process is not None:
                    self.processes.append(process)

        transport.close = recording_close
        return client
