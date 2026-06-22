"""LangChain runner with session continuity aware input preparation."""

from __future__ import annotations

import inspect
import logging
import os
import uuid
from typing import Any, AsyncIterator, Dict, Mapping, Optional

from ksadk.runners.base_runner import BaseRunner
from ksadk.runners.utils import (
    get_langfuse_callback,
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

        langfuse_cb = get_langfuse_callback()
        if langfuse_cb:
            config["callbacks"] = [langfuse_cb]

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

        try:
            if hasattr(self._agent, "astream"):
                kwargs = self._build_optional_call_kwargs(
                    self._agent.astream,
                    config=config,
                    context=native_context,
                )
                async for chunk in self._agent.astream(payload, **kwargs):
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
                    delta, chunk_type = self._extract_chunk(chunk)
                    if delta:
                        accumulated_text += delta
                        yield {"delta": delta, "type": chunk_type}
        except Exception as exc:
            print(f"\n⚠️ 流式调用失败: {exc}，回退到同步模式")

        if not accumulated_text:
            result = await self.invoke(input_data)
            yield {"output": result.get("output", ""), "type": "final"}

    def _resolve_request_path(self) -> str:
        module = getattr(self, "_module", None)
        if callable(getattr(module, "ksadk_prepare_input", None)):
            return "standard_hook"
        if self._is_runnable_with_message_history():
            return "runnable_with_message_history"
        return "replay"

    def _prepare_with_standard_hook(self, input_data: Dict[str, Any], session_id: str) -> dict[str, Any]:
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
        prepared = prepare_input(payload, session_context)
        return prepared if isinstance(prepared, dict) else payload

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
        kb_text = str(kb_context.get("formatted_text") or "").strip() if isinstance(kb_context, dict) else ""
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
    def _message_usage(message: Any) -> dict[str, Any]:
        usage_metadata = getattr(message, "usage_metadata", None)
        if isinstance(usage_metadata, Mapping):
            input_token_details = usage_metadata.get("input_token_details")
            output_token_details = usage_metadata.get("output_token_details")
            return {
                "input_tokens": int(usage_metadata.get("input_tokens") or 0),
                "output_tokens": int(usage_metadata.get("output_tokens") or 0),
                "total_tokens": int(
                    usage_metadata.get("total_tokens")
                    or (
                        int(usage_metadata.get("input_tokens") or 0)
                        + int(usage_metadata.get("output_tokens") or 0)
                    )
                ),
                "input_token_details": (
                    dict(input_token_details) if isinstance(input_token_details, Mapping) else {}
                ),
                "output_token_details": (
                    dict(output_token_details) if isinstance(output_token_details, Mapping) else {}
                ),
            }

        response_metadata = getattr(message, "response_metadata", None)
        if isinstance(response_metadata, Mapping):
            token_usage = response_metadata.get("token_usage")
            if isinstance(token_usage, Mapping):
                completion_details = token_usage.get("completion_tokens_details")
                output_token_details = {}
                if isinstance(completion_details, Mapping):
                    reasoning_tokens = completion_details.get("reasoning_tokens")
                    if reasoning_tokens is not None:
                        output_token_details["reasoning"] = int(reasoning_tokens)
                return {
                    "input_tokens": int(token_usage.get("prompt_tokens") or 0),
                    "output_tokens": int(token_usage.get("completion_tokens") or 0),
                    "total_tokens": int(
                        token_usage.get("total_tokens")
                        or (
                            int(token_usage.get("prompt_tokens") or 0)
                            + int(token_usage.get("completion_tokens") or 0)
                        )
                    ),
                    "input_token_details": {},
                    "output_token_details": output_token_details,
                }
        return {}

    @classmethod
    def _extract_usage(cls, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            direct_usage = result.get("usage")
            if isinstance(direct_usage, Mapping):
                return dict(direct_usage)

            messages = result.get("messages")
            if isinstance(messages, list):
                for message in reversed(messages):
                    usage = cls._message_usage(message)
                    if usage:
                        return usage

        usage = cls._message_usage(result)
        if usage:
            return usage
        return {}

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
