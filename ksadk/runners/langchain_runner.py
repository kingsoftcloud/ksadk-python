"""LangChain runner with session continuity aware input preparation."""

from __future__ import annotations

import inspect
import logging
import os
import uuid
from typing import Any, AsyncIterator, Dict, Optional

from ksadk.runners.base_runner import BaseRunner
from ksadk.runners.utils import (
    get_langfuse_callbacks,
    get_langfuse_metadata,
    load_agent_module,
    prepare_trace_metadata,
)
from ksadk.sessions.continuity import LangChainSessionAdapter

logger = logging.getLogger(__name__)


class LangChainRunner(BaseRunner):
    """LangChain framework runner."""

    def load_agent(self) -> None:
        self._load_agent(force_reload=False)

    def _load_agent(self, *, force_reload: bool) -> None:
        self._agent, self._module = load_agent_module(
            self.project_dir,
            self.detection_result.entry_point,
            self.detection_result.agent_variable,
            force_reload=force_reload,
        )
        self._loaded_model_name = self.normalize_requested_model(
            os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
        )

    def prepare_for_request(self, model: Optional[str]) -> None:
        normalized = self.sync_process_model_env(model)
        if normalized is None or self._agent is None:
            return
        if normalized == getattr(self, "_loaded_model_name", None):
            return
        self._load_agent(force_reload=True)

    def get_session_adapter(self):
        return LangChainSessionAdapter()

    def _get_config(self, session_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        config: dict[str, Any] = {}

        langfuse_callbacks = get_langfuse_callbacks()
        if langfuse_callbacks:
            config["callbacks"] = langfuse_callbacks

            metadata = get_langfuse_metadata(session_id)
            user_id, tags, _, _ = prepare_trace_metadata(session_id)
            if user_id:
                metadata["langfuse_user_id"] = user_id
            if tags:
                metadata["langfuse_tags"] = tags
            config["metadata"] = metadata

        return config or None

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        session_id = input_data.get("session_id") or str(uuid.uuid4())[:8]
        path = self._resolve_request_path()
        config = self._get_config(session_id)
        native_context = self.build_native_context(input_data.get("platform_context"))

        if path == "standard_hook":
            payload = self._prepare_with_standard_hook(input_data, session_id)
            result = await self._invoke_agent(payload, config=config, context=native_context)
        elif path == "runnable_with_message_history":
            result = await self._invoke_with_message_history(
                input_data,
                session_id,
                config=config,
                context=native_context,
            )
        else:
            payload = self._prepare_with_replay(input_data)
            result = await self._invoke_agent(payload, config=config, context=native_context)

        output = {"output": self._extract_output(result)}
        usage = self._extract_usage(result)
        if usage:
            output["usage"] = usage
        last_usage = self._extract_last_usage(result)
        if last_usage:
            output.setdefault("metadata", {})["last_usage"] = last_usage
        return output

    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        session_id = input_data.get("session_id") or str(uuid.uuid4())[:8]
        path = self._resolve_request_path()
        config = self._get_config(session_id)
        native_context = self.build_native_context(input_data.get("platform_context"))

        if path == "standard_hook":
            payload = self._prepare_with_standard_hook(input_data, session_id)
        elif path == "runnable_with_message_history":
            payload = {"input": self._prepare_message_history_input(input_data)}
            config = self._with_session_config(config, session_id)
        else:
            payload = self._prepare_with_replay(input_data)

        accumulated_text = ""
        last_chunk: Any = None
        final_output_text = ""
        emitted_non_text_event = False
        message_snapshots: dict[str, str] = {}

        try:
            if self._should_stream_events(payload):
                kwargs = self._build_optional_call_kwargs(
                    self._agent.astream_events,
                    config=config,
                    context=native_context,
                )
                kwargs["version"] = "v2"
                async for event in self._agent.astream_events(payload, **kwargs):
                    if not isinstance(event, dict):
                        continue
                    event_kind = event.get("event", "")
                    data = event.get("data") or {}

                    if event_kind == "on_chat_model_stream":
                        chunk = data.get("chunk") if isinstance(data, dict) else None
                        if chunk is None:
                            continue
                        last_chunk = chunk
                        delta, chunk_type = self._extract_chunk(chunk)
                        if delta:
                            if chunk_type == "text":
                                accumulated_text += delta
                            else:
                                emitted_non_text_event = True
                            yield {"delta": delta, "type": chunk_type}
                    elif event_kind == "on_tool_start":
                        emitted_non_text_event = True
                        yield {
                            "type": "tool_call",
                            "tool_name": event.get("name", "unknown"),
                            "tool_args": data.get("input", {}) if isinstance(data, dict) else {},
                            "run_id": event.get("run_id"),
                        }
                    elif event_kind == "on_tool_end":
                        emitted_non_text_event = True
                        tool_output = data.get("output", "") if isinstance(data, dict) else ""
                        yield {
                            "type": "tool_result",
                            "tool_name": event.get("name", "unknown"),
                            "tool_args": data.get("input", {}) if isinstance(data, dict) else {},
                            "tool_output": (
                                tool_output
                                if isinstance(tool_output, dict)
                                else (str(tool_output) if tool_output else "")
                            ),
                            "run_id": event.get("run_id"),
                        }
                    elif event_kind == "on_chain_end":
                        output = data.get("output") if isinstance(data, dict) else None
                        extracted_output = self._extract_recognized_output(output)
                        if extracted_output:
                            final_output_text = extracted_output
                        if self._extract_usage(output):
                            last_chunk = output
            elif hasattr(self._agent, "astream"):
                kwargs = self._build_optional_call_kwargs(
                    self._agent.astream,
                    config=config,
                    context=native_context,
                )
                async for chunk in self._agent.astream(payload, **kwargs):
                    last_chunk = chunk
                    message_state = self._extract_message_state(chunk)
                    if message_state:
                        content, message_key = message_state
                        previous = message_snapshots.get(message_key, "")
                        delta = self._snapshot_delta(content, previous)
                        message_snapshots[message_key] = content
                        chunk_type = "text"
                    else:
                        delta, chunk_type = self._extract_chunk(chunk)
                    if delta:
                        accumulated_text += delta
                        yield {"delta": delta, "type": chunk_type}
            elif hasattr(self._agent, "stream"):
                kwargs = self._build_optional_call_kwargs(
                    self._agent.stream,
                    config=config,
                    context=native_context,
                )
                for chunk in self._agent.stream(payload, **kwargs):
                    last_chunk = chunk
                    message_state = self._extract_message_state(chunk)
                    if message_state:
                        content, message_key = message_state
                        previous = message_snapshots.get(message_key, "")
                        delta = self._snapshot_delta(content, previous)
                        message_snapshots[message_key] = content
                        chunk_type = "text"
                    else:
                        delta, chunk_type = self._extract_chunk(chunk)
                    if delta:
                        accumulated_text += delta
                        yield {"delta": delta, "type": chunk_type}
        except Exception as exc:
            logger.warning("LangChain stream failed: %s", exc)

        if not accumulated_text:
            if final_output_text or emitted_non_text_event:
                final_chunk = {"output": final_output_text, "type": "final"}
                usage = self._extract_usage(last_chunk)
                if usage:
                    final_chunk["usage"] = usage
                last_usage = self._extract_last_usage(last_chunk)
                if last_usage:
                    final_chunk.setdefault("metadata", {})["last_usage"] = last_usage
                yield final_chunk
                return
            result = await self.invoke(input_data)
            final_chunk = {"output": result.get("output", ""), "type": "final"}
            usage = self._extract_usage(result)
            if usage:
                final_chunk["usage"] = usage
            last_usage = self._extract_last_usage(result)
            if last_usage:
                final_chunk.setdefault("metadata", {})["last_usage"] = last_usage
            yield final_chunk
            return

        final_chunk = {"output": accumulated_text, "type": "final"}
        usage = self._extract_usage(last_chunk)
        if usage:
            final_chunk["usage"] = usage
        last_usage = self._extract_last_usage(last_chunk)
        if last_usage:
            final_chunk.setdefault("metadata", {})["last_usage"] = last_usage
        yield final_chunk

    def _should_stream_events(self, payload: dict[str, Any]) -> bool:
        """Use LangGraph events for LangChain create_agent message-state agents."""
        return isinstance(payload.get("messages"), list) and hasattr(
            self._agent, "astream_events"
        )

    def _resolve_request_path(self) -> str:
        module = getattr(self, "_module", None)
        if callable(getattr(module, "ksadk_prepare_input", None)):
            return "standard_hook"
        if self._is_runnable_with_message_history():
            return "runnable_with_message_history"
        return "replay"

    def _prepare_with_standard_hook(
        self, input_data: Dict[str, Any], session_id: str
    ) -> dict[str, Any]:
        module = getattr(self, "_module", None)
        prepare_input = getattr(module, "ksadk_prepare_input", None)
        if not callable(prepare_input):
            return {"input": input_data.get("input", "")}

        payload = {"input": input_data.get("input", "")}
        session_context = {
            "session_id": session_id,
            "history": list(input_data.get("history") or []),
            "input_parts": list(input_data.get("input_parts") or []),
            "attachments": list(input_data.get("attachments") or []),
            "attachment_results": list(input_data.get("attachment_results") or []),
            "instructions": input_data.get("instructions"),
            "platform_context": input_data.get("platform_context"),
            "kb_context": input_data.get("kb_context"),
            "memory_context": input_data.get("memory_context"),
        }
        builtin_context = self._ksadk_builtin_tool_context()
        if builtin_context:
            session_context.update(builtin_context)
        prepared = prepare_input(payload, session_context)
        return prepared if isinstance(prepared, dict) else payload

    @staticmethod
    def _ksadk_builtin_tool_context() -> dict[str, Any]:
        try:
            from ksadk.toolsets import (
                builtin_tool_descriptors_for_runtime,
                builtin_tools_mode,
                builtin_tools_profile,
            )

            mode = builtin_tools_mode(default="off")
            descriptors = builtin_tool_descriptors_for_runtime(mode=mode)
            if not descriptors:
                return {}
            return {
                "ksadk_tools": descriptors,
                "ksadk_builtin_tools_mode": mode,
                "ksadk_builtin_tools_profile": builtin_tools_profile("default"),
                "deferred_direct_injection_supported": False,
            }
        except Exception as exc:
            logger.warning("Failed to prepare ksadk built-in tool descriptors: %s", exc)
            return {}

    def _prepare_with_replay(self, input_data: Dict[str, Any]) -> dict[str, Any]:
        user_input = str(input_data.get("input", "") or "")
        history = list(input_data.get("history") or [])
        ambient_text = self._ambient_context_text(input_data)
        instructions = str(input_data.get("instructions") or "").strip()
        if not history and not ambient_text and not instructions:
            return {"input": user_input}
        return {
            "input": self._format_replay_prompt(
                user_input,
                history,
                ambient_text=ambient_text,
                instructions=instructions,
            )
        }

    @staticmethod
    def _ambient_context_text(input_data: Dict[str, Any]) -> str:
        sections: list[str] = []
        kb_context = input_data.get("kb_context") or {}
        kb_text = (
            str(kb_context.get("formatted_text") or "").strip()
            if isinstance(kb_context, dict)
            else ""
        )
        if kb_text:
            sections.append(f"Knowledge base context:\n{kb_text}")

        memory_context = input_data.get("memory_context") or {}
        memory_text = (
            str(memory_context.get("formatted_text") or "").strip()
            if isinstance(memory_context, dict)
            else ""
        )
        if memory_text:
            sections.append(f"Long-term memory context:\n{memory_text}")

        return "\n\n".join(section for section in sections if section)

    def _prepare_message_history_input(self, input_data: Dict[str, Any]) -> str:
        user_input = str(input_data.get("input", "") or "")
        context_text = self._request_context_text(input_data)
        if not context_text:
            return user_input
        current_input = user_input.strip() or "[empty message]"
        return f"{context_text}\n\nCurrent user input:\n{current_input}"

    def _request_context_text(self, input_data: Dict[str, Any]) -> str:
        ambient_text = self._ambient_context_text(input_data)
        instructions = str(input_data.get("instructions") or "").strip()
        return "\n\n".join(section for section in (instructions, ambient_text) if section)

    def _format_replay_prompt(
        self,
        user_input: str,
        history: list[dict[str, Any]],
        *,
        ambient_text: str = "",
        instructions: str = "",
    ) -> str:
        lines: list[str] = []
        if instructions:
            lines.append(instructions)
        if ambient_text:
            lines.append(ambient_text)
        if history:
            lines.append("Conversation history:")
        normalized_history: list[tuple[str, str]] = []
        for item in history:
            role = self._normalize_history_role(item.get("role"))
            content = str(item.get("content", "") or "").strip()
            if not role or not content:
                continue
            normalized_history.append((role, content))
            lines.append(f"{role}: {content}")

        if user_input.strip():
            if not normalized_history or normalized_history[-1] != ("user", user_input.strip()):
                lines.append(f"user: {user_input.strip()}")
        elif ambient_text and not history:
            lines.append("Current user input:\n[empty message]")

        return "\n".join(lines)

    @staticmethod
    def _normalize_history_role(role: Any) -> str:
        normalized = str(role or "").strip().lower()
        if normalized in {"assistant", "model"}:
            return "assistant"
        if normalized == "user":
            return "user"
        return normalized

    @staticmethod
    def _with_session_config(
        config: Optional[dict[str, Any]],
        session_id: str,
    ) -> dict[str, Any]:
        merged = dict(config or {})
        configurable = dict(merged.get("configurable") or {})
        configurable["session_id"] = session_id
        merged["configurable"] = configurable
        return merged

    async def _invoke_agent(
        self,
        payload: Any,
        *,
        config: Optional[dict[str, Any]],
        context: Optional[dict[str, Any]],
    ) -> Any:
        if hasattr(self._agent, "ainvoke"):
            kwargs = self._build_optional_call_kwargs(
                self._agent.ainvoke,
                config=config,
                context=context,
            )
            return await self._agent.ainvoke(payload, **kwargs)
        if hasattr(self._agent, "invoke"):
            kwargs = self._build_optional_call_kwargs(
                self._agent.invoke,
                config=config,
                context=context,
            )
            return self._agent.invoke(payload, **kwargs)
        if callable(self._agent):
            return self._agent(payload)
        raise TypeError("Agent 不支持 invoke 调用")

    def _is_runnable_with_message_history(self) -> bool:
        try:
            from langchain_core.runnables.history import RunnableWithMessageHistory

            return isinstance(self._agent, RunnableWithMessageHistory)
        except Exception:
            return False

    async def _invoke_with_message_history(
        self,
        input_data: Dict[str, Any],
        session_id: str,
        *,
        config: Optional[dict[str, Any]],
        context: Optional[dict[str, Any]],
    ) -> Any:
        context_text = self._request_context_text(input_data)
        payload = {"input": self._prepare_message_history_input(input_data)}
        session_config = self._with_session_config(config, session_id)
        wrapped_runnable = self._extract_wrapped_history_runnable()
        message_history = self._get_message_history_store(session_id)

        if context_text and wrapped_runnable is not None and message_history is not None:
            return await self._invoke_wrapped_history_with_ambient_context(
                input_data=input_data,
                wrapped_runnable=wrapped_runnable,
                message_history=message_history,
                session_config=session_config,
                context=context,
                ambient_text=context_text,
            )

        try:
            return await self._invoke_agent(payload, config=session_config, context=context)
        except Exception:
            if wrapped_runnable is None or message_history is None:
                raise
            return await self._invoke_wrapped_history_with_ambient_context(
                input_data=input_data,
                wrapped_runnable=wrapped_runnable,
                message_history=message_history,
                session_config=session_config,
                context=context,
                ambient_text=context_text,
            )

    async def _invoke_wrapped_runnable(
        self,
        runnable: Any,
        payload: Any,
        config: Optional[dict[str, Any]],
        context: Optional[dict[str, Any]],
    ) -> Any:
        if hasattr(runnable, "ainvoke"):
            kwargs = self._build_optional_call_kwargs(
                runnable.ainvoke,
                config=config,
                context=context,
            )
            return await runnable.ainvoke(payload, **kwargs)
        if hasattr(runnable, "invoke"):
            kwargs = self._build_optional_call_kwargs(
                runnable.invoke,
                config=config,
                context=context,
            )
            return runnable.invoke(payload, **kwargs)
        raise TypeError("Wrapped runnable does not support invoke")

    async def _invoke_wrapped_history_with_ambient_context(
        self,
        *,
        input_data: Dict[str, Any],
        wrapped_runnable: Any,
        message_history: Any,
        session_config: Optional[dict[str, Any]],
        context: Optional[dict[str, Any]],
        ambient_text: str,
    ) -> Any:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        user_input = str(input_data.get("input", "") or "")
        prompt_messages = list(getattr(message_history, "messages", []))
        if ambient_text:
            prompt_messages = [SystemMessage(content=ambient_text), *prompt_messages]
        prompt_messages.append(HumanMessage(content=user_input or "[empty message]"))
        result = await self._invoke_wrapped_runnable(
            wrapped_runnable,
            {"input": prompt_messages},
            session_config,
            context,
        )
        output_text = self._extract_output(result)
        await self._append_message_history(
            message_history,
            [
                HumanMessage(content=user_input or "[empty message]"),
                AIMessage(content=output_text),
            ],
        )
        return result

    def _extract_wrapped_history_runnable(self) -> Any | None:
        runnable_lambda = self._get_nested_attr(
            self._agent,
            ("bound", "bound", "last", "bound"),
        )
        if runnable_lambda is None:
            logger.debug(
                "Unable to inspect RunnableWithMessageHistory wrapper: "
                "missing bound.bound.last.bound"
            )
            return None

        func = getattr(runnable_lambda, "func", None)
        if func is None:
            logger.debug(
                "Unable to inspect RunnableWithMessageHistory wrapper: missing lambda func"
            )
            return None

        try:
            closure = inspect.getclosurevars(func).nonlocals
        except Exception as exc:
            logger.debug(
                "Unable to inspect RunnableWithMessageHistory wrapper: %s",
                exc,
            )
            return None
        return closure.get("runnable_async") or closure.get("runnable_sync")

    @staticmethod
    def _get_nested_attr(obj: Any, path: tuple[str, ...]) -> Any | None:
        current = obj
        for name in path:
            current = getattr(current, name, None)
            if current is None:
                return None
        return current

    def _get_message_history_store(self, session_id: str) -> Any | None:
        get_session_history = getattr(self._agent, "get_session_history", None)
        if not callable(get_session_history):
            return None
        return get_session_history(session_id)

    async def _append_message_history(self, history_store: Any, messages: list[Any]) -> None:
        if hasattr(history_store, "aadd_messages"):
            await history_store.aadd_messages(messages)
            return
        if hasattr(history_store, "add_messages"):
            history_store.add_messages(messages)
            return
        for message in messages:
            role = getattr(message, "type", "")
            content = getattr(message, "content", "")
            if role == "human" and hasattr(history_store, "add_user_message"):
                history_store.add_user_message(content)
            elif role == "ai" and hasattr(history_store, "add_ai_message"):
                history_store.add_ai_message(content)

    @staticmethod
    def _extract_output(result: Any) -> str:
        if isinstance(result, dict):
            if "output" in result:
                return result["output"]
            if "text" in result:
                return result["text"]
            messages = result.get("messages")
            if isinstance(messages, list) and messages:
                last = messages[-1]
                if isinstance(last, dict):
                    return str(last.get("content", str(last)))
                content = getattr(last, "content", None)
                if content is not None:
                    return str(content)
            return str(result)
        content = getattr(result, "content", None)
        if content is not None:
            return str(content)
        return str(result)

    @classmethod
    def _extract_recognized_output(cls, result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result

        if isinstance(result, dict):
            for key in ("output", "text"):
                if key not in result:
                    continue
                value = result[key]
                if isinstance(value, str):
                    return value
                extracted = cls._extract_recognized_output(value)
                if extracted:
                    return extracted
                if value is not None and not isinstance(value, (dict, list, tuple)):
                    return str(value)

            message_state = cls._extract_message_state(result)
            return message_state[0] if message_state else ""

        if isinstance(result, (list, tuple)):
            for item in reversed(result):
                extracted = cls._extract_recognized_output(item)
                if extracted:
                    return extracted
            return ""

        command_update = getattr(result, "update", None)
        if isinstance(command_update, dict):
            return cls._extract_recognized_output(command_update)

        content = cls._ai_message_content(result)
        return content if content is not None else ""

    @classmethod
    def _extract_message_state(cls, chunk: Any) -> tuple[str, str] | None:
        """Extract the newest AI message from LangGraph values/updates snapshots."""

        def visit(value: Any, path: tuple[str, ...]) -> tuple[str, str] | None:
            if not isinstance(value, dict):
                return None

            messages = value.get("messages")
            if isinstance(messages, list):
                for index in range(len(messages) - 1, -1, -1):
                    message = messages[index]
                    content = cls._ai_message_content(message)
                    if content is None:
                        continue
                    message_id = (
                        message.get("id")
                        if isinstance(message, dict)
                        else getattr(message, "id", None)
                    )
                    fallback_key = "/".join((*path, "messages", str(index)))
                    return content, str(message_id or fallback_key)

            for key, nested in value.items():
                if key == "messages" or not isinstance(nested, dict):
                    continue
                result = visit(nested, (*path, str(key)))
                if result:
                    return result
            return None

        return visit(chunk, ())

    @staticmethod
    def _ai_message_content(message: Any) -> str | None:
        if isinstance(message, dict):
            role = str(message.get("role") or message.get("type") or "").lower()
            if role not in {"ai", "assistant", "model", "aimessage", "aimessagechunk"}:
                return None
            content = message.get("content")
        else:
            role = str(getattr(message, "type", "") or "").lower()
            class_name = type(message).__name__.lower()
            if role not in {
                "ai",
                "assistant",
                "model",
                "aimessage",
                "aimessagechunk",
            } and not class_name.startswith("aimessage"):
                return None
            content = getattr(message, "content", None)

        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return None

        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    @staticmethod
    def _snapshot_delta(content: str, previous: str) -> str:
        if content.startswith(previous):
            return content[len(previous) :]
        if previous.startswith(content):
            return ""
        return content

    def _extract_chunk(self, chunk: Any) -> tuple[Optional[str], Optional[str]]:
        if isinstance(chunk, dict):
            if "output" in chunk:
                return chunk["output"], "text"
            if "text" in chunk:
                return chunk["text"], "text"
            return None, None

        reasoning = None
        if hasattr(chunk, "reasoning_content") and chunk.reasoning_content:
            reasoning = chunk.reasoning_content
        elif hasattr(chunk, "additional_kwargs"):
            reasoning = chunk.additional_kwargs.get("reasoning_content")

        if reasoning:
            return reasoning, "thinking"

        content = chunk.content if hasattr(chunk, "content") else str(chunk)
        return content if content else None, "text"
