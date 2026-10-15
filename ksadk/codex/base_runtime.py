"""Project native Codex capabilities into the BaseRuntime contract."""

from __future__ import annotations

from typing import Any

from ksadk.codex.client import CodexClient
from ksadk.runtime.adapter import BaseRuntime


class _CodexAsBaseRuntime(BaseRuntime):
    """把 CodexClient 包装为 BaseRuntime(原生能力面)。"""

    def __init__(self, client: CodexClient) -> None:
        self._client = client
        self.runtime_type = "codex"

    def native_capabilities(self) -> dict[str, Any]:
        return {
            "Framework": "codex",
            "cancel": "thread",
            "pause": "interrupt_then_resume_thread",
            "resume": "thread_id",
            "live_interaction": True,
        }
