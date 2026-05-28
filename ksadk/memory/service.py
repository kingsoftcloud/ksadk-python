"""Platform-level long-term memory service helpers."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from ksadk.common.aicp_env import resolve_aicp_connection
from ksadk.memory.adk.backends.base_ltm_backend import BaseLongTermMemoryBackend
from ksadk.memory.ltm_backend_factory import get_long_term_memory_backend_cls

logger = logging.getLogger(__name__)


def format_memory_entries(entries: list[str]) -> str:
    if not entries:
        return "未找到相关长期记忆。"

    formatted_entries: list[str] = []
    for index, entry in enumerate(entries, 1):
        text = entry
        try:
            payload = json.loads(entry)
        except (json.JSONDecodeError, TypeError):
            payload = None

        if isinstance(payload, dict):
            parts = payload.get("parts") or []
            if parts and isinstance(parts[0], dict):
                text = str(parts[0].get("text") or text)
        formatted_entries.append(f"[{index}] {text}")

    return "\n\n".join(formatted_entries)


class LongTermMemoryService:
    def __init__(
        self,
        *,
        backend: str | BaseLongTermMemoryBackend = "local",
        backend_config: dict[str, Any] | None = None,
        top_k: int = 5,
        index: str = "",
        app_name: str = "",
    ):
        self.backend = backend
        self.backend_config = dict(backend_config or {})
        self.top_k = top_k
        self.app_name = app_name
        backend_index = getattr(backend, "index", "") if isinstance(backend, BaseLongTermMemoryBackend) else ""
        self.index = index or backend_index or app_name or "default_app"
        self._backend = self._resolve_backend()

    @classmethod
    def from_env(
        cls,
        *,
        app_name: str = "",
        backend: str | None = None,
    ) -> "LongTermMemoryService":
        resolved_backend = backend or os.environ.get("KSADK_LTM_BACKEND", "local")
        top_k = int(os.environ.get("KSADK_LTM_TOP_K", "5"))
        index = os.environ.get("KSADK_LTM_INDEX", "")
        app_name = os.environ.get("KSADK_LTM_APP_NAME", "") or app_name

        backend_config: dict[str, Any] = {}
        if resolved_backend == "http":
            backend_config = {
                "base_url": os.environ.get("KSADK_LTM_HTTP_URL", ""),
                "token": os.environ.get("KSADK_LTM_HTTP_TOKEN", ""),
            }
        elif resolved_backend == "sdk":
            connection = resolve_aicp_connection("KSADK_LTM")
            backend_config = {
                "access_key": (
                    os.environ.get("KSADK_LTM_ACCESS_KEY")
                    or os.environ.get("KSYUN_ACCESS_KEY", "")
                ),
                "secret_key": (
                    os.environ.get("KSADK_LTM_SECRET_KEY")
                    or os.environ.get("KSYUN_SECRET_KEY", "")
                ),
                "region": connection["region"],
                "endpoint": connection["endpoint"],
                "scheme": connection["scheme"],
                "namespace": os.environ.get("KSADK_LTM_NAMESPACE", ""),
                "agent_id": os.environ.get("KSADK_LTM_AGENT_ID", ""),
                "scene_id": os.environ.get("KSADK_LTM_SCENE_ID", "_sys_general"),
            }

        return cls(
            backend=resolved_backend,
            backend_config=backend_config,
            top_k=top_k,
            index=index,
            app_name=app_name,
        )

    @staticmethod
    def is_configured() -> bool:
        return bool(str(os.environ.get("KSADK_LTM_BACKEND", "")).strip())

    def _resolve_backend(self) -> BaseLongTermMemoryBackend:
        if isinstance(self.backend, BaseLongTermMemoryBackend):
            return self.backend

        backend_cls = get_long_term_memory_backend_cls(str(self.backend))
        config = dict(self.backend_config)
        config.setdefault("index", self.index)
        return backend_cls(**config)

    def search_entries(self, *, user_id: str, query: str, top_k: int | None = None) -> list[str]:
        return self._backend.search_memory(
            user_id=user_id,
            query=query,
            top_k=top_k if top_k is not None else self.top_k,
        )

    def search_text(self, *, user_id: str, query: str, top_k: int | None = None) -> str:
        try:
            return format_memory_entries(
                self.search_entries(user_id=user_id, query=query, top_k=top_k)
            )
        except Exception as exc:
            logger.error("load_memory failed: %s", exc)
            return f"长期记忆检索失败: {exc}"

    def save_event_strings(
        self,
        *,
        user_id: str,
        event_strings: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        return bool(
            self._backend.save_memory(
                user_id=user_id,
                event_strings=event_strings,
                metadata=metadata or {},
            )
        )

    def save_text(self, *, user_id: str, content: str, metadata: dict[str, Any] | None = None) -> bool:
        payload = {
            "role": "user",
            "parts": [{"text": content}],
            "metadata": metadata or {},
        }
        return self.save_event_strings(
            user_id=user_id,
            event_strings=[json.dumps(payload, ensure_ascii=False)],
            metadata=metadata,
        )

    def build_context(
        self,
        *,
        user_id: str,
        query: str,
        top_k: int | None = None,
    ) -> dict[str, str] | None:
        normalized = str(query or "").strip()
        if not normalized:
            return None
        if not self.is_configured():
            return None
        return {
            "query": normalized,
            "formatted_text": self.search_text(user_id=user_id, query=normalized, top_k=top_k),
        }
