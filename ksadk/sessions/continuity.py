from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ksadk.sessions.base import BaseSessionService, Session


class SessionContinuityLevel(str, Enum):
    UI_ONLY = "ui_only"
    SEMANTIC = "semantic"
    RUNTIME = "runtime"
    EXACT = "exact"


@dataclass
class SessionContinuityStatus:
    level: SessionContinuityLevel
    path: str
    runner: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "Level": self.level.value,
            "Path": self.path,
            "Runner": self.runner,
        }
        if self.details:
            payload["Details"] = self.details
        return payload


class ConversationSessionCore:
    def __init__(self, service: BaseSessionService):
        self.service = service

    @staticmethod
    def binding_scope(runner_key: str) -> str:
        return f"runner_binding:{runner_key}"

    @staticmethod
    def runtime_scope(runner_key: str) -> str:
        return f"runner_runtime:{runner_key}"

    async def _load_session(self, session_id: str) -> Optional[Session]:
        if not session_id:
            return None
        return await self.service.get_session(session_id)

    async def get_binding_by_session_id(self, session_id: str, runner_key: str) -> dict[str, Any]:
        session = await self._load_session(session_id)
        if session is None:
            return {}
        state = await self.service.get_state(
            agent_id=session.agent_id,
            user_id=session.user_id,
            session_id=session.id,
            scope=self.binding_scope(runner_key),
        )
        return dict(state.state) if state else {}

    async def set_binding_by_session_id(
        self,
        session_id: str,
        runner_key: str,
        state_delta: dict[str, Any],
    ) -> dict[str, Any]:
        session = await self._load_session(session_id)
        if session is None:
            return {}
        state = await self.service.update_state(
            agent_id=session.agent_id,
            user_id=session.user_id,
            session_id=session.id,
            scope=self.binding_scope(runner_key),
            state_delta=state_delta,
        )
        return dict(state.state)

    async def get_runtime_state_by_session_id(self, session_id: str, runner_key: str) -> dict[str, Any]:
        session = await self._load_session(session_id)
        if session is None:
            return {}
        state = await self.service.get_state(
            agent_id=session.agent_id,
            user_id=session.user_id,
            session_id=session.id,
            scope=self.runtime_scope(runner_key),
        )
        return dict(state.state) if state else {}

    async def set_runtime_state_by_session_id(
        self,
        session_id: str,
        runner_key: str,
        state_delta: dict[str, Any],
    ) -> dict[str, Any]:
        session = await self._load_session(session_id)
        if session is None:
            return {}
        state = await self.service.update_state(
            agent_id=session.agent_id,
            user_id=session.user_id,
            session_id=session.id,
            scope=self.runtime_scope(runner_key),
            state_delta=state_delta,
        )
        return dict(state.state)


class RunnerSessionAdapter:
    def runner_key(self, runner: Any) -> str:
        runner_type = getattr(getattr(runner, "detection_result", None), "type", None)
        runner_value = getattr(runner_type, "value", "") if runner_type is not None else ""
        if isinstance(runner_value, str) and runner_value.strip():
            return runner_value.strip()
        runner_name = getattr(getattr(runner, "detection_result", None), "name", "")
        return str(runner_name or runner.__class__.__name__).strip().lower()

    def continuity_status(
        self,
        *,
        runner: Any,
        binding_state: dict[str, Any],
        runtime_state: dict[str, Any],
    ) -> SessionContinuityStatus:
        path = str(runtime_state.get("path") or "replay")
        level = runtime_state.get("level") or SessionContinuityLevel.SEMANTIC.value
        return SessionContinuityStatus(
            level=SessionContinuityLevel(str(level)),
            path=path,
            runner=self.runner_key(runner),
            details={k: v for k, v in runtime_state.items() if k not in {"path", "level"}},
        )

    async def describe_continuity(
        self,
        *,
        runner: Any,
        session: Session,
        core: ConversationSessionCore,
    ) -> SessionContinuityStatus:
        runner_key = self.runner_key(runner)
        binding_state = await core.get_binding_by_session_id(session.id, runner_key)
        runtime_state = await core.get_runtime_state_by_session_id(session.id, runner_key)
        return self.continuity_status(
            runner=runner,
            binding_state=binding_state,
            runtime_state=runtime_state,
        )

    async def bind_session(
        self,
        *,
        runner: Any,
        core: ConversationSessionCore,
        session_id: str,
        external_session_id: str,
        internal_session_id: str,
    ) -> dict[str, Any]:
        if not session_id:
            return {}
        return await core.set_binding_by_session_id(
            session_id,
            self.runner_key(runner),
            {
                "external_session_id": external_session_id,
                "internal_session_id": internal_session_id,
            },
        )

    async def persist_turn(
        self,
        *,
        runner: Any,
        core: ConversationSessionCore,
        session_id: str,
        state_delta: dict[str, Any],
    ) -> dict[str, Any]:
        if not session_id or not state_delta:
            return {}
        return await core.set_runtime_state_by_session_id(
            session_id,
            self.runner_key(runner),
            state_delta,
        )

    def prepare_request(self, *, runner: Any, input_data: dict[str, Any]) -> dict[str, Any]:
        return dict(input_data)

    async def clear_session(
        self,
        *,
        runner: Any,
        core: ConversationSessionCore,
        session_id: str,
    ) -> None:
        await self.persist_turn(
            runner=runner,
            core=core,
            session_id=session_id,
            state_delta={"path": "replay", "level": SessionContinuityLevel.SEMANTIC.value},
        )


