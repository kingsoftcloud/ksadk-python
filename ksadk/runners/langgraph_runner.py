"""
LangGraphRunner - LangGraph 框架运行时

直接透传 LangGraph 原生能力，最小化封装
"""

import asyncio
import os
import uuid
import re
import sqlite3
from typing import Any, AsyncIterator, Dict, Mapping
import base64
from pathlib import Path

from ksadk.runners.base_runner import BaseRunner
from ksadk.runners.usage_accumulator import accumulate_usage
from ksadk.sessions.continuity import LangGraphSessionAdapter
from ksadk.runners.utils import get_langfuse_callbacks, get_langfuse_metadata, load_agent_module
from langgraph.types import Command
from ksadk.conversations.attachments import classify_attachment_kind, read_attachment_uri_bytes
from ksadk.conversations.reasoning_markup import ReasoningMarkupParser, strip_reasoning_markup


class LangGraphRunner(BaseRunner):
    """LangGraph 框架运行时
    
    透传原生 LangGraph 功能，支持任意 State 格式
    """

    def __init__(self, detection_result: Any, project_dir: str):
        super().__init__(detection_result, project_dir)
        self._managed_checkpoint_lock = asyncio.Lock()
        self._managed_checkpoint_prepared = False
        self._managed_checkpoint_error: tuple[str, str] | None = None
        self._managed_checkpoint_pool: Any = None
        self._managed_checkpoint_namespace = ""

    def load_agent(self) -> None:
        self._load_agent(force_reload=False)

    def _load_agent(self, *, force_reload: bool) -> None:
        """加载 LangGraph 编译后的图"""
        self._agent, self._module = load_agent_module(
            self.project_dir,
            self.detection_result.entry_point,
            self.detection_result.agent_variable,
            force_reload=force_reload,
        )
        self._loaded_model_name = self.normalize_requested_model(
            os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
        )
        
        if not hasattr(self._agent, "invoke"):
            raise TypeError("加载的对象不是有效的 LangGraph CompiledGraph")

    def prepare_for_request(self, model: str | None) -> None:
        normalized = self.sync_process_model_env(model)
        if normalized is None or self._agent is None:
            return
        if normalized == getattr(self, "_loaded_model_name", None):
            return
        self._load_agent(force_reload=True)

    def get_session_adapter(self):
        return LangGraphSessionAdapter()

    def describe_lazy_checkpoint_capability(self) -> dict[str, Any] | None:
        """Describe a checkpointer created lazily by a custom runner.

        Standard LangGraph runners expose the compiled graph through ``_agent``.
        Custom runners that construct a graph per invocation can override this
        hook so bootstrap capability discovery does not depend on a resident
        graph object.
        """
        return None

    @staticmethod
    def _checkpoint_backend_from_saver(checkpointer: Any) -> str:
        for saver_type in type(checkpointer).__mro__:
            qualified_name = f"{saver_type.__module__}.{saver_type.__name__}".lower()
            if "checkpoint.postgres" in qualified_name or "postgressaver" in qualified_name:
                return "postgres"
            if "checkpoint.sqlite" in qualified_name or "sqlitesaver" in qualified_name:
                return "sqlite"
            if "checkpoint.memory" in qualified_name or saver_type.__name__.lower() in {
                "memorysaver",
                "inmemorysaver",
            }:
                return "memory"
        return "unknown"

    @staticmethod
    def _sqlite_target_storage(database: Any) -> str:
        target = os.fspath(database).strip() if isinstance(database, (str, os.PathLike)) else ""
        if not target:
            return "memory"
        lowered = target.lower()
        if lowered == ":memory:":
            return "memory"
        if lowered.startswith("file:"):
            path, _, query = lowered.partition("?")
            if path in {"file:", "file::memory:"} or "mode=memory" in query.split("&"):
                return "memory"
        return "file"

    @classmethod
    def _sqlite_checkpoint_storage(cls, checkpointer: Any) -> str:
        connection = getattr(checkpointer, "conn", None)
        if isinstance(connection, sqlite3.Connection):
            try:
                rows = connection.execute("PRAGMA database_list").fetchall()
            except Exception:
                return "unknown"
            for row in rows:
                if len(row) >= 3 and row[1] == "main":
                    return "file" if str(row[2] or "").strip() else "memory"
            return "unknown"

        # AsyncSqliteSaver keeps the original aiosqlite connector closure. Its
        # public PRAGMA API is async, while capability discovery is synchronous,
        # so inspect the connection target and fail closed if it is unavailable.
        connector = getattr(connection, "_connector", None)
        code = getattr(connector, "__code__", None)
        closure = getattr(connector, "__closure__", None)
        if code is None or closure is None:
            return "unknown"
        try:
            closed_values = {
                name: cell.cell_contents
                for name, cell in zip(code.co_freevars, closure)
            }
        except (AttributeError, ValueError):
            return "unknown"
        if "database" not in closed_values:
            return "unknown"
        return cls._sqlite_target_storage(closed_values["database"])

    @classmethod
    def _checkpoint_capability_for_backend(
        cls,
        backend: str,
        *,
        checkpointer: Any = None,
    ) -> dict[str, Any]:
        if backend == "postgres":
            return {
                "Supported": True,
                "Backend": "postgres",
                "Scope": "shared",
                "Durable": True,
                "SharedAcrossPods": True,
                "ResumeMode": "time_travel",
                "Reason": "",
            }
        if backend == "sqlite":
            if checkpointer is not None:
                storage = cls._sqlite_checkpoint_storage(checkpointer)
                if storage != "file":
                    return {
                        "Supported": False,
                        "Backend": "sqlite",
                        "Scope": "process_local" if storage == "memory" else "unknown",
                        "Durable": False,
                        "SharedAcrossPods": False,
                        "ResumeMode": "none",
                        "ReasonCode": "CHECKPOINTER_NOT_DURABLE",
                        "Reason": (
                            "In-memory SQLite checkpoint cannot be recovered after process restart"
                            if storage == "memory"
                            else "SQLite checkpoint file target cannot be verified as durable"
                        ),
                    }
            return {
                "Supported": True,
                "Backend": "sqlite",
                "Scope": "pod_local",
                "Durable": True,
                "SharedAcrossPods": False,
                "ResumeMode": "time_travel",
                "Reason": (
                    "SQLite checkpoint is durable for local web debugging "
                    "but is not shared across pods"
                ),
            }
        if backend == "memory":
            return {
                "Supported": False,
                "Backend": "memory",
                "Scope": "process_local",
                "Durable": False,
                "SharedAcrossPods": False,
                "ResumeMode": "none",
                "ReasonCode": "CHECKPOINTER_NOT_DURABLE",
                "Reason": (
                    "In-memory checkpoint cannot be recovered after process restart "
                    "or across pods"
                ),
            }
        return {
            "Supported": False,
            "Backend": "unknown",
            "Scope": "unknown",
            "Durable": False,
            "SharedAcrossPods": False,
            "ResumeMode": "none",
            "ReasonCode": "CHECKPOINTER_NOT_DURABLE",
            "Reason": "LangGraph checkpointer backend is not recognized as durable",
        }

    @staticmethod
    def _invalid_lazy_checkpoint_capability(
        capability: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = capability or {}
        return {
            "Supported": False,
            "Backend": str(source.get("Backend") or "unknown").strip().lower(),
            "Scope": str(source.get("Scope") or "unknown").strip().lower(),
            "Durable": bool(source.get("Durable")),
            "SharedAcrossPods": bool(source.get("SharedAcrossPods")),
            "ResumeMode": "none",
            "ReasonCode": "CHECKPOINTER_NOT_DURABLE",
            "Reason": "Lazy LangGraph checkpoint capability is invalid or not durably resumable",
        }

    @classmethod
    def _normalize_lazy_checkpoint_capability(cls, capability: Any) -> dict[str, Any]:
        if not isinstance(capability, Mapping):
            return cls._invalid_lazy_checkpoint_capability()

        backend = str(capability.get("Backend") or "unknown").strip().lower()
        scope = str(capability.get("Scope") or "unknown").strip().lower()
        durable = capability.get("Durable") is True
        shared = capability.get("SharedAcrossPods") is True
        resume_mode = str(capability.get("ResumeMode") or "none").strip().lower()
        supported = capability.get("Supported") is True

        if supported:
            valid = (
                durable
                and resume_mode == "time_travel"
                and (
                    (backend == "postgres" and scope == "shared" and shared)
                    or (backend == "sqlite" and scope == "pod_local" and not shared)
                )
            )
            if not valid:
                return cls._invalid_lazy_checkpoint_capability(capability)

        return {
            "Supported": supported,
            "Backend": backend,
            "Scope": scope,
            "Durable": durable,
            "SharedAcrossPods": shared,
            "ResumeMode": resume_mode if supported else "none",
            "Reason": str(capability.get("Reason") or "").strip(),
        }

    def describe_checkpoint_capability(self) -> dict[str, Any]:
        agent = getattr(self, "_agent", None)
        checkpointer = getattr(agent, "checkpointer", None)
        if checkpointer is None:
            checkpointer = getattr(agent, "_checkpointer", None)
        if checkpointer is None:
            if self._managed_checkpoint_error is not None:
                reason_code, reason = self._managed_checkpoint_error
                return {
                    "Supported": False,
                    "Backend": "postgres",
                    "Scope": "unknown",
                    "Durable": False,
                    "SharedAcrossPods": False,
                    "ResumeMode": "none",
                    "ReasonCode": reason_code,
                    "Reason": reason,
                }
            lazy_capability = self.describe_lazy_checkpoint_capability()
            if lazy_capability is not None:
                return self._normalize_lazy_checkpoint_capability(lazy_capability)
            return {
                "Supported": False,
                "Backend": "none",
                "Scope": "unknown",
                "Durable": False,
                "SharedAcrossPods": False,
                "ReasonCode": "CHECKPOINTER_NOT_DURABLE",
                "Reason": "LangGraph graph has no configured checkpointer",
            }
        backend = self._checkpoint_backend_from_saver(checkpointer)
        return self._checkpoint_capability_for_backend(backend, checkpointer=checkpointer)

    def get_runtime_capabilities(self) -> dict[str, Any]:
        capabilities = super().get_runtime_capabilities()
        reason_code = str(capabilities["Checkpoint"].get("ReasonCode") or "")
        if reason_code:
            capabilities["ResumeRun"]["ReasonCode"] = reason_code
        capabilities["SessionContinuity"] = {
            "Supported": True,
            "Type": "checkpoint"
            if capabilities["Checkpoint"].get("Supported")
            else "semantic_replay",
            "Level": "runtime"
            if capabilities["Checkpoint"].get("Supported")
            else "semantic",
            "Reason": "",
        }
        return capabilities

    @staticmethod
    def _env_flag(name: str) -> bool:
        return str(os.getenv(name) or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _resolve_checkpoint_namespace() -> str:
        session_namespace = str(os.getenv("KSADK_SESSION_NAMESPACE") or "").strip()
        if session_namespace:
            return session_namespace
        agent_id = str(
            os.getenv("AGENTENGINE_AGENT_ID")
            or os.getenv("KSADK_AGENT_ID")
            or "default"
        ).strip()
        return f"agent:{agent_id}"

    async def _create_managed_postgres_saver(self, dsn: str) -> tuple[Any, Any]:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        timeout = max(0.1, float(os.getenv("KSADK_SESSION_CONNECT_TIMEOUT") or "5"))
        pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=10,
            open=False,
            timeout=timeout,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        )
        try:
            await pool.open(wait=True, timeout=timeout)
            saver = AsyncPostgresSaver(pool)
            await saver.setup()
            return saver, pool
        except Exception:
            await pool.close()
            raise

    async def prepare_runtime_capabilities(self) -> None:
        if self._managed_checkpoint_prepared:
            return
        async with self._managed_checkpoint_lock:
            if self._managed_checkpoint_prepared:
                return

            checkpointer = getattr(self._agent, "checkpointer", None)
            if checkpointer is None:
                checkpointer = getattr(self._agent, "_checkpointer", None)
            if self._checkpoint_backend_from_saver(checkpointer) == "postgres":
                self._managed_checkpoint_namespace = (
                    self._resolve_checkpoint_namespace()
                )
                self._managed_checkpoint_prepared = True
                return

            auto_enabled = self._env_flag("KSADK_LANGGRAPH_AUTO_CHECKPOINT")
            dsn = str(
                os.getenv("KSADK_LANGGRAPH_CHECKPOINT_DSN")
                or os.getenv("KSADK_SESSION_DSN")
                or ""
            ).strip()
            if not auto_enabled or not dsn:
                self._managed_checkpoint_prepared = True
                return

            factory = getattr(self._module, "ksadk_graph_factory", None)
            if not callable(factory):
                self._managed_checkpoint_error = (
                    "LANGGRAPH_FACTORY_REQUIRED",
                    "LangGraph graph has no durable checkpointer; export "
                    "ksadk_graph_factory(*, checkpointer) for managed PostgreSQL checkpoints",
                )
                self._managed_checkpoint_prepared = True
                return

            pool = None
            try:
                saver, pool = await self._create_managed_postgres_saver(dsn)
                managed_graph = factory(checkpointer=saver)
                if not callable(getattr(managed_graph, "invoke", None)):
                    raise TypeError("ksadk_graph_factory must return a compiled LangGraph graph")
                self._agent = managed_graph
                self._managed_checkpoint_pool = pool
                self._managed_checkpoint_namespace = (
                    self._resolve_checkpoint_namespace()
                )
                self._managed_checkpoint_error = None
            except (ModuleNotFoundError, ImportError):
                self._managed_checkpoint_error = (
                    "DEPENDENCY_MISSING",
                    "langgraph-checkpoint-postgres and psycopg are required "
                    "for managed checkpoints",
                )
            except Exception as exc:
                error_name = type(exc).__name__.lower()
                reason_code = (
                    "SCHEMA_PERMISSION_DENIED"
                    if "privilege" in error_name or "permission" in error_name
                    else "DB_UNREACHABLE"
                )
                self._managed_checkpoint_error = (
                    reason_code,
                    "Managed LangGraph PostgreSQL checkpointer initialization failed",
                )
            finally:
                if pool is not None and self._managed_checkpoint_pool is None:
                    try:
                        await pool.close()
                    except Exception:
                        pass
                self._managed_checkpoint_prepared = True

    async def close(self) -> None:
        pool = self._managed_checkpoint_pool
        self._managed_checkpoint_pool = None
        if pool is not None:
            await pool.close()
        await super().close()

    def _get_config(self, session_id: str) -> dict:
        """获取运行配置"""
        config = {"configurable": {"thread_id": session_id}}
        if self._managed_checkpoint_namespace:
            config["configurable"]["checkpoint_ns"] = self._managed_checkpoint_namespace
        
        langfuse_callbacks = get_langfuse_callbacks()
        if langfuse_callbacks:
            config["callbacks"] = langfuse_callbacks
            config["metadata"] = get_langfuse_metadata(session_id)
        
        return config

    @staticmethod
    def _extract_langgraph_checkpoint_ref(payload: Dict[str, Any]) -> dict[str, Any]:
        framework_ref = payload.get("framework_ref") or {}
        if not isinstance(framework_ref, dict):
            return {}
        langgraph_ref = framework_ref.get("langgraph") or {}
        if not isinstance(langgraph_ref, dict):
            return {}
        return dict(langgraph_ref)

    @classmethod
    def _apply_checkpoint_resume_config(
        cls,
        config: dict[str, Any],
        *,
        session_id: str,
        checkpoint_ref: dict[str, Any],
    ) -> dict[str, Any]:
        checkpoint_id = str(checkpoint_ref.get("checkpoint_id") or "").strip()
        if not checkpoint_id:
            raise ValueError("checkpoint_resume requires framework_ref.langgraph.checkpoint_id")

        thread_id = str(checkpoint_ref.get("thread_id") or session_id or "").strip()
        if not thread_id:
            raise ValueError("checkpoint_resume requires session_id or framework_ref.langgraph.thread_id")

        next_config = dict(config)
        configurable = dict(next_config.get("configurable") or {})
        configurable["thread_id"] = thread_id
        configurable["checkpoint_ns"] = str(checkpoint_ref.get("checkpoint_ns") or "")
        configurable["checkpoint_id"] = checkpoint_id
        next_config["configurable"] = configurable
        return next_config

    @staticmethod
    def _checkpoint_ref_from_state(state: Any) -> dict[str, Any]:
        state_config = None
        if isinstance(state, dict):
            state_config = state.get("config")
        else:
            state_config = getattr(state, "config", None)
        if not isinstance(state_config, dict):
            return {}
        configurable = state_config.get("configurable") or {}
        if not isinstance(configurable, dict):
            return {}
        thread_id = str(configurable.get("thread_id") or "").strip()
        checkpoint_id = str(configurable.get("checkpoint_id") or "").strip()
        if not thread_id or not checkpoint_id:
            return {}
        next_nodes_raw = state.get("next") if isinstance(state, dict) else getattr(state, "next", None)
        next_nodes: list[str] = []
        if isinstance(next_nodes_raw, str):
            if next_nodes_raw.strip():
                next_nodes = [next_nodes_raw.strip()]
        elif isinstance(next_nodes_raw, (list, tuple, set)):
            next_nodes = [
                str(item).strip()
                for item in next_nodes_raw
                if str(item or "").strip()
            ]
        return {
            "langgraph": {
                "thread_id": thread_id,
                **(
                    {"checkpoint_ns": str(configurable.get("checkpoint_ns")).strip()}
                    if str(configurable.get("checkpoint_ns") or "").strip()
                    else {}
                ),
                "checkpoint_id": checkpoint_id,
                **({"next_node": next_nodes[0], "next_nodes": next_nodes} if next_nodes else {}),
            }
        }

    async def _latest_checkpoint_metadata(self, config: dict[str, Any]) -> dict[str, Any]:
        state = None
        try:
            if callable(getattr(self._agent, "aget_state", None)):
                state = await self._agent.aget_state(config)
            elif callable(getattr(self._agent, "get_state", None)):
                state = self._agent.get_state(config)
        except Exception:
            return {}
        framework_ref = self._checkpoint_ref_from_state(state)
        if not framework_ref:
            return {}
        langgraph_ref = framework_ref.get("langgraph") if isinstance(framework_ref, dict) else {}
        next_node = ""
        if isinstance(langgraph_ref, dict):
            next_node = str(langgraph_ref.get("next_node") or "").strip()
        checkpoint_capability = self.describe_checkpoint_capability()
        is_terminal = not bool(next_node)
        is_resumable = bool(
            checkpoint_capability.get("Supported")
            and next_node
            and checkpoint_capability.get("Scope") != "process_local"
        )
        return {
            "agentengine": {
                "framework": "langgraph",
                "framework_ref": framework_ref,
                "next_node": next_node,
                "is_terminal": is_terminal,
                "is_resumable": is_resumable,
                "resume_status": "resumable" if is_resumable else "disabled",
                "resume_disabled_reason": (
                    "该 checkpoint 已是终态；可选择更早恢复点重跑"
                    if is_terminal
                    else str(checkpoint_capability.get("Reason") or "")
                ),
                "backend": checkpoint_capability.get("Backend"),
                "scope": checkpoint_capability.get("Scope"),
                "durable": bool(checkpoint_capability.get("Durable")),
            }
        }

    async def _latest_state_usage(self, config: dict[str, Any]) -> dict[str, Any]:
        state = None
        try:
            if callable(getattr(self._agent, "aget_state", None)):
                state = await self._agent.aget_state(config)
            elif callable(getattr(self._agent, "get_state", None)):
                state = self._agent.get_state(config)
        except Exception:
            return {}
        values = getattr(state, "values", None)
        if values is not None:
            return self._extract_usage(values)
        return {}

    @staticmethod
    def _latest_checkpoint_config(config: dict[str, Any]) -> dict[str, Any]:
        """Select the newest state in a resumed thread, not the source checkpoint."""
        latest_config = dict(config)
        configurable = dict(latest_config.get("configurable") or {})
        configurable.pop("checkpoint_id", None)
        latest_config["configurable"] = configurable
        return latest_config

    @staticmethod
    def _ambient_context_text(payload: Dict[str, Any]) -> str:
        sections: list[str] = []
        kb_context = payload.get("kb_context") or {}
        kb_text = str(kb_context.get("formatted_text") or "").strip() if isinstance(kb_context, dict) else ""
        if kb_text:
            sections.append(f"Knowledge base context:\n{kb_text}")

        memory_context = payload.get("memory_context") or {}
        memory_text = (
            str(memory_context.get("formatted_text") or "").strip()
            if isinstance(memory_context, dict)
            else ""
        )
        if memory_text:
            sections.append(f"Long-term memory context:\n{memory_text}")

        return "\n\n".join(section for section in sections if section)

    @staticmethod
    def _strip_platform_context_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in payload.items()
            if key not in {"platform_context", "kb_context", "memory_context"}
        }

    def _has_prepare_state_hook(self) -> bool:
        module = getattr(self, "_module", None)
        return callable(getattr(module, "ksadk_prepare_state", None))

    def _prepare_state_with_hook(
        self,
        payload: Dict[str, Any],
        session_id: str,
        history: list,
        *,
        is_resume: bool = False,
    ) -> Dict[str, Any]:
        module = getattr(self, "_module", None)
        prepare_state = getattr(module, "ksadk_prepare_state", None)
        if not callable(prepare_state):
            return self._to_state(payload, history)

        normalized_payload = self._strip_platform_context_fields(payload)
        session_context = {
            "session_id": session_id,
            "history": list(history),
            "is_resume": bool(is_resume),
            "platform_context": payload.get("platform_context"),
            "kb_context": payload.get("kb_context"),
            "memory_context": payload.get("memory_context"),
        }
        prepared = prepare_state(dict(normalized_payload), session_context)
        if not isinstance(prepared, dict):
            raise TypeError("ksadk_prepare_state(payload, session_context) must return a dict")
        return prepared

    def _to_state(self, payload: Dict[str, Any], history: list) -> Dict[str, Any]:
        """将简化输入转换为 state，并保留除 input 外的附加字段。"""
        normalized_payload = self._strip_platform_context_fields(payload)
        ambient_text = self._ambient_context_text(payload)
        instructions = str(normalized_payload.pop("instructions", "") or "").strip()
        system_sections = [section for section in (instructions, ambient_text) if section]
        system_text = "\n\n".join(system_sections)

        if "input" in normalized_payload and "messages" not in normalized_payload:
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

            messages = []
            if system_text:
                messages.append(SystemMessage(content=system_text))
            for msg in history:
                role = msg.get("role")
                content = msg.get("content", "")
                if role == "user":
                    messages.append(HumanMessage(content=content))
                elif role in ("assistant", "model"):
                    messages.append(AIMessage(content=content))

            user_input = normalized_payload["input"] or "[empty message]"
            attachments = list(normalized_payload.get("attachments") or [])
            model_metadata = normalized_payload.get("model_metadata")
            user_content = self._build_langgraph_human_content(
                user_input,
                attachments,
                model_metadata=model_metadata if isinstance(model_metadata, dict) else None,
            )
            if not self._history_tail_matches_user_content(history, user_content):
                messages.append(HumanMessage(content=user_content))
            state = {k: v for k, v in normalized_payload.items() if k != "input"}
            state["messages"] = messages
            return state

        if "messages" in normalized_payload:
            state = dict(normalized_payload)
            if system_text and isinstance(state.get("messages"), list):
                from langchain_core.messages import SystemMessage

                state["messages"] = [SystemMessage(content=system_text), *state["messages"]]
            return state

        return normalized_payload

    @classmethod
    def _history_tail_matches_user_content(cls, history: list, user_content: Any) -> bool:
        if not history:
            return False
        tail = history[-1]
        if not isinstance(tail, dict) or tail.get("role") != "user":
            return False
        tail_text = cls._normalizable_text_content(tail.get("content"))
        user_text = cls._normalizable_text_content(user_content)
        return tail_text is not None and tail_text == user_text

    @staticmethod
    def _normalizable_text_content(content: Any) -> str | None:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "text":
                    return None
                text_parts.append(str(item.get("text") or ""))
            return "\n".join(text_parts).strip()
        return None

    @staticmethod
    def _build_langgraph_human_content(
        user_input: str,
        attachments: list[dict[str, Any]],
        *,
        model_metadata: dict[str, Any] | None,
    ) -> Any:
        del model_metadata
        image_blocks: list[dict[str, Any]] = []

        for attachment in attachments or []:
            if not isinstance(attachment, dict):
                continue

            mime_type = str(attachment.get("mime_type") or "application/octet-stream")
            display_name = str(attachment.get("display_name") or "")
            if classify_attachment_kind(mime_type, display_name) != "image":
                continue

            data_b64 = str(attachment.get("data") or "").strip()
            transport = str(attachment.get("transport") or "")
            file_uri = str(attachment.get("file_uri") or "").strip()

            if transport == "inline" and data_b64:
                image_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{data_b64}",
                        },
                    }
                )
                continue

            if file_uri.startswith(("http://", "https://")):
                image_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": file_uri,
                        },
                    }
                )
                continue

            file_uri = attachment.get("file_uri")
            if not file_uri:
                continue

            raw = read_attachment_uri_bytes(file_uri)
            if not raw:
                continue

            image_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}",
                    },
                }
            )

        if not image_blocks:
            return user_input

        content: list[dict[str, Any]] = []
        if user_input:
            content.append({"type": "text", "text": user_input})
        content.extend(image_blocks)
        return content

    async def _invoke_graph(
        self,
        payload: Any,
        *,
        config: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> Any:
        if hasattr(self._agent, "ainvoke"):
            kwargs = self._build_optional_call_kwargs(
                self._agent.ainvoke,
                config=config,
                context=context,
            )
            return await self._agent.ainvoke(payload, **kwargs)
        kwargs = self._build_optional_call_kwargs(
            self._agent.invoke,
            config=config,
            context=context,
        )
        return self._agent.invoke(payload, **kwargs)

    async def _invoke_from_stream_events(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        chunks: list[dict[str, Any]] = []
        accumulated_text = ""
        final_chunk: dict[str, Any] | None = None
        metadata: dict[str, Any] = {}

        async for chunk in self.stream(input_data):
            chunks.append(dict(chunk))
            chunk_type = chunk.get("type")
            if chunk_type == "text":
                accumulated_text += str(chunk.get("delta") or "")
            elif chunk_type == "final":
                final_chunk = dict(chunk)
            elif chunk_type == "checkpoint":
                chunk_metadata = chunk.get("metadata")
                if isinstance(chunk_metadata, Mapping):
                    metadata.update(dict(chunk_metadata))
            elif chunk_type == "interrupt":
                return {
                    "type": "interrupt",
                    "interrupt_info": chunk.get("interrupt_info"),
                    "session_id": chunk.get("session_id") or input_data.get("session_id"),
                    "output": (
                        chunk.get("interrupt_info", {}).get("message", "需要用户确认")
                        if isinstance(chunk.get("interrupt_info"), Mapping)
                        else "需要用户确认"
                    ),
                    "raw": {"chunks": chunks},
                }

        if final_chunk:
            output_text = str(final_chunk.get("output") or accumulated_text)
            final_metadata = final_chunk.get("metadata")
            if isinstance(final_metadata, Mapping):
                metadata = {**dict(final_metadata), **metadata}
            result: dict[str, Any] = {"output": output_text, "raw": {"chunks": chunks}}
            usage = final_chunk.get("usage")
            if isinstance(usage, Mapping) and usage:
                result["usage"] = dict(usage)
            if metadata:
                result["metadata"] = metadata
            return result

        return {"output": accumulated_text, "raw": {"chunks": chunks}}

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """调用 LangGraph 图
        
        支持两种输入格式：
        1. 简化格式: {"input": "hello"} - 自动转换为 messages
        2. 原生格式: {"messages": [...]} 或自定义 State - 直接透传
        """
        await self.prepare_runtime_capabilities()
        payload = dict(input_data)
        force_graph_invoke = bool(payload.pop("_ksadk_force_graph_invoke", False))
        if not force_graph_invoke and hasattr(self._agent, "astream_events"):
            return await self._invoke_from_stream_events(payload)

        session_id = payload.pop("session_id", None) or str(uuid.uuid4())[:8]
        is_resume = payload.pop("resume", False)
        is_checkpoint_resume = bool(payload.pop("checkpoint_resume", False))
        checkpoint_ref = self._extract_langgraph_checkpoint_ref(payload)
        history = payload.pop("history", [])
        native_context = self.build_native_context(payload.get("platform_context"))
        normalized_payload = self._strip_platform_context_fields(payload)
        
        config = self._get_config(session_id)
        if is_checkpoint_resume:
            config = self._apply_checkpoint_resume_config(
                config,
                session_id=session_id,
                checkpoint_ref=checkpoint_ref,
            )
        
        # 判断输入格式 / resume
        if is_checkpoint_resume:
            state = None
        elif self._has_prepare_state_hook():
            state = self._prepare_state_with_hook(payload, session_id, history, is_resume=is_resume)
        elif is_resume:
            if "input" in normalized_payload and len(normalized_payload) == 1:
                state = normalized_payload["input"]
            else:
                state = normalized_payload
        else:
            state = self._to_state(payload, history)

        try:
            if is_checkpoint_resume:
                result = await self._invoke_graph(
                    None,
                    config=config,
                    context=native_context,
                )
            elif is_resume:
                result = await self._invoke_graph(
                    Command(resume=state),
                    config=config,
                    context=native_context,
                )
            else:
                result = await self._invoke_graph(
                    state,
                    config=config,
                    context=native_context,
                )

            output = {"output": self._extract_output(result), "raw": result}
            usage = self._extract_usage(result)
            if usage:
                output["usage"] = usage
            last_usage = self._extract_last_usage(result)
            if last_usage:
                output.setdefault("metadata", {})["last_usage"] = last_usage
            metadata = await self._latest_checkpoint_metadata(config)
            if metadata:
                output["metadata"] = {**(output.get("metadata") or {}), **metadata}
            return output
            
        except Exception as e:
            if "Interrupt" in type(e).__name__:
                interrupt_info = self._get_interrupt_info(self._agent.get_state(config))
                return {
                    "type": "interrupt",
                    "interrupt_info": interrupt_info,
                    "session_id": session_id,
                    "output": interrupt_info.get("message", "需要用户确认") if isinstance(interrupt_info, dict) else "需要用户确认",
                }
            raise

    def _extract_output(self, result: Any) -> str:
        """从结果中提取输出文本"""
        if isinstance(result, dict):
            # 自定义 output 字段是业务显式出参，优先于内部 messages state。
            if "output" in result:
                return result["output"]
            # LangGraph 示例常用 answer 作为最终业务回答字段。
            if "answer" in result:
                return result["answer"]
            # 标准 messages 格式
            if "messages" in result:
                messages = result["messages"]
                if messages:
                    last = messages[-1]
                    return last.get("content", str(last)) if isinstance(last, dict) else getattr(last, "content", str(last))
        return str(result) if result else ""

    def _get_interrupt_info(self, state) -> dict:
        """从 state 中获取 interrupt 信息"""
        if hasattr(state, "tasks") and state.tasks:
            for task in state.tasks:
                if hasattr(task, "interrupts") and task.interrupts:
                    for intr in task.interrupts:
                        if hasattr(intr, "value"):
                            return intr.value
        return {}

    async def _stream_checkpoint_resume_updates(
        self,
        *,
        config: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> AsyncIterator[Dict[str, Any]]:
        if not callable(getattr(self._agent, "astream", None)):
            return

        kwargs = self._build_optional_call_kwargs(
            self._agent.astream,
            config=config,
            context=context,
        )
        if self._callable_accepts_keyword(self._agent.astream, "stream_mode"):
            kwargs["stream_mode"] = "updates"

        latest_output: Any = None
        emitted_update = False
        async for update in self._agent.astream(None, **kwargs):
            emitted_update = True
            latest_output = update
            if isinstance(update, Mapping):
                for node_name, node_output in update.items():
                    yield {
                        "type": "graph_update",
                        "node": str(node_name),
                        "output": node_output,
                    }
            else:
                yield {"type": "graph_update", "output": update}

        latest_config = self._latest_checkpoint_config(config)
        state_usage = await self._latest_state_usage(latest_config)
        state_output = ""
        try:
            state = None
            if callable(getattr(self._agent, "aget_state", None)):
                state = await self._agent.aget_state(latest_config)
            elif callable(getattr(self._agent, "get_state", None)):
                state = self._agent.get_state(latest_config)
            values = getattr(state, "values", None)
            if values is not None:
                state_output = self._extract_output(values)
        except Exception:
            state_output = ""
        final_output = state_output or self._extract_output(latest_output)
        yield {
            "type": "final",
            "output": final_output,
            **({"usage": state_usage} if state_usage else {}),
            **({"resume_noop": True} if not emitted_update else {}),
        }

        metadata = await self._latest_checkpoint_metadata(latest_config)
        if metadata:
            yield {"type": "checkpoint", "metadata": metadata}

    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 LangGraph 图"""
        await self.prepare_runtime_capabilities()
        payload = dict(input_data)
        payload.pop("_ksadk_force_graph_invoke", None)
        session_id = payload.pop("session_id", None) or str(uuid.uuid4())[:8]
        history = payload.pop("history", [])
        is_resume = payload.pop("resume", False)
        is_checkpoint_resume = bool(payload.pop("checkpoint_resume", False))
        checkpoint_ref = self._extract_langgraph_checkpoint_ref(payload)
        native_context = self.build_native_context(payload.get("platform_context"))
        normalized_payload = self._strip_platform_context_fields(payload)

        invoke_payload = dict(payload)
        invoke_payload["session_id"] = session_id
        if history:
            invoke_payload["history"] = history
        if is_resume:
            invoke_payload["resume"] = True
        if is_checkpoint_resume:
            invoke_payload["checkpoint_resume"] = True
        
        config = self._get_config(session_id)
        if is_checkpoint_resume:
            config = self._apply_checkpoint_resume_config(
                config,
                session_id=session_id,
                checkpoint_ref=checkpoint_ref,
            )

        if is_checkpoint_resume:
            state = None
        elif self._has_prepare_state_hook():
            state = self._prepare_state_with_hook(payload, session_id, history, is_resume=is_resume)
        elif is_resume:
            if "input" in normalized_payload and len(normalized_payload) == 1:
                state = normalized_payload["input"]
            else:
                state = normalized_payload
        else:
            state = self._to_state(payload, history)

        accumulated_text = ""
        accumulated_reasoning = ""
        inline_reasoning_parser = ReasoningMarkupParser()
        emitted_non_text_event = False
        final_output_text = ""
        final_output_usage: dict[str, Any] = {}
        final_output_last_usage: dict[str, Any] = {}
        model_run_usages: dict[str, dict[str, Any]] = {}
        model_run_order: list[str] = []
        stream_usage_run_keys: set[str] = set()
        latest_stream_usage: dict[str, Any] = {}

        def model_run_key(
            event: Mapping[str, Any],
            *,
            fallback_key: str | None = None,
        ) -> str:
            raw_run_id = event.get("run_id")
            return (
                str(raw_run_id)
                if raw_run_id
                else fallback_key or f"model-event-{len(model_run_order)}"
            )

        def record_model_usage(
            event: Mapping[str, Any],
            usage: dict[str, Any],
            *,
            fallback_key: str | None = None,
        ) -> None:
            if not usage:
                return
            run_key = model_run_key(event, fallback_key=fallback_key)
            if run_key not in model_run_usages:
                model_run_order.append(run_key)
            model_run_usages[run_key] = dict(usage)

        def accumulated_model_usage() -> dict[str, Any]:
            if len(model_run_order) == 1:
                return dict(model_run_usages.get(model_run_order[0]) or {})
            usage: dict[str, Any] = {}
            for run_key in model_run_order:
                usage = accumulate_usage(usage, model_run_usages.get(run_key) or {})
            return usage

        def latest_model_usage() -> dict[str, Any]:
            for run_key in reversed(model_run_order):
                usage = model_run_usages.get(run_key)
                if usage:
                    return dict(usage)
            return {}

        if is_checkpoint_resume and callable(getattr(self._agent, "astream", None)):
            try:
                async for chunk in self._stream_checkpoint_resume_updates(
                    config=config,
                    context=native_context,
                ):
                    yield chunk
                return
            except Exception as e:
                yield {
                    "type": "error",
                    "message": str(e) or "LangGraph checkpoint resume failed",
                    "checkpoint_id": str(checkpoint_ref.get("checkpoint_id") or ""),
                    "exception_type": type(e).__name__,
                }
                return

        if not hasattr(self._agent, "astream_events"):
            result = await self.invoke(invoke_payload)
            final_chunk = {"output": result.get("output", ""), "type": "final"}
            usage = self._extract_usage(result)
            if usage:
                final_chunk["usage"] = usage
            last_usage = self._extract_last_usage(result)
            if last_usage:
                final_chunk.setdefault("metadata", {})["last_usage"] = last_usage
            yield final_chunk
            return

        try:
            stream_input = None if is_checkpoint_resume else (Command(resume=state) if is_resume else state)
            stream_kwargs = {"version": "v2", "config": config}
            if native_context and self._callable_accepts_keyword(self._agent.astream_events, "context"):
                stream_kwargs["context"] = native_context
            async for event in self._agent.astream_events(stream_input, **stream_kwargs):
                event_kind = event.get("event", "")

                if event_kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if not chunk:
                        continue
                    chunk_usage = self._extract_usage(chunk)
                    if chunk_usage:
                        # Some LangChain providers attach cumulative usage to
                        # every stream chunk, and LangChain may then sum those
                        # cumulative snapshots into an inflated
                        # on_chat_model_end usage. For a concrete model run,
                        # keep the latest stream snapshot and ignore the later
                        # end usage for that same run_id.
                        latest_stream_usage = dict(chunk_usage)
                        if event.get("run_id"):
                            run_key = model_run_key(event)
                            stream_usage_run_keys.add(run_key)
                            record_model_usage(event, latest_stream_usage)

                    # 推理内容
                    reasoning = getattr(chunk, "reasoning_content", None)
                    if not reasoning and hasattr(chunk, "additional_kwargs"):
                        reasoning = chunk.additional_kwargs.get("reasoning_content")
                    
                    if reasoning:
                        accumulated_reasoning += reasoning
                        yield {"delta": reasoning, "type": "thinking"}

                    # 常规内容
                    if hasattr(chunk, "content") and chunk.content:
                        content = self._filter_tool_tags(chunk.content)
                        if isinstance(content, str):
                            if accumulated_reasoning and content.startswith(accumulated_reasoning):
                                content = content[len(accumulated_reasoning):]
                            elif reasoning and content.startswith(reasoning):
                                content = content[len(reasoning):]
                        if content:
                            for part in inline_reasoning_parser.feed(content):
                                if not part.text or not part.text.strip():
                                    continue
                                if part.kind == "thinking":
                                    accumulated_reasoning += part.text
                                    yield {"delta": part.text, "type": "thinking"}
                                else:
                                    accumulated_text += part.text
                                    yield {"delta": part.text, "type": "text"}

                elif event_kind == "on_chat_model_end":
                    data = event.get("data") or {}
                    output = data.get("output") if isinstance(data, Mapping) else None
                    usage = self._extract_usage(output) or self._extract_usage(data)
                    last_usage = self._extract_last_usage(output) or self._extract_last_usage(data)
                    run_key = model_run_key(event)
                    if run_key not in stream_usage_run_keys:
                        record_model_usage(event, last_usage or usage)

                elif event_kind == "on_tool_start":
                    emitted_non_text_event = True
                    yield {
                        "type": "tool_call",
                        "tool_name": event.get("name", "unknown"),
                        "tool_args": event.get("data", {}).get("input", {}),
                        "run_id": event.get("run_id"),
                    }
                
                elif event_kind == "on_tool_end":
                    emitted_non_text_event = True
                    tool_output = event.get("data", {}).get("output", "")
                    yield {
                        "type": "tool_result",
                        "tool_name": event.get("name", "unknown"),
                        "tool_args": event.get("data", {}).get("input", {}),
                        "tool_output": tool_output if isinstance(tool_output, dict) else (str(tool_output) if tool_output else ""),
                        "run_id": event.get("run_id"),
                    }
                    
                elif event_kind == "on_chain_end":
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict) and "__interrupt__" in output:
                        emitted_non_text_event = True
                        yield {"type": "interrupt", "interrupt_info": output["__interrupt__"], "session_id": session_id}
                        return
                    extracted_output = self._extract_output(output)
                    if extracted_output:
                        final_output_text = strip_reasoning_markup(str(extracted_output))
                    final_output_usage = self._extract_usage(output)
                    final_output_last_usage = self._extract_last_usage(output)

        except Exception as e:
            if "Interrupt" in type(e).__name__:
                yield {"type": "interrupt", "interrupt_info": self._get_interrupt_info(self._agent.get_state(config)), "session_id": session_id}
                return
            raise

        for part in inline_reasoning_parser.flush():
            if not part.text or not part.text.strip():
                continue
            if part.kind == "thinking":
                accumulated_reasoning += part.text
                yield {"delta": part.text, "type": "thinking"}
            else:
                accumulated_text += part.text
                yield {"delta": part.text, "type": "text"}

        if not accumulated_text:
            if final_output_text:
                final_chunk = {"output": final_output_text, "type": "final"}
                usage = accumulated_model_usage() or final_output_usage or latest_stream_usage
                last_usage = latest_model_usage() or final_output_last_usage or latest_stream_usage or usage
                if usage:
                    final_chunk["usage"] = usage
                if last_usage:
                    final_chunk.setdefault("metadata", {})["last_usage"] = last_usage
                yield final_chunk
            elif not emitted_non_text_event:
                result = await self.invoke(
                    {**invoke_payload, "_ksadk_force_graph_invoke": True}
                )
                final_chunk = {"output": result.get("output", ""), "type": "final"}
                usage = self._extract_usage(result)
                if usage:
                    final_chunk["usage"] = usage
                last_usage = self._extract_last_usage(result)
                if last_usage:
                    final_chunk.setdefault("metadata", {})["last_usage"] = last_usage
                yield final_chunk
                metadata = result.get("metadata") if isinstance(result, dict) else None
                if isinstance(metadata, dict) and metadata.get("agentengine"):
                    yield {"type": "checkpoint", "metadata": metadata}
                    return
        else:
            final_chunk = {"output": accumulated_text, "type": "final"}
            state_usage = await self._latest_state_usage(config)
            usage = accumulated_model_usage() or state_usage or final_output_usage or latest_stream_usage
            if usage:
                final_chunk["usage"] = usage
                last_usage = latest_model_usage() or state_usage or final_output_last_usage or latest_stream_usage or usage
                final_chunk.setdefault("metadata", {})["last_usage"] = last_usage
            yield final_chunk

        metadata = await self._latest_checkpoint_metadata(config)
        if metadata:
            yield {"type": "checkpoint", "metadata": metadata}

    def _filter_tool_tags(self, content: str) -> str:
        """过滤 <tool_call> 标签"""
        if not isinstance(content, str):
            return content
        content = re.sub(r'<tool_call>.*?</tool_call>', '', content, flags=re.DOTALL)
        content = re.sub(r'</?(?:tool_call|arg_key|arg_value)>', '', content)
        return content
