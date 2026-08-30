"""Platform-level long-term memory service helpers."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, cast

from ksadk.common.aicp_env import resolve_aicp_connection
from ksadk.memory.adk.backends.base_ltm_backend import (
    CAP_SESSION_STATUS,
    CAP_STRUCTURED_SEARCH,
    BaseLongTermMemoryBackend,
)
from ksadk.memory.ltm_backend_factory import get_long_term_memory_backend_cls
from ksadk.memory.models import (
    LongTermMemoryRecord,
    MemoryExtractionStatus,
    MemoryMutationResult,
    UnsupportedMemoryOperation,
)

logger = logging.getLogger(__name__)


def format_memory_entries(entries: list[str]) -> str:
    if not entries:
        return ""  # empty → "" not "未找到"（§10.8）

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


def _normalize_content(text: str) -> str:
    """归一化正文，用于写后确认的可见性匹配（去空白，小写）。"""
    return "".join(str(text or "").split()).lower()


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
        backend_index = (
            getattr(backend, "index", "") if isinstance(backend, BaseLongTermMemoryBackend) else ""
        )
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
                    or os.environ.get("KSYUN_ACCESS_KEY")
                    or os.environ.get("KSYUN_ACCESS_KEY_ID", "")
                ),
                "secret_key": (
                    os.environ.get("KSADK_LTM_SECRET_KEY")
                    or os.environ.get("KSYUN_SECRET_KEY")
                    or os.environ.get("KSYUN_SECRET_ACCESS_KEY", "")
                ),
                "session_token": (
                    os.environ.get("KSADK_LTM_SESSION_TOKEN")
                    or os.environ.get("KSYUN_SESSION_TOKEN", "")
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
        return cast(BaseLongTermMemoryBackend, backend_cls(**config))

    def search_entries(self, *, user_id: str, query: str, top_k: int | None = None) -> list[str]:
        return self._backend.search_memory(
            user_id=user_id,
            query=query,
            top_k=top_k if top_k is not None else self.top_k,
        )

    def search_records(
        self, *, user_id: str, query: str, top_k: int | None = None
    ) -> list[LongTermMemoryRecord]:
        """结构化检索（§7.5）：优先走 backend.search_records，返回带服务端 ID 的记录。

        旧 backend 不支持时抛出 UnsupportedMemoryOperation，
        由上层降级为只读 search/add，不得伪造 ID。
        """
        return self._backend.search_records(
            user_id=user_id,
            query=query,
            top_k=top_k if top_k is not None else self.top_k,
        )

    def update_memory(
        self,
        *,
        user_id: str,
        memory_id: str,
        content: str,
    ) -> MemoryMutationResult:
        """按 ID 原地更新记忆（§7.4）；不支持的 backend 抛稳定异常。"""
        return self._backend.update_memory(
            user_id=user_id,
            memory_id=memory_id,
            content=content,
        )

    def delete_memory(self, *, user_id: str, memory_id: str) -> MemoryMutationResult:
        """按 ID 软删除记忆（§7.4）；重复删除归一为 already_absent。"""
        return self._backend.delete_memory(user_id=user_id, memory_id=memory_id)

    def list_memory_records(
        self,
        *,
        user_id: str,
        query: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> list[LongTermMemoryRecord]:
        """ListMemories 透传：写后确认可见性 / 取当前 MemoryId。"""
        return self._backend.list_memory_records(
            user_id=user_id, query=query, page=page, page_size=page_size
        )

    def get_extraction_status(
        self,
        *,
        user_id: str,
        session_id: str,
        confirm_searchable: bool = False,
        expected_content: str = "",
    ) -> MemoryExtractionStatus:
        """写后确认（§7.7）：查询 Session 后台提取状态。

        Args:
            confirm_searchable: True 且 State=100 时，进一步通过
                ListMemories 确认目标记录可见（目标达成后 searchable=True）。
            expected_content: 用于在 ListMemories 中定位目标记录的归一化正文。
        """
        if CAP_SESSION_STATUS not in self._backend.capabilities():
            raise UnsupportedMemoryOperation(
                f"{type(self._backend).__name__} does not support session status"
            )
        status = self._backend.get_extraction_status(user_id=user_id, session_id=session_id)
        if (
            confirm_searchable
            and status.status == "extracted"
            and CAP_STRUCTURED_SEARCH in self._backend.capabilities()
            and expected_content.strip()
        ):
            # 用 expected_content 作为 Query 语义过滤，避免大记忆库时目标不在首页。
            records = self.list_memory_records(
                user_id=user_id, query=expected_content, page_size=50
            )
            if self._find_matching_record(records, expected_content) is not None:
                status = MemoryExtractionStatus(
                    session_id=status.session_id,
                    state=status.state,
                    status=status.status,
                    searchable=True,
                    message=status.message,
                )
        return status

    @staticmethod
    def _find_matching_record(
        records: list[LongTermMemoryRecord], expected_content: str
    ) -> LongTermMemoryRecord | None:
        """按归一化正文等值/包含匹配目标记录（方案 N3：可见性判定规则）。

        expected_content 为空时不作匹配（返回 None），避免误判任意记录为可见。
        """
        normalized = _normalize_content(expected_content)
        if not normalized:
            return None
        for record in records:
            if normalized == _normalize_content(record.content):
                return record
        for record in records:
            if normalized in _normalize_content(record.content):
                return record
        return None

    def capabilities(self) -> set[str]:
        """透传 backend 能力声明（§7.3）。"""
        return set(self._backend.capabilities())

    @property
    def last_error(self) -> str:
        """最近一次后端失败原因（成功调用前置空，失败时填充）。

        后端（SDK/HTTP）失败时可能吞掉异常返空列表而非抛错，这里把该信号暴露给
        ``build_context``，以区分"后端吞错返空"与"真无记忆"。
        """
        return str(getattr(self._backend, "last_error", "") or "")

    def search_text(self, *, user_id: str, query: str, top_k: int | None = None) -> str:
        """检索长期记忆并格式化为文本（方案 §10.8：错误不得混入正文）。

        Provider 异常时返回空字符串而非错误文本——错误文本会被当作记忆正文注入模型上下文，
        污染回答。需要区分"真无记忆"与"后端失败"的调用方应改用 ``build_context()``
        或检查 ``self.last_error``。
        """
        try:
            return format_memory_entries(
                self.search_entries(user_id=user_id, query=query, top_k=top_k)
            )
        except Exception as exc:
            logger.error("load_memory failed: %s", exc)
            return ""

    def save_event_strings(
        self,
        *,
        user_id: str,
        event_strings: list[str],
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        flush: bool | None = None,
    ) -> bool:
        """保存事件字符串。

        显式参数优先于 metadata（方案 §7.6）：
            flush: None 表示未指定，回退到 metadata["flush"]；
                True/False 显式覆盖 metadata。
            session_id: None 回退到 metadata["session_id"]。
        兼容期旧调用（仅 metadata）行为不变。
        """
        base_metadata = dict(metadata or {})
        effective_flush = flush if flush is not None else base_metadata.get("flush")
        if session_id is not None:
            base_metadata["session_id"] = session_id
        effective_session_id = base_metadata.get("session_id")
        # 只有显式为 True 时才携带 flush；False/未携带时不传，
        # 让服务端走默认累积策略（§5.2）。
        if effective_flush is True:
            base_metadata["flush"] = True
        else:
            base_metadata.pop("flush", None)
        return bool(
            self._backend.save_memory(
                user_id=user_id,
                event_strings=event_strings,
                metadata=base_metadata,
                session_id=effective_session_id,
                flush=effective_flush,
            )
        )

    def save_text(
        self,
        *,
        user_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        flush: bool | None = None,
    ) -> bool:
        """保存自包含事实文本。

        显式 flush=True 用于显式、内容自包含的持久事实保存
        （如 ksadk_memory_add）；普通 sync_turn 不传 flush（§3.1/§8.7）。
        """
        payload = {
            "role": "user",
            "parts": [{"text": content}],
            "metadata": metadata or {},
        }
        return self.save_event_strings(
            user_id=user_id,
            event_strings=[json.dumps(payload, ensure_ascii=False)],
            metadata=metadata,
            session_id=session_id,
            flush=flush,
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
        try:
            entries = self.search_entries(user_id=user_id, query=normalized, top_k=top_k)
        except Exception as exc:
            logger.error("load_memory failed: %s", exc)
            return {"query": normalized, "formatted_text": "", "error": str(exc)}
        backend_error = self.last_error
        if not entries and backend_error:
            return {"query": normalized, "formatted_text": "", "error": backend_error}
        return {
            "query": normalized,
            "formatted_text": format_memory_entries(entries),
        }
