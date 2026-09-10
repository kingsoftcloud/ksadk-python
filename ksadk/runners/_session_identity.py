"""Framework-native session bindings for verified platform identities."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from typing import Any

from ksadk.runtime_context import get_current_invocation_context
from ksadk.sessions import resolve_session_service
from ksadk.sessions.continuity import ConversationSessionCore
from ksadk.sessions.invocation_identity import (
    identity_native_user_id,
    identity_scope_ref,
    identity_session_is_legacy,
)


class ADKSessionIdentityMixin:
    """Bind canonical sessions to ADK's native ``user_id`` and session id."""

    def _invocation_session_owner(self) -> tuple[str, str]:
        context = get_current_invocation_context()
        if context is None or context.identity.is_empty:
            return "ksadk_user", ""
        if not context.identity.is_complete:
            raise ValueError("Incomplete trusted business identity context")
        return (
            str(context.user_id or identity_native_user_id(context.identity)),
            identity_scope_ref(context.identity),
        )

    @staticmethod
    def _invocation_uses_legacy_session(native_user_id: str) -> bool:
        context = get_current_invocation_context()
        if context is None or context.identity.is_empty or not context.identity.is_complete:
            return False
        return identity_session_is_legacy(context.identity, native_user_id)

    @staticmethod
    def _session_cache_key(external_session_id: str, owner_scope_ref: str) -> str:
        return (
            f"{owner_scope_ref}:{external_session_id}" if owner_scope_ref else external_session_id
        )

    async def _get_adk_session(self, *, user_id: str, session_id: str) -> Any | None:
        if self._session_service is None or not session_id:
            return None
        try:
            return await self._session_service.get_session(
                app_name=self._agent.name,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception:
            return None

    async def _create_adk_session(
        self,
        *,
        user_id: str,
        session_id: str = "",
        preserve_requested_id: bool = False,
    ) -> Any:
        if self._short_term_memory:
            session = await self._short_term_memory.create_session(
                app_name=self._agent.name,
                user_id=user_id,
                session_id=session_id,
            )
        else:
            if self._session_service is None:
                raise RuntimeError("ADK session service is not initialized")
            create_kwargs: dict[str, Any] = {
                "app_name": self._agent.name,
                "user_id": user_id,
            }
            if session_id and preserve_requested_id:
                create_kwargs["session_id"] = session_id
            try:
                session = await self._session_service.create_session(**create_kwargs)
            except TypeError:
                # Older ADK in-memory services did not accept session_id.
                create_kwargs.pop("session_id", None)
                session = await self._session_service.create_session(**create_kwargs)
        if session is None:
            raise RuntimeError("Failed to create ADK session")
        return session

    async def _ensure_session(self, external_session_id: str | None = None) -> str:
        native_user_id, owner_scope_ref = self._invocation_session_owner()
        if external_session_id:
            cache_key = self._session_cache_key(external_session_id, owner_scope_ref)
            if cache_key in self._session_map:
                return self._session_map[cache_key]

            core = ConversationSessionCore(resolve_session_service())
            binding = await core.get_binding_by_session_id(external_session_id, "adk")
            bound_scope_ref = str(binding.get("owner_scope_ref") or "")
            if bound_scope_ref and bound_scope_ref != owner_scope_ref:
                raise ValueError("ADK session belongs to a different business identity")

            internal_session_id = str(binding.get("internal_session_id") or "")
            bound_user_id = str(binding.get("native_user_id") or "")
            if binding:
                native_user_id = bound_user_id or "ksadk_user"
            elif owner_scope_ref and self._invocation_uses_legacy_session(native_user_id):
                # Canonical preprocessing has already proved that this exact
                # platform session is an authorized legacy row.
                legacy_session = await self._get_adk_session(
                    user_id="ksadk_user",
                    session_id=external_session_id,
                )
                if legacy_session is not None:
                    native_user_id = "ksadk_user"
                    internal_session_id = str(legacy_session.id)

            session = None
            if internal_session_id:
                session = await self._get_adk_session(
                    user_id=native_user_id,
                    session_id=internal_session_id,
                )
            if session is None:
                session = await self._create_adk_session(
                    user_id=native_user_id,
                    session_id=internal_session_id or external_session_id,
                    preserve_requested_id=bool(owner_scope_ref or internal_session_id),
                )
            internal_session_id = str(session.id)
            self._session_map[cache_key] = internal_session_id
            self._session_user_map[cache_key] = native_user_id
            await core.set_binding_by_session_id(
                external_session_id,
                "adk",
                {
                    "external_session_id": str(external_session_id),
                    "internal_session_id": internal_session_id,
                    "native_user_id": native_user_id,
                    "owner_scope_ref": owner_scope_ref,
                },
            )
            return internal_session_id

        # Identity-aware requests normally receive a canonical external id
        # before reaching a runner. Keep this fallback isolated as well.
        if owner_scope_ref:
            cache_key = self._session_cache_key("__default__", owner_scope_ref)
            if cache_key not in self._session_map:
                session = await self._create_adk_session(user_id=native_user_id)
                self._session_map[cache_key] = str(session.id)
                self._session_user_map[cache_key] = native_user_id
            return self._session_map[cache_key]

        if self._default_session_id is None:
            session = await self._create_adk_session(user_id=native_user_id)
            self._default_session_id = session.id
        return str(self._default_session_id)

    def _native_user_for_session(self, external_session_id: str | None) -> str:
        native_user_id, owner_scope_ref = self._invocation_session_owner()
        if not external_session_id:
            return native_user_id
        cache_key = self._session_cache_key(str(external_session_id), owner_scope_ref)
        return self._session_user_map.get(cache_key, native_user_id)


class LangGraphSessionIdentityMixin:
    """Bind canonical sessions to LangGraph thread/checkpoint addresses."""

    def _invocation_identity_scope_ref(self) -> str:
        context = get_current_invocation_context()
        if context is None or context.identity.is_empty:
            return ""
        if not context.identity.is_complete:
            raise ValueError("Incomplete trusted business identity context")
        return identity_scope_ref(context.identity)

    @staticmethod
    def _invocation_uses_legacy_session() -> bool:
        context = get_current_invocation_context()
        if context is None or context.identity.is_empty or not context.identity.is_complete:
            return False
        return identity_session_is_legacy(context.identity, context.user_id)

    @staticmethod
    def _identity_thread_id(session_id: str, owner_scope_ref: str) -> str:
        digest = hashlib.sha256(f"{owner_scope_ref}\0{session_id}".encode()).hexdigest()
        return f"lgt_{digest}"

    async def _thread_has_checkpoint(self, thread_id: str) -> bool:
        get_state = getattr(self._agent, "aget_state", None) or getattr(
            self._agent, "get_state", None
        )
        if not callable(get_state):
            return False
        try:
            state = get_state(self._get_config(thread_id))
            if inspect.isawaitable(state):
                state = await state
        except Exception:
            return False
        config = (
            state.get("config") if isinstance(state, Mapping) else getattr(state, "config", None)
        )
        values = (
            state.get("values") if isinstance(state, Mapping) else getattr(state, "values", None)
        )
        metadata = (
            state.get("metadata")
            if isinstance(state, Mapping)
            else getattr(state, "metadata", None)
        )
        created_at = (
            state.get("created_at")
            if isinstance(state, Mapping)
            else getattr(state, "created_at", None)
        )
        if values or metadata or created_at:
            return True
        configurable = config.get("configurable") if isinstance(config, Mapping) else None
        return bool(
            isinstance(configurable, Mapping)
            and str(configurable.get("checkpoint_id") or "").strip()
        )

    async def _get_session_config(self, session_id: str) -> dict[str, Any]:
        owner_scope_ref = self._invocation_identity_scope_ref()
        if not owner_scope_ref:
            return self._get_config(session_id)

        cache_key = f"{owner_scope_ref}:{session_id}"
        cached = self._identity_thread_bindings.get(cache_key)
        if cached:
            return self._get_config(cached)

        async with self._identity_thread_lock:
            cached = self._identity_thread_bindings.get(cache_key)
            if cached:
                return self._get_config(cached)

            core = ConversationSessionCore(resolve_session_service())
            binding = await core.get_binding_by_session_id(session_id, "langgraph")
            bound_scope_ref = str(binding.get("owner_scope_ref") or "")
            if bound_scope_ref and bound_scope_ref != owner_scope_ref:
                raise ValueError("LangGraph session belongs to a different business identity")

            thread_id = str(binding.get("thread_id") or "")
            if not thread_id:
                thread_id = self._identity_thread_id(session_id, owner_scope_ref)
                if self._invocation_uses_legacy_session() and await self._thread_has_checkpoint(
                    session_id
                ):
                    thread_id = session_id

            await core.set_binding_by_session_id(
                session_id,
                "langgraph",
                {
                    "external_session_id": session_id,
                    "thread_id": thread_id,
                    "checkpoint_ns": self._managed_checkpoint_namespace,
                    "owner_scope_ref": owner_scope_ref,
                },
            )
            self._identity_thread_bindings[cache_key] = thread_id
            return self._get_config(thread_id)