class TranscriptReplayAdapter(RunnerSessionAdapter):
    pass


class LangChainSessionAdapter(RunnerSessionAdapter):
    def continuity_status(
        self,
        *,
        runner: Any,
        binding_state: dict[str, Any],
        runtime_state: dict[str, Any],
    ) -> SessionContinuityStatus:
        if runtime_state:
            return super().continuity_status(
                runner=runner,
                binding_state=binding_state,
                runtime_state=runtime_state,
            )

        path = "replay"
        module = getattr(runner, "_module", None)
        if callable(getattr(module, "ksadk_prepare_input", None)):
            path = "standard_hook"
        else:
            try:
                from langchain_core.runnables.history import RunnableWithMessageHistory

                if isinstance(getattr(runner, "_agent", None), RunnableWithMessageHistory):
                    path = "runnable_with_message_history"
            except Exception:
                pass

        return SessionContinuityStatus(
            level=SessionContinuityLevel.SEMANTIC,
            path=path,
            runner=self.runner_key(runner),
        )


class LangGraphSessionAdapter(RunnerSessionAdapter):
    def continuity_status(
        self,
        *,
        runner: Any,
        binding_state: dict[str, Any],
        runtime_state: dict[str, Any],
    ) -> SessionContinuityStatus:
        if runtime_state:
            return super().continuity_status(
                runner=runner,
                binding_state=binding_state,
                runtime_state=runtime_state,
            )
        agent = getattr(runner, "_agent", None)
        has_checkpointer = bool(
            getattr(agent, "checkpointer", None) or getattr(agent, "_checkpointer", None)
        )
        module = getattr(runner, "_module", None)
        has_hook = callable(getattr(module, "ksadk_prepare_state", None))
        if has_hook:
            path = "standard_hook"
        elif has_checkpointer:
            path = "checkpoint"
        else:
            path = "replay"
        return SessionContinuityStatus(
            level=SessionContinuityLevel.RUNTIME if has_checkpointer else SessionContinuityLevel.SEMANTIC,
            path=path,
            runner=self.runner_key(runner),
        )


class ADKSessionAdapter(RunnerSessionAdapter):
    def continuity_status(
        self,
        *,
        runner: Any,
        binding_state: dict[str, Any],
        runtime_state: dict[str, Any],
    ) -> SessionContinuityStatus:
        if runtime_state:
            return super().continuity_status(
                runner=runner,
                binding_state=binding_state,
                runtime_state=runtime_state,
            )
        has_native_session = bool(getattr(runner, "_short_term_memory", None)) or any(
            str((__import__("os").environ.get(name) or "")).strip()
            for name in (
                "KSADK_ADK_SESSION_BACKEND",
                "KSADK_ADK_SESSION_PATH",
                "KSADK_ADK_SESSION_URL",
                "KSADK_STM_BACKEND",
                "KSADK_STM_PATH",
                "KSADK_STM_URL",
                "KSADK_STM_DB_PATH",
                "KSADK_STM_DB_URL",
                "KSADK_SESSION_BACKEND",
                "KSADK_SESSION_DSN",
            )
        )
        return SessionContinuityStatus(
            level=SessionContinuityLevel.SEMANTIC,
            path="native_session" if has_native_session else "replay",
            runner=self.runner_key(runner),
        )
