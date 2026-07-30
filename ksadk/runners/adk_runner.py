"""
ADKRunner - Google ADK 框架运行时

参考 adk-python 原生实现，缓存 Runner 和 SessionService。
支持通过环境变量配置记忆体 (ShortTermMemory / LongTermMemory)。
"""

import asyncio
import base64
import inspect
import logging
import os
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Mapping, Optional, cast

from opentelemetry import trace

from ksadk.compat.adk_compat import genai_types as types
from ksadk.conversations.attachments import classify_attachment_kind, read_attachment_uri_bytes
from ksadk.conversations.model_context import supports_native_image_input
from ksadk.runners.base_runner import BaseRunner
from ksadk.runners.usage_accumulator import accumulate_usage
from ksadk.sessions.continuity import ADKSessionAdapter

logger = logging.getLogger(__name__)

tracer = trace.get_tracer(__name__)


def _part_metadata_flag(part: Any, key: str) -> bool:
    """读取 A2A→GenAI Part 保留下来的扩展 metadata 标记。"""
    metadata = getattr(part, "part_metadata", None)
    if isinstance(metadata, Mapping):
        return bool(metadata.get(key))
    getter = getattr(metadata, "get", None)
    if callable(getter):
        try:
            return bool(getter(key))
        except (KeyError, TypeError, ValueError):
            return False
    return False


class ADKRunner(BaseRunner):
    """ADK 框架运行时"""

    def __init__(self, detection_result: Any, project_dir: str):
        super().__init__(detection_result, project_dir)
        self._runner: Any = None
        self._session_service: Any = None
        # Map external session_ids (e.g. from run_interactive or web) to ADK internal session IDs
        self._session_map: Dict[str, str] = {}
        # Fallback default session
        self._default_session_id: Optional[str] = None
        # Memory integration
        self._short_term_memory: Any = None
        self._long_term_memory: Any = None
        # Knowledge base integration
        self._knowledge_base: Any = None
        # Keep runtime toolsets alive for the lifetime of the runner.
        self._runtime_toolsets: list[Any] = []
        # ADK resumability state
        self._resumable: bool = False
        self._resume_disabled_reason: Optional[str] = None
        # P1.1 sub-issue: guard invocation_map read-modify-write so concurrent
        # invocations on the same session don't lose each other's mappings.
        self._invocation_map_lock = asyncio.Lock()
        self._module: Any = None
        self._adk_resume_min_version = os.environ.get(
            "GOOGLE_ADK_RESUME_MIN_VERSION", "1.16.0"
        ).strip()

    async def close(self) -> None:
        """Close runtime toolsets owned by this runner."""
        toolsets = list(self._runtime_toolsets)
        self._runtime_toolsets.clear()
        for toolset in toolsets:
            close = getattr(toolset, "aclose", None) or getattr(toolset, "close", None)
            if not callable(close):
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning("Failed to close runtime toolset %r: %s", toolset, exc)

    def _apply_json_patch(self):
        """Patch ADK LiteLlm to handle malformed JSON in tool-call arguments.

        The previous RobustJson approach replaced the entire json module inside
        lite_llm.py, which broke ADK's streaming args-completeness detection:
        ADK uses ``try: json.loads(args) except json.JSONDecodeError: pass``
        to decide whether accumulated streaming args form a complete JSON
        object. When RobustJson.loads swallowed the exception (or even when
        it re-raised after json_repair "fixed" incomplete fragments like
        ``{`` into ``{}``), fallback_index incremented on every args fragment,
        causing a single tool call to fragment into multiple entries with
        empty names -- the "Tool '' not found" error.

        The new approach is surgical: we do NOT replace the json module at
        all. Instead, we patch _message_to_generate_content_response to
        tolerate malformed JSON in the FINAL assembled arguments only.
        The streaming args detection continues using stdlib json.loads and
        gets proper JSONDecodeError for incomplete fragments.
        """
        try:
            import inspect as _inspect
            import json as _stdlib_json

            from ksadk.compat.adk_compat import lite_llm_module

            adk_lite_llm = lite_llm_module()

            _original_fn = adk_lite_llm._message_to_generate_content_response
            if getattr(_original_fn, "__ksadk_json_patch__", False):
                return
            # Determine which kwargs the original function actually accepts,
            # so the patch is compatible across ADK versions that may have
            # added or removed `model_version` / `thought_parts`.
            _orig_params = set(_inspect.signature(_original_fn).parameters.keys())

            def _patched_message_to_generate_content_response(
                message, *, is_partial=False, model_version=None, thought_parts=None
            ):
                """Wrapper that catches JSONDecodeError in final args parsing."""
                # Only forward kwargs that the original function accepts,
                # avoiding TypeError on older ADK versions that lack
                # `model_version` or `thought_parts`.
                forward_kwargs = {"is_partial": is_partial}
                if "model_version" in _orig_params:
                    forward_kwargs["model_version"] = model_version
                if "thought_parts" in _orig_params:
                    forward_kwargs["thought_parts"] = thought_parts
                try:
                    result = _original_fn(message, **forward_kwargs)
                except _stdlib_json.JSONDecodeError:
                    logger.warning(
                        "ADKRunner: caught JSONDecodeError in args parsing, "
                        "returning empty response"
                    )
                    from ksadk.compat.adk_compat import LlmResponse
                    from ksadk.compat.adk_compat import genai_types as _genai_types

                    return LlmResponse(
                        content=_genai_types.Content(role="model", parts=[]),
                        partial=is_partial,
                        model_version=model_version,
                    )
                # If the response has function_call parts with empty args,
                # fill in {} as fallback (this was what RobustJson used to do,
                # but now only at the final output stage, not during streaming).
                # Also strip phantom function_calls whose name is empty or
                # whitespace-only — these are streaming artifacts produced by
                # the old ADK fallback_index mechanism when parallel tool-call
                # fragments are mis-assembled.
                phantom_indices = set()
                if result.content and result.content.parts:
                    for i, part in enumerate(result.content.parts):
                        if part.function_call and part.function_call.args is None:
                            part.function_call.args = {}
                        if part.function_call and not (part.function_call.name or "").strip():
                            phantom_indices.add(i)
                if phantom_indices:
                    result.content.parts = [
                        p for i, p in enumerate(result.content.parts) if i not in phantom_indices
                    ]
                return result

            _patched_message_to_generate_content_response.__ksadk_json_patch__ = True
            adk_lite_llm._message_to_generate_content_response = (
                _patched_message_to_generate_content_response
            )

            # Don't patch _model_response_to_generate_content_response since
            # it calls _message_to_generate_content_response internally, and
            # that's already patched. Double-patching would cause recursion.

            logger.info(
                "ADKRunner: Applied surgical args-parsing patch "
                "(stdlib json preserved, streaming detection intact)"
            )

        except ImportError:
            pass  # ADK not installed
        except Exception:
            pass

    def _apply_mcp_result_patch(self):
        """Patch ADK McpTool to convert CallToolResult to dict.

        Old ADK (1.14.1) McpTool._run_async_impl returns the raw
        CallToolResult Pydantic object from session.call_tool(), which
        cannot be JSON-serialized when ADK builds the FunctionResponse.
        New ADK (1.34.0) added response.model_dump(exclude_none=True,
        mode="json") before returning. This patch replicates that fix
        for the old version.
        """
        try:
            from ksadk.compat.adk_compat import McpTool

            _original_run_async = McpTool._run_async_impl
            if getattr(_original_run_async, "__ksadk_mcp_result_patch__", False):
                return

            async def _patched_run_async_impl(self, *, args, tool_context, credential):
                response = await _original_run_async(
                    self,
                    args=args,
                    tool_context=tool_context,
                    credential=credential,
                )
                # If response is a Pydantic model (e.g. CallToolResult),
                # convert to dict so it can be JSON-serialized downstream.
                if hasattr(response, "model_dump"):
                    return response.model_dump(exclude_none=True, mode="json")
                return response

            _patched_run_async_impl.__ksadk_mcp_result_patch__ = True
            McpTool._run_async_impl = _patched_run_async_impl

            logger.info(
                "ADKRunner: Applied MCP result serialization patch "
                "(CallToolResult -> dict via model_dump)"
            )

        except ImportError:
            pass  # ADK or MCP not installed
        except Exception as exc:
            logger.debug("ADKRunner: MCP result patch failed: %s", exc)

    def _init_short_term_memory(self):
        """从环境变量初始化短期记忆

        环境变量:
            KSADK_ADK_SESSION_BACKEND / PATH / URL: ADK 专用 session 配置
            KSADK_STM_BACKEND / PATH / URL: 旧平台级 STM 配置
            KSADK_SESSION_BACKEND / DSN: 统一 session 配置 fallback
        """
        configured_names = (
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
        if not any(str(os.environ.get(name, "")).strip() for name in configured_names):
            return None

        try:
            from ksadk.memory.adk import ShortTermMemory

            stm = ShortTermMemory.from_env()
            logger.info(
                "ShortTermMemory initialized: backend=%s path=%s",
                stm.backend,
                stm.local_database_path,
            )
            return stm
        except Exception as e:
            logger.warning(f"Failed to init ShortTermMemory: {e}. Using default.")
            return None

    def get_session_adapter(self):
        return ADKSessionAdapter()

    def describe_checkpoint_capability(self) -> dict[str, Any]:
        resumable = getattr(self, "_resumable", False)
        stm_backend = (
            getattr(self._short_term_memory, "backend", None) if self._short_term_memory else None
        )
        if resumable:
            backend = "adk_invocation"
            if stm_backend == "sqlite":
                backend = "adk_invocation+sqlite"
            elif stm_backend == "database":
                backend = "adk_invocation+postgres"
            shared_across_pods = stm_backend == "database"
            return {
                "Supported": shared_across_pods,
                "Backend": backend,
                "Scope": "invocation",
                "Durable": stm_backend is not None and stm_backend != "local",
                "SharedAcrossPods": shared_across_pods,
                "LocalOnly": not shared_across_pods,
                "ResumeMode": "invocation_id",
                "Reason": (
                    "ADK ResumabilityConfig and shared database session backend enabled; "
                    "resume via invocation_id"
                    if shared_across_pods
                    else "ADK ResumabilityConfig enabled, but the session backend is "
                    "process-local or SQLite and cannot support cross-pod recovery"
                ),
            }
        return {
            "Supported": False,
            "Backend": "none",
            "Scope": "unknown",
            "Durable": False,
            "SharedAcrossPods": False,
            "ResumeMode": "forward_only",
            "Reason": (
                self._resume_disabled_reason
                or "ADK ResumabilityConfig not enabled; set "
                "KSADK_ADK_RESUMABLE=1 or configure App with resumability_config"
            ),
        }

    def get_runtime_capabilities(self) -> dict[str, Any]:
        capabilities = super().get_runtime_capabilities()
        resumable = getattr(self, "_resumable", False)
        stm_backend = (
            getattr(self._short_term_memory, "backend", None) if self._short_term_memory else None
        )
        is_durable = stm_backend is not None and stm_backend != "local"
        if resumable:
            # P1.3: Level must degrade with backend — in_memory session state
            # cannot survive pod restarts, so "runtime" is misleading.
            level = "runtime" if is_durable else "semantic"
            capabilities["SessionContinuity"] = {
                "Supported": True,
                "Type": "adk_invocation",
                "Level": level,
                "Reason": (
                    "ADK ResumabilityConfig enabled with durable session backend, "
                    "invocation_id-based checkpoint resume available"
                    if is_durable
                    else "ADK ResumabilityConfig enabled but session state is in-memory; "
                    "resume only works within the same process lifetime"
                ),
            }
        else:
            has_native_session = bool(getattr(self, "_short_term_memory", None)) or any(
                str(os.getenv(name) or "").strip()
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
            capabilities["SessionContinuity"] = {
                "Supported": True,
                "Type": "native_session" if has_native_session else "semantic_replay",
                "Level": "semantic",
                "Reason": (
                    "ADK native session can continue conversation context"
                    if has_native_session
                    else "conversation transcript can be replayed"
                ),
            }
        capabilities["ResumeRun"]["Reason"] = capabilities["Checkpoint"]["Reason"]
        return capabilities

    @dataclass
    class _ResolvabilityResult:
        enabled: bool
        source: str  # "agent_module" | "env" | "auto_persistent_session" | "default"
        app: Any  # User module exported App object (if any)

    def _resolve_resumability(self) -> "_ResolvabilityResult":
        """从环境变量或 agent 模块推断是否启用 ADK 可恢复性。"""
        # Priority 1: agent module exports app object with resumability_config
        module = getattr(self, "_module", None)
        if module is not None:
            try:
                from ksadk.compat.adk_compat import App

                for attr_name in ("app", "application"):
                    candidate = getattr(module, attr_name, None)
                    if isinstance(candidate, App) and candidate.resumability_config:
                        if candidate.resumability_config.is_resumable:
                            return self._ResolvabilityResult(
                                enabled=True, source="agent_module", app=candidate
                            )
            except ImportError:
                pass

        # Priority 2: environment variable explicit control
        env_val = os.environ.get("KSADK_ADK_RESUMABLE", "").strip().lower()
        if env_val in ("1", "true", "yes"):
            return self._ResolvabilityResult(enabled=True, source="env", app=None)

        # Priority 3: persistent session backend auto-enable
        stm_backend = (
            getattr(self._short_term_memory, "backend", None) if self._short_term_memory else None
        )
        if stm_backend and stm_backend != "local":
            return self._ResolvabilityResult(
                enabled=True, source="auto_persistent_session", app=None
            )

        return self._ResolvabilityResult(enabled=False, source="default", app=None)

    @staticmethod
    def _get_adk_version() -> Optional[str]:
        """返回当前安装的 google-adk 版本号，无法获取时返回 None。"""
        try:
            return _pkg_version("google-adk")
        except PackageNotFoundError:
            return None

    def _check_adk_resume_compatibility(self) -> tuple[bool, str]:
        """检查当前 google-adk 版本是否支持恢复 (invocation_id)。

        Returns:
            (compatible, reason) — compatible=True 表示版本足够；reason 为不兼容时的说明文本。
        """
        adk_ver = self._get_adk_version()
        if adk_ver is None:
            # 无法获取版本信息，保守地认为不兼容
            return False, "google-adk version unknown, cannot guarantee invocation_id support"
        try:
            from packaging.version import Version

            if Version(adk_ver) < Version(self._adk_resume_min_version):
                return (
                    False,
                    f"google-adk {adk_ver} < {self._adk_resume_min_version}, "
                    f"run_async() does not accept invocation_id",
                )
        except Exception:
            # packaging 不可用时，保守地认为不兼容
            return False, "cannot compare google-adk version, assuming incompatible"
        return True, ""

    def _build_runner(self) -> None:
        """构造 ADK Runner，优先使用 App 对象以启用 ResumabilityConfig。"""
        from ksadk.compat.adk_compat import Runner

        resumable = self._resolve_resumability()
        resumability_enabled = resumable.enabled

        # 版本兼容性检查：低于最低版本时强制关闭恢复
        resume_compatible, resume_reason = self._check_adk_resume_compatibility()
        if not resume_compatible and resumable.enabled:
            logger.warning("ADKRunner: resumability disabled — %s", resume_reason)
            resumable = self._ResolvabilityResult(enabled=False, source="version_check", app=None)
            resumability_enabled = False
            self._resume_disabled_reason = resume_reason

        if resumable.app is not None:
            runner_kwargs = dict(
                app=resumable.app,
                session_service=self._session_service,
            )
        elif resumable.enabled:
            try:
                from ksadk.compat.adk_compat import App, ResumabilityConfig

                app = App(
                    name=self._agent.name,
                    root_agent=self._agent,
                    resumability_config=ResumabilityConfig(is_resumable=True),
                )
                runner_kwargs = dict(
                    app=app,
                    session_service=self._session_service,
                )
            except ImportError:
                logger.warning(
                    "ADK ResumabilityConfig not available (requires google-adk >= 1.14.0); "
                    "falling back to non-resumable Runner"
                )
                runner_kwargs = dict(
                    agent=self._agent,
                    session_service=self._session_service,
                    app_name=self._agent.name,
                )
                resumability_enabled = False
                self._resume_disabled_reason = "ADK ResumabilityConfig is unavailable"
        else:
            runner_kwargs = dict(
                agent=self._agent,
                session_service=self._session_service,
                app_name=self._agent.name,
            )

        if self._long_term_memory:
            runner_kwargs["memory_service"] = self._long_term_memory
            logger.info("ADKRunner: LongTermMemory injected as memory_service")

        self._runner = Runner(**runner_kwargs)
        self._resumable = resumability_enabled

        if resumability_enabled:
            stm_backend = (
                getattr(self._short_term_memory, "backend", None)
                if self._short_term_memory
                else None
            )
            logger.info(
                "ADKRunner: resumability enabled (source=%s, backend=%s)",
                resumable.source,
                stm_backend or "in_memory",
            )

    def _init_long_term_memory(self):
        """从环境变量初始化长期记忆

        环境变量:
            KSADK_LTM_BACKEND: local / http / sdk
            KSADK_LTM_HTTP_URL: HTTP 记忆服务地址
            KSADK_LTM_HTTP_TOKEN: 认证 Token
            KSADK_LTM_ACCESS_KEY: SDK AK (fallback to KSYUN_ACCESS_KEY)
            KSADK_LTM_SECRET_KEY: SDK SK (fallback to KSYUN_SECRET_KEY)
            KSADK_LTM_TOP_K: 检索数量
        """
        backend = os.environ.get("KSADK_LTM_BACKEND", "")
        if not backend:
            return None

        try:
            from ksadk.memory.adk import LongTermMemory

            agent_name = self._agent.name if self._agent else "default"
            ltm = LongTermMemory.from_env(app_name=agent_name)
            logger.info(f"LongTermMemory initialized: backend={backend}, " f"app_name={agent_name}")
            return ltm
        except Exception as e:
            logger.warning(f"Failed to init LongTermMemory: {e}.")
            return None

    def _init_knowledge_base(self):
        """从环境变量初始化知识库

        环境变量:
            KSADK_KB_DATASET_ID: 知识库 ID (必填，存在即启用)
            KSADK_KB_ACCESS_KEY: AK (可选)
            KSADK_KB_SECRET_KEY: SK (可选)
            KSADK_KB_REGION: 区域 (默认 cn-beijing-6)
            KSADK_KB_TOP_K: 返回结果数 (默认 5)
        """
        try:
            from ksadk.knowledge_base.client import KnowledgeBaseClient

            if not KnowledgeBaseClient.is_configured():
                return None

            kb = KnowledgeBaseClient.from_env()
            logger.info(
                f"KnowledgeBase initialized: dataset_id={kb.dataset_id}, " f"region={kb.region}"
            )
            return kb
        except ImportError:
            logger.warning(
                "kingsoftcloud-sdk-python not installed, "
                "knowledge base disabled. "
                "Install with: pip install kingsoftcloud-sdk-python"
            )
            return None
        except Exception as e:
            logger.warning(f"Failed to init KnowledgeBase: {e}.")
            return None

    def _inject_search_knowledge_tool(self):
        """自动注入 search_knowledge_base 工具到 Agent"""
        try:
            from ksadk.knowledge_base.adk_tool import search_knowledge_base

            added = self._append_tools_by_name([search_knowledge_base])
            if added:
                logger.info(
                    "Injected 'search_knowledge_base' tool into agent "
                    f"(total tools: {len(self._agent.tools)})"
                )
            else:
                logger.debug("Agent already has 'search_knowledge_base' tool")
        except ImportError as e:
            logger.warning(f"Failed to import knowledge base tool: {e}")
        except Exception as e:
            logger.warning(f"Failed to inject search_knowledge_base tool: {e}")

    def _inject_load_memory_tool(self):
        """自动注入 load_memory 工具到 Agent"""
        try:
            from ksadk.compat.adk_compat import load_memory

            added = self._append_tools_by_name([load_memory])
            if added:
                logger.info(
                    "Injected 'load_memory' tool into agent "
                    f"(total tools: {len(self._agent.tools)})"
                )
            else:
                logger.debug("Agent already has 'load_memory' tool")
        except ImportError:
            logger.warning(
                "google.adk.tools.load_memory not available. "
                "Ensure google-adk >= 1.0.0 is installed."
            )
        except Exception as e:
            logger.warning(f"Failed to inject load_memory tool: {e}")

    def _inject_save_memory_tool(self):
        """自动注入 save_memory 工具到 Agent"""
        try:
            from ksadk.memory.adk_tool import create_adk_tool

            save_memory_tool = create_adk_tool()
            added = self._append_tools_by_name([save_memory_tool])
            if added:
                logger.info(
                    "Injected 'save_memory' tool into agent "
                    f"(total tools: {len(self._agent.tools)})"
                )
            else:
                logger.debug("Agent already has 'save_memory' tool")
        except ImportError as e:
            logger.warning(f"Failed to import save_memory tool: {e}")
        except Exception as e:
            logger.warning(f"Failed to inject save_memory tool: {e}")

    def _inject_skill_runtime_tools(self):
        """Inject Skill Runtime tools when skills are configured for sandbox mode."""
        mode = self._resolve_skills_mode()
        if mode == "local":
            self._inject_local_skill_tools()
            return
        if mode != "sandbox":
            return

        try:
            from ksadk.skills.runtime import create_skill_runtime_backend
            from ksadk.skills.tool_defs import (
                build_execute_skills_tool,
                build_skill_manifest_instruction,
                load_remote_skill_manifests,
                resolve_skill_space_ids,
                resolve_user_skill_space_ids,
            )

            skill_space_ids = resolve_skill_space_ids()
            backend = create_skill_runtime_backend()
            execute_skills = build_execute_skills_tool(
                backend=backend,
                skill_space_ids=resolve_user_skill_space_ids(),
                session_id=getattr(self._agent, "name", None) or self.detection_result.name,
            )
            added = self._append_tools_by_name([execute_skills])
            try:
                manifest_instruction = build_skill_manifest_instruction(
                    load_remote_skill_manifests(skill_space_ids)
                )
                if manifest_instruction:
                    self._append_agent_instruction(manifest_instruction)
            except Exception as exc:
                logger.warning("Failed to inject remote Skill manifest: %s", exc)
            if added:
                logger.info("Injected Skill Runtime tools into agent (added: %s)", ", ".join(added))
            else:
                logger.debug("Skill Runtime tools already present")
        except Exception as exc:
            logger.warning("Failed to inject Skill Runtime tools: %s", exc)

    def _append_agent_instruction(self, extra_instruction: str) -> None:
        if not extra_instruction or not hasattr(self._agent, "instruction"):
            return
        current = str(getattr(self._agent, "instruction") or "")
        if extra_instruction in current:
            return
        self._agent.instruction = f"{current.rstrip()}\n{extra_instruction}".strip()

    def _inject_local_skill_tools(self):
        try:
            from ksadk.skills.loader import load_local_skill
            from ksadk.skills.tool_defs import build_skills_tool

            skills_dir = Path(
                os.environ.get("KSADK_LOCAL_SKILLS_DIR")
                or os.environ.get("KSADK_SKILL_CACHE_DIR")
                or Path(self.project_dir) / "skills"
            )
            if not skills_dir.exists():
                logger.info("ADKRunner: local skills directory does not exist: %s", skills_dir)
                return
            skills = [
                load_local_skill(path)
                for path in sorted(skills_dir.iterdir())
                if path.is_dir() and (path / "SKILL.md").exists()
            ]
            if not skills:
                return
            tool = build_skills_tool(skills)
            added = self._append_tools_by_name([tool])
            if added:
                logger.info("Injected local Skill tools into agent (added: %s)", ", ".join(added))
        except Exception as exc:
            logger.warning("Failed to inject local Skill tools: %s", exc)

    def _resolve_skills_mode(self) -> str:
        mode = os.environ.get("KSADK_SKILLS_MODE", "auto").strip().lower()
        if mode != "auto":
            return mode
        runtime_backend = os.environ.get("KSADK_SKILL_RUNTIME_BACKEND")
        if runtime_backend is not None:
            backend = runtime_backend.strip().lower()
            return "sandbox" if backend and backend not in {"disabled", "none", "off"} else "auto"

        backend = (os.environ.get("KSADK_SANDBOX_BACKEND") or "").strip().lower()
        if backend and backend not in {"disabled", "none", "off"}:
            return "sandbox"
        if os.environ.get("KSADK_SANDBOX_TEMPLATE_ID") or os.environ.get(
            "KSADK_SKILL_RUNTIME_TEMPLATE_ID"
        ):
            return "sandbox"
        skills_dir = Path(
            os.environ.get("KSADK_LOCAL_SKILLS_DIR")
            or os.environ.get("KSADK_SKILL_CACHE_DIR")
            or Path(self.project_dir) / "skills"
        )
        if self._has_local_skills(skills_dir):
            return "local"
        return "auto"

    @staticmethod
    def _has_local_skills(skills_dir: Path) -> bool:
        if not skills_dir.exists():
            return False
        return any(path.is_dir() and (path / "SKILL.md").exists() for path in skills_dir.iterdir())

    def _inject_mcp_toolsets(self):
        """默认注入远端 MCP toolset。"""
        try:
            from ksadk.mcp_runtime import (
                MCP_TOOLSET_KEY_ATTR,
                load_mcp_toolsets_from_env,
                mcp_tools_enabled,
                register_mcp_toolset_descriptors,
            )

            if not mcp_tools_enabled():
                logger.info("ADKRunner: MCP tools disabled via KSADK_ENABLE_MCP_TOOLS=0")
                return

            toolsets = load_mcp_toolsets_from_env()
            if not toolsets:
                return

            added = self._append_toolsets_by_key(
                toolsets,
                key_attr=MCP_TOOLSET_KEY_ATTR,
            )
            if not added:
                logger.debug("ADKRunner: MCP toolsets already present")
                return

            for toolset in toolsets:
                key = getattr(toolset, MCP_TOOLSET_KEY_ATTR, None)
                if key in added:
                    self._runtime_toolsets.append(toolset)
                    register_mcp_toolset_descriptors(toolset)
            logger.info("Injected MCP toolsets into agent (added: %s)", ", ".join(added))
        except ImportError as exc:
            logger.warning(f"Failed to import MCP runtime helpers: {exc}")
        except Exception as exc:
            logger.warning(f"Failed to inject MCP toolsets: {exc}")

    def _inject_builtin_tools(self):
        """Inject ksadk built-in tools according to the runtime profile."""
        try:
            from ksadk.toolsets import builtin_tools_for_runtime, builtin_tools_mode

            mode = builtin_tools_mode(default="off")
            if mode == "off":
                return
            tools = builtin_tools_for_runtime(mode=mode)
            if not tools:
                return
            added = self._append_tools_by_name(tools)
            if added:
                logger.info(
                    "Injected ksadk built-in tools into agent (added: %s)",
                    ", ".join(added),
                )
            else:
                logger.debug("ksadk built-in tools already present")
        except Exception as exc:
            logger.warning("Failed to inject ksadk built-in tools: %s", exc)

    def inject_deferred_tools_for_request(
        self, tool_names: list[str] | tuple[str, ...]
    ) -> list[str]:
        """Append direct built-in tools selected by deferred tool search."""
        names = [str(name or "").strip() for name in tool_names or [] if str(name or "").strip()]
        if not names:
            return []
        try:
            from ksadk.toolsets import get_agentengine_tools

            tools = get_agentengine_tools(include=names, profile="coding", mode="direct")
            added = self._append_tools_by_name(tools)
            if added:
                logger.info(
                    "Injected deferred ksadk tools for request (added: %s)",
                    ", ".join(added),
                )
            return added
        except Exception as exc:
            logger.warning("Failed to inject deferred ksadk tools for request: %s", exc)
            return []

    @staticmethod
    def _tool_name(tool: Any) -> str:
        return str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))

    def _append_tools_by_name(self, tools: list[Any]) -> list[str]:
        if not hasattr(self._agent, "tools"):
            logger.warning("Agent has no 'tools' attribute, cannot inject runtime tools")
            return []

        if self._agent.tools is None:
            self._agent.tools = []

        existing_names = {
            self._tool_name(tool) for tool in self._agent.tools if self._tool_name(tool)
        }
        added_names: list[str] = []
        for tool in tools:
            tool_name = self._tool_name(tool)
            if tool_name and tool_name in existing_names:
                continue
            self._agent.tools.append(tool)
            if tool_name:
                existing_names.add(tool_name)
                added_names.append(tool_name)
        return added_names

    def _append_toolsets_by_key(self, toolsets: list[Any], *, key_attr: str) -> list[str]:
        if not hasattr(self._agent, "tools"):
            logger.warning("Agent has no 'tools' attribute, cannot inject MCP toolsets")
            return []

        if self._agent.tools is None:
            self._agent.tools = []

        existing_keys = {
            getattr(tool, key_attr) for tool in self._agent.tools if getattr(tool, key_attr, None)
        }
        added_keys: list[str] = []
        for toolset in toolsets:
            key = getattr(toolset, key_attr, None)
            if key and key in existing_keys:
                continue
            self._agent.tools.append(toolset)
            if key:
                existing_keys.add(key)
                added_keys.append(key)
        return added_keys

    def _invalid_agent_name_load_error(self, exc: Exception) -> ValueError | None:
        """Convert ADK's import-time agent-name validation into an actionable error."""
        errors = getattr(exc, "errors", None)
        if not callable(errors):
            return None
        try:
            details = errors()
        except Exception:
            return None
        if not isinstance(details, list):
            return None
        for detail in details:
            if not isinstance(detail, Mapping):
                continue
            location = detail.get("loc")
            message = str(detail.get("msg") or "")
            name = detail.get("input")
            if location not in (("name",), ["name"]) or not isinstance(name, str):
                continue
            # adk 1.34 报 "...valid identifier";adk 2.x 报 "...valid Python identifier"。
            # 放宽匹配以同时覆盖两种措辞,保证两版都给出 actionable hint。
            msg_lower = message.lower()
            if "identifier" not in msg_lower or "valid" not in msg_lower:
                continue
            safe_name = "".join(
                char if char.isascii() and (char.isalnum() or char == "_") else "_" for char in name
            )
            if not safe_name or safe_name[0].isdigit():
                safe_name = f"agent_{safe_name}"
            return ValueError(
                "ADK Agent 名称不合法: "
                f"{name!r}。Google ADK 要求 `Agent(name=...)` 以英文字母或下划线开头，"
                "且仅包含英文字母、数字和下划线。"
                f"请在 {self.detection_result.entry_point} 中创建 "
                f"{self.detection_result.agent_variable} 时改为，例如 {safe_name!r}。"
            )
        return None

    def load_agent(self) -> None:
        """加载 ADK Agent"""
        import warnings

        warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.main")

        self._apply_json_patch()
        self._apply_mcp_result_patch()

        # 添加项目目录到 Python 路径
        project_path = Path(self.project_dir).resolve()
        if str(project_path) not in sys.path:
            sys.path.insert(0, str(project_path))

        # 确定模块名: 从 entry_point 获取
        # (e.g. "smart_assistant_adk/agent.py" -> "smart_assistant_adk.agent")
        entry_point = self.detection_result.entry_point
        if entry_point.endswith(".py"):
            module_name = entry_point[:-3]  # 移除 .py 后缀
        else:
            module_name = entry_point

        # 转换路径为模块路径 (e.g., "subdir/agent" -> "subdir.agent")
        module_name = module_name.replace("/", ".").replace("\\", ".")

        try:
            module = __import__(module_name, fromlist=[self.detection_result.agent_variable])
            self._module = module
            self._agent = getattr(module, self.detection_result.agent_variable)

            # Inject safety instruction for DeepSeek/LLMs to prevent empty tool names
            if hasattr(self._agent, "instruction"):
                safety_prompt = "\nIMPORTANT: Do NOT output tool calls with empty names."
                if self._agent.instruction:
                    self._agent.instruction += safety_prompt
                else:
                    self._agent.instruction = safety_prompt

        except ImportError as e:
            raise ImportError(f"无法导入模块 {module_name}: {e}")
        except AttributeError:
            raise AttributeError(
                f"模块 {module_name} 中未找到 {self.detection_result.agent_variable}"
            )
        except Exception as exc:
            name_error = self._invalid_agent_name_load_error(exc)
            if name_error is not None:
                raise name_error from exc
            raise

        # 验证是否为 ADK Agent
        if not hasattr(self._agent, "name"):
            raise TypeError("加载的对象不是有效的 ADK Agent")

        # 初始化记忆体 (从环境变量读取配置)
        self._short_term_memory = self._init_short_term_memory()
        self._long_term_memory = self._init_long_term_memory()

        # 初始化知识库 (从环境变量读取配置)
        self._knowledge_base = self._init_knowledge_base()
        if self._knowledge_base:
            self._inject_search_knowledge_tool()

        # 初始化 SessionService
        from ksadk.compat.adk_compat import InMemorySessionService

        if self._short_term_memory:
            self._session_service = self._short_term_memory.session_service
            logger.info("ADKRunner: using ShortTermMemory session service")
        else:
            self._session_service = InMemorySessionService()

        # 如果配置了长期记忆，自动注入 load_memory 工具到 agent
        if self._long_term_memory:
            self._inject_load_memory_tool()
            self._inject_save_memory_tool()

        self._inject_skill_runtime_tools()
        self._inject_builtin_tools()
        self._inject_mcp_toolsets()

        # 初始化 Runner (使用 _build_runner 以支持 ResumabilityConfig)
        self._build_runner()
        self._default_model_name = self.normalize_requested_model(
            os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
        )
        self._default_model_reference = self._discover_model_reference(self._agent)
        self._active_model_name = self._default_model_reference or self._default_model_name

    def _discover_model_reference(self, agent: Any) -> Optional[str]:
        visited: set[int] = set()

        def _visit(node: Any) -> Optional[str]:
            if node is None:
                return None
            node_id = id(node)
            if node_id in visited:
                return None
            visited.add(node_id)

            current_model = getattr(node, "model", None)
            if hasattr(current_model, "model"):
                candidate = str(getattr(current_model, "model", None) or "").strip()
                if candidate:
                    return candidate
            elif isinstance(current_model, str):
                candidate = current_model.strip()
                if candidate:
                    return candidate

            for child in getattr(node, "sub_agents", []) or []:
                discovered = _visit(child)
                if discovered:
                    return discovered
            return None

        return _visit(agent)

    @staticmethod
    def _resolve_model_reference(existing_model: Any, requested_model: str) -> str:
        existing = str(existing_model or "").strip()
        requested = requested_model.strip()
        if "/" in requested:
            return requested
        if "/" in existing:
            provider_prefix = existing.split("/", 1)[0]
            return f"{provider_prefix}/{requested}"
        return requested

    def _apply_model_to_agent_tree(self, agent: Any, requested_model: str) -> None:
        visited: set[int] = set()

        def _visit(node: Any) -> None:
            if node is None:
                return
            node_id = id(node)
            if node_id in visited:
                return
            visited.add(node_id)

            current_model = getattr(node, "model", None)
            if hasattr(current_model, "model"):
                current_reference = getattr(current_model, "model", None)
                next_reference = self._resolve_model_reference(current_reference, requested_model)
                if current_reference != next_reference:
                    setattr(current_model, "model", next_reference)
            elif isinstance(current_model, str):
                next_reference = self._resolve_model_reference(current_model, requested_model)
                if current_model != next_reference:
                    setattr(node, "model", next_reference)

            for child in getattr(node, "sub_agents", []) or []:
                _visit(child)

        _visit(agent)

    def prepare_for_request(self, model: str | None) -> None:
        normalized = self.sync_process_model_env(model)
        if normalized is None:
            default_model_name = getattr(
                self, "_default_model_name", None
            ) or self.normalize_requested_model(
                os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
            )
            if default_model_name:
                self.sync_process_model_env(default_model_name)
            target_reference = getattr(self, "_default_model_reference", None) or default_model_name
            if target_reference and self._agent is not None:
                current_reference = self._discover_model_reference(self._agent)
                if current_reference != target_reference:
                    self._apply_model_to_agent_tree(self._agent, target_reference)
            self._active_model_name = target_reference
            return
        target_reference = (
            self._resolve_model_reference(
                self._discover_model_reference(self._agent)
                or getattr(self, "_default_model_reference", None)
                or normalized,
                normalized,
            )
            if self._agent is not None
            else normalized
        )
        if target_reference == getattr(self, "_active_model_name", None):
            return
        if self._agent is not None:
            self._apply_model_to_agent_tree(self._agent, normalized)
            discovered = self._discover_model_reference(self._agent)
            self._active_model_name = discovered or target_reference
            return
        self._active_model_name = target_reference

    def _prepare_trace_metadata(self, session_id: Optional[str]):
        """准备 Trace 元数据 (Tags, UserID, etc.)"""
        from ksadk.tracing.span_utils import prepare_trace_metadata

        return prepare_trace_metadata(detection_result=getattr(self, "detection_result", None))

    async def _ensure_session(self, external_session_id: Optional[str] = None) -> str:
        """Get or create ADK session ID based on external ID

        When ShortTermMemory is configured, uses its create_session method
        which supports session retrieval (if session_id already exists).
        """
        # Case 1: External ID provided
        if external_session_id:
            if external_session_id in self._session_map:
                return self._session_map[external_session_id]

            # Create new ADK session and map it
            if self._short_term_memory:
                session = await self._short_term_memory.create_session(
                    app_name=self._agent.name,
                    user_id="ksadk_user",
                    session_id=external_session_id,
                )
            else:
                if self._session_service is None:
                    raise RuntimeError("ADK session service is not initialized")
                session = await self._session_service.create_session(
                    app_name=self._agent.name, user_id="ksadk_user"
                )
            self._session_map[external_session_id] = session.id
            return str(session.id)

        # Case 2: No external ID (use default singleton)
        if self._default_session_id is None:
            if self._short_term_memory:
                session = await self._short_term_memory.create_session(
                    app_name=self._agent.name,
                    user_id="ksadk_user",
                )
            else:
                if self._session_service is None:
                    raise RuntimeError("ADK session service is not initialized")
                session = await self._session_service.create_session(
                    app_name=self._agent.name, user_id="ksadk_user"
                )
            self._default_session_id = session.id
        return str(self._default_session_id)

    async def save_session_to_long_term_memory(
        self, session_id: str, user_id: str = "ksadk_user"
    ) -> bool:
        """将指定 session 保存到长期记忆

        Args:
            session_id: ADK 内部 session ID
            user_id: 用户 ID

        Returns:
            是否保存成功
        """
        if not self._long_term_memory:
            logger.warning("LongTermMemory not configured, cannot save session.")
            return False

        try:
            session = await self._session_service.get_session(
                app_name=self._agent.name,
                user_id=user_id,
                session_id=session_id,
            )
            if not session:
                logger.error(f"Session {session_id} not found, cannot save.")
                return False

            await self._long_term_memory.add_session_to_memory(session)
            logger.info(f"Session {session_id} saved to long-term memory.")
            return True
        except Exception as e:
            logger.error(f"Error saving session to long-term memory: {e}")
            return False

    def _build_adk_content(
        self,
        text: str,
        attachments: list[Dict[str, Any]],
        *,
        model_metadata: Dict[str, Any] | None = None,
    ) -> types.Content:
        parts = []
        if text:
            parts.append(types.Part(text=text))
        skipped_images: list[str] = []
        image_input_supported = supports_native_image_input(model_metadata)
        for att in attachments:
            mime_type = att.get("mime_type", "application/octet-stream")
            display_name = att.get("display_name", "")
            if (
                classify_attachment_kind(str(mime_type), str(display_name)) == "image"
                and not image_input_supported
            ):
                skipped_images.append(str(display_name or "未命名图片"))
                continue

            data: Optional[bytes] = None

            inline_data = att.get("data")
            if att.get("transport") == "inline" and inline_data:
                try:
                    data = base64.b64decode(str(inline_data).strip() + "===")
                except Exception as e:
                    att_name = att.get("display_name", "uploaded_file")
                    logger.warning(f"Failed to decode inline attachment {att_name}: {e}")

            if data is None:
                file_uri = att.get("file_uri")
                if file_uri:
                    data = read_attachment_uri_bytes(file_uri)
                    if data is None:
                        logger.warning("Failed to load stored attachment %s", file_uri)

            if data is None:
                file_uri = att.get("file_uri", "")
                if file_uri.startswith("local:"):
                    logger.warning(
                        (
                            "Ignoring direct local attachment reference %s; "
                            "only resolved storage paths are allowed."
                        ),
                        file_uri,
                    )

            if data is not None:
                parts.append(types.Part.from_bytes(data=data, mime_type=mime_type))

        if skipped_images:
            image_list = "、".join(skipped_images)
            parts.append(
                types.Part(
                    text=(
                        "系统提示：当前模型不支持图片输入，"
                        f"无法直接分析图片附件（{image_list}）。"
                        "请切换到支持视觉的模型后重试。"
                    )
                )
            )

        # If no parts were found at all (e.g. empty message), fallback to prevent crash
        if not parts:
            parts.append(types.Part(text="[empty message]"))

        return types.Content(role="user", parts=parts)

    def _build_state_delta(self, input_data: Dict[str, Any]) -> dict[str, Any]:
        state_delta: dict[str, Any] = {}
        for key in (
            "input_parts",
            "attachments",
            "attachment_results",
            "current_attachments",
            "current_attachment_results",
            "has_current_files",
            "metadata",
        ):
            if key in input_data:
                state_delta[key] = input_data.get(key)
        return state_delta

    @staticmethod
    def _normalize_usage_metadata(usage_metadata: Any) -> dict[str, Any]:
        if usage_metadata is None:
            return {}
        if hasattr(usage_metadata, "model_dump"):
            try:
                usage_metadata = usage_metadata.model_dump(exclude_none=True)
            except Exception:
                usage_metadata = None
        elif hasattr(usage_metadata, "dict"):
            try:
                usage_metadata = usage_metadata.dict()
            except Exception:
                usage_metadata = None
        if not isinstance(usage_metadata, Mapping):
            return {}

        reasoning_tokens = usage_metadata.get("thoughts_token_count")
        output_token_details = {}
        if reasoning_tokens is not None:
            try:
                output_token_details["reasoning"] = int(reasoning_tokens)
            except (TypeError, ValueError):
                pass

        input_token_details: dict[str, Any] = {}
        cached_tokens = usage_metadata.get("cached_content_token_count")
        if cached_tokens is not None:
            try:
                input_token_details["cached"] = int(cached_tokens)
            except (TypeError, ValueError):
                pass
        tool_use_tokens = usage_metadata.get("tool_use_prompt_token_count")
        if tool_use_tokens is not None:
            try:
                input_token_details["tool_use"] = int(tool_use_tokens)
            except (TypeError, ValueError):
                pass

        if "input_tokens" in usage_metadata or "output_tokens" in usage_metadata:
            input_tokens = int(usage_metadata.get("input_tokens") or 0)
            output_tokens = int(usage_metadata.get("output_tokens") or 0)
            total_tokens = int(usage_metadata.get("total_tokens") or (input_tokens + output_tokens))
            direct_input_details = usage_metadata.get("input_token_details")
            if isinstance(direct_input_details, Mapping):
                input_token_details.update(dict(direct_input_details))
            normalized = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "input_token_details": input_token_details,
                "output_token_details": output_token_details,
            }
            direct_output_details = usage_metadata.get("output_token_details")
            if isinstance(direct_output_details, Mapping):
                normalized["output_token_details"] = dict(direct_output_details)
            return normalized

        input_tokens = int(usage_metadata.get("prompt_token_count") or 0)
        output_tokens = int(usage_metadata.get("candidates_token_count") or 0)
        total_tokens = int(
            usage_metadata.get("total_token_count") or (input_tokens + output_tokens)
        )
        if not (
            input_tokens
            or output_tokens
            or total_tokens
            or input_token_details
            or output_token_details
        ):
            return {}
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "input_token_details": input_token_details,
            "output_token_details": output_token_details,
        }

    @classmethod
    def _extract_event_usage(cls, event: Any) -> dict[str, Any]:
        return cls._normalize_usage_metadata(getattr(event, "usage_metadata", None))

    # --- ADK invocation_id & checkpoint helpers ---

    async def _get_max_checkpoint_seq_for_run(
        self,
        *,
        session_id: str,
        run_id: str,
    ) -> int:
        """扫描已有 run_checkpoint 事件，返回同一 run_id 下的最大 checkpoint_seq。

        恢复模式下 checkpoint_seq 从已有最大值继续递增，避免从 0 重启导致
        checkpoint_id 碰撞并被 append_run_checkpoint_event 的去重逻辑静默丢弃。
        """
        if not run_id:
            return 0
        try:
            from ksadk.sessions import resolve_session_service

            service = resolve_session_service()
            events = await service.get_events(session_id)
            max_seq = 0
            for event in reversed(events):
                if event.event_type != "run_checkpoint":
                    continue
                metadata = event.metadata or {}
                if str(metadata.get("run_id") or "") != str(run_id):
                    continue
                if str(metadata.get("framework") or "") != "adk":
                    continue
                checkpoint_id = str(metadata.get("checkpoint_id") or "")
                if checkpoint_id.startswith("adk-ckpt-"):
                    try:
                        seq = int(checkpoint_id.removeprefix("adk-ckpt-"))
                        if seq > max_seq:
                            max_seq = seq
                    except ValueError:
                        pass
            return max_seq
        except Exception as exc:
            logger.warning(
                "ADKRunner: failed to query max checkpoint_seq " "for run_id=%s: %s",
                run_id,
                exc,
            )
            return 0

    async def _collect_adk_invocation_id(
        self,
        events_async,
        *,
        ksadk_invocation_id: str,
        session_id: str,
        checkpoint_run_id: str = "",
    ):
        """包装 event 迭代器，在首个 event 到达时采集 ADK invocation_id 并存储映射，
        同时在可恢复边界写入 checkpoint 事件。

        checkpoint_run_id 用于 checkpoint 的 run_id 字段，恢复模式下沿用原始 RunId
        以保证同一长任务的 checkpoint 时间线连贯；ksadk_invocation_id 用于
        invocation_id 映射和 event 级 invocation_id 字段。
        """
        effective_run_id = checkpoint_run_id or ksadk_invocation_id
        first_event_captured = False
        captured_adk_invocation_id = ""
        # 从已有 checkpoint 的最大 seq 继续，避免恢复模式下 ID 碰撞被去重丢弃
        checkpoint_seq = await self._get_max_checkpoint_seq_for_run(
            session_id=session_id,
            run_id=effective_run_id,
        )
        async for event in events_async:
            if not first_event_captured and hasattr(event, "invocation_id") and event.invocation_id:
                first_event_captured = True
                # P1.1: Use local variable instead of self._last_adk_invocation_id
                # to avoid cross-session corruption under concurrent runner access.
                captured_adk_invocation_id = event.invocation_id
                await self._persist_invocation_mapping(
                    session_id=session_id,
                    ksadk_invocation_id=ksadk_invocation_id,
                    adk_invocation_id=captured_adk_invocation_id,
                )
            # 每个 resumable boundary 都立即写 checkpoint，用递增 seq 保证
            # 即使程序崩溃也有最新恢复点。
            if self._resumable and first_event_captured and self._is_resumable_boundary(event):
                checkpoint_seq += 1
                await self._maybe_write_checkpoint(
                    event=event,
                    session_id=session_id,
                    ksadk_invocation_id=ksadk_invocation_id,
                    adk_invocation_id=captured_adk_invocation_id,
                    checkpoint_seq=checkpoint_seq,
                    checkpoint_run_id=effective_run_id,
                )
            yield event

    async def _collect_adk_invocation_id_if_present(
        self,
        events_async,
        *,
        session_id: str,
        ksadk_invocation_id: str,
    ):
        """轻量版 invocation_id 采集：仅捕获 ID 并持久化映射，不写 checkpoint。"""
        first_event_captured = False
        async for event in events_async:
            if not first_event_captured and hasattr(event, "invocation_id") and event.invocation_id:
                first_event_captured = True
                # P1.1: Local variable — self._last_adk_invocation_id was removed
                # to prevent cross-session corruption under concurrent runner access.
                local_adk_invocation_id = event.invocation_id
                if ksadk_invocation_id:
                    await self._persist_invocation_mapping(
                        session_id=session_id,
                        ksadk_invocation_id=ksadk_invocation_id,
                        adk_invocation_id=local_adk_invocation_id,
                    )
            yield event

    async def _persist_invocation_mapping(
        self,
        *,
        session_id: str,
        ksadk_invocation_id: str,
        adk_invocation_id: str,
    ) -> None:
        """将 ksadk invocation_id → ADK invocation_id 映射持久化到 session binding state。

        存储位置：ksadk_states 表，scope = "runner_binding:adk"
        state_json 内新增字段 invocation_map: { ksadk_inv_id: adk_inv_id, ... }
        无需新增数据库表或字段，复用现有 state_json 的 JSON 存储。
        """
        try:
            from ksadk.sessions import resolve_session_service
            from ksadk.sessions.continuity import ConversationSessionCore

            service = resolve_session_service()
            core = ConversationSessionCore(service)
            # P1.1 sub-issue: hold the lock across the entire read-modify-write
            # so concurrent invocations on the same session can't lose mappings.
            async with self._invocation_map_lock:
                binding = await core.get_binding_by_session_id(session_id, "adk")
                invocation_map = dict(binding.get("invocation_map") or {})
                invocation_map[ksadk_invocation_id] = adk_invocation_id

                await core.set_binding_by_session_id(
                    session_id,
                    "adk",
                    {
                        "external_session_id": str(session_id),
                        "internal_session_id": str(session_id),
                        "invocation_map": invocation_map,
                    },
                )
        except Exception as exc:
            logger.warning("Failed to persist ADK invocation mapping: %s", exc)

    async def _resolve_adk_invocation_id(
        self,
        *,
        session_id: str,
        ksadk_invocation_id: str,
    ) -> str | None:
        """从 session binding state 读取 ADK invocation_id 映射。"""
        try:
            from ksadk.sessions import resolve_session_service
            from ksadk.sessions.continuity import ConversationSessionCore

            service = resolve_session_service()
            core = ConversationSessionCore(service)
            binding = await core.get_binding_by_session_id(session_id, "adk")
            invocation_map = dict(binding.get("invocation_map") or {})
            return invocation_map.get(ksadk_invocation_id)
        except Exception:
            return None

    def _is_resumable_boundary(self, event: Any) -> bool:
        """判断 ADK event 是否为可恢复边界。"""
        # 工具调用请求
        if hasattr(event, "get_function_calls") and event.get_function_calls():
            return True
        # 自定义 Agent 状态保存
        if hasattr(event, "actions") and event.actions:
            if getattr(event.actions, "agent_state", None) is not None:
                return True
            if getattr(event.actions, "end_of_agent", False):
                return True
        return False

    def _extract_approval_signals(self, event: Any) -> list[dict[str, Any]]:
        """从 ADK event 提取原生审批信号,翻译成统一 interrupt_info(供 runtime:4645 消费)。

        覆盖 ADK 两套原生 HITL 机制,经 ``adk_compat`` 能力探测,兼容 1.34.x → 2.x:

        1. **tool-confirmation**(``actions.requested_tool_confirmations``):工具执行前
           yes/no 审批,1.34+ 均有。→ ``kind="tool"``。
        2. **workflow HITL**(v2.0+):``event.interrupted=True`` + ``RequestInput`` 信号
           (message/payload/response_schema)。→ ``kind="input"``。

        命中任一即返回 ``[{"approval_request_id","tool_name","args","kind","message"}, ...]``,
        否则返回 ``[]``。不引入对 v2.0 API 的硬依赖——字段缺失时跳过,不报错。
        """
        signals: list[dict[str, Any]] = []
        actions = getattr(event, "actions", None)

        # 1) tool-confirmation:requested_tool_confirmations(1.34+,经 getattr 容错)
        if actions is not None:
            confirmations = getattr(actions, "requested_tool_confirmations", None)
            if confirmations:
                for conf in confirmations:
                    if conf is None:
                        continue
                    conf_id = str(
                        getattr(conf, "id", "") or getattr(conf, "approval_request_id", "") or ""
                    )
                    if not conf_id:
                        continue
                    signals.append(
                        {
                            "approval_request_id": conf_id,
                            "tool_name": str(
                                getattr(conf, "tool_name", "")
                                or getattr(conf, "name", "")
                                or "tool"
                            ),
                            "args": dict(getattr(conf, "args", None) or {}),
                            "kind": "tool",
                            "message": str(
                                getattr(conf, "message", "") or getattr(conf, "hint", "") or ""
                            ),
                        }
                    )

        # 2) workflow HITL(v2.0+):event.interrupted + RequestInput 信号
        #    仅当 adk_compat 探测到 v2.0+ 时检查,避免在 1.34 上访问不存在字段。
        try:
            from ksadk.compat.adk_compat import adk_version_at_least

            if adk_version_at_least("2.0.0") and getattr(event, "interrupted", False):
                # RequestInput 信号可能挂在 actions 或 event 上(字段名跨小版本未冻结,
                # 用 getattr 容错,缺失即视为纯 interrupt 无 payload)。
                req_input = None
                if actions is not None:
                    req_input = getattr(actions, "requested_input", None) or getattr(
                        actions, "request_input", None
                    )
                if req_input is None:
                    req_input = getattr(event, "requested_input", None)
                interrupt_id = "adk-hitl-" + str(getattr(event, "invocation_id", "") or "unknown")
                signals.append(
                    {
                        "approval_request_id": interrupt_id,
                        "tool_name": "RequestInput",
                        "args": dict(getattr(req_input, "payload", None) or {}),
                        "kind": "input",
                        "message": str(
                            getattr(req_input, "message", "") or "ADK workflow 请求人工输入"
                        ),
                    }
                )
        except Exception:
            # 能力探测失败时不阻塞主流(审批 surface 是 best-effort,不降级为报错)。
            pass

        return signals

    async def _maybe_write_checkpoint(
        self,
        *,
        event: Any,
        session_id: str,
        ksadk_invocation_id: str,
        adk_invocation_id: str,
        checkpoint_seq: int,
        checkpoint_run_id: str = "",
    ) -> None:
        """Write a run_checkpoint event at a resumable boundary.

        Each boundary gets its own incrementing checkpoint_id so the latest
        checkpoint always reflects the latest state for crash recovery.
        """
        if not self._is_resumable_boundary(event):
            return

        from ksadk.conversations.runtime import append_run_checkpoint_event

        metadata = self._extract_checkpoint_metadata(event)

        effective_run_id = checkpoint_run_id or ksadk_invocation_id
        await append_run_checkpoint_event(
            session_id=session_id,
            author=self._agent.name,
            run_id=effective_run_id,
            checkpoint_id=f"adk-ckpt-{checkpoint_seq}",
            framework="adk",
            framework_ref={
                "adk": {
                    "invocation_id": adk_invocation_id,
                    "checkpoint_seq": checkpoint_seq,
                    "event_id": getattr(event, "id", ""),
                    "author": getattr(event, "author", ""),
                }
            },
            phase=(
                "tool_call"
                if (hasattr(event, "get_function_calls") and event.get_function_calls())
                else "agent_state"
            ),
            invocation_id=ksadk_invocation_id,
            metadata=metadata,
        )
        logger.debug(
            "ADKRunner: wrote checkpoint adk-ckpt-%d at boundary " "(session=%s, invocation_id=%s)",
            checkpoint_seq,
            session_id,
            adk_invocation_id,
        )

    def _extract_checkpoint_metadata(self, event: Any) -> dict[str, Any]:
        """从 ADK event 中提取 checkpoint 元数据。"""
        metadata: dict[str, Any] = {}

        # 工具调用信息
        if hasattr(event, "get_function_calls") and event.get_function_calls():
            fcs = event.get_function_calls()
            metadata["tool_names"] = [fc.name for fc in fcs if hasattr(fc, "name")]
            metadata["tool_call_ids"] = [fc.id for fc in fcs if hasattr(fc, "id")]

        # Agent 状态信息
        if hasattr(event, "actions") and event.actions:
            agent_state = getattr(event.actions, "agent_state", None)
            if agent_state is not None:
                if isinstance(agent_state, dict):
                    metadata["agent_state_keys"] = list(agent_state.keys())
                else:
                    metadata["agent_state_keys"] = []
            if getattr(event.actions, "end_of_agent", False):
                metadata["is_terminal"] = True
                metadata["end_of_agent"] = True

        # 是否可恢复
        stm_backend = (
            getattr(self._short_term_memory, "backend", None) if self._short_term_memory else None
        )
        shared_across_pods = stm_backend == "database"
        platform_resumable = self._resumable and shared_across_pods
        metadata["is_resumable"] = platform_resumable
        metadata["resume_status"] = "resumable" if platform_resumable else "disabled"
        metadata["backend"] = stm_backend or "in_memory"
        metadata["scope"] = "invocation"
        metadata["durable"] = stm_backend is not None and stm_backend != "local"
        metadata["shared_across_pods"] = shared_across_pods
        if not platform_resumable:
            metadata["resume_disabled_reason"] = (
                self._resume_disabled_reason
                if not self._resumable and self._resume_disabled_reason
                else "ADK checkpoint uses an in-memory or local-only session backend; "
                "cross-pod resume is unavailable"
            )
        # P1.4: ADK only supports invocation-level (forward-only) resume, not
        # arbitrary checkpoint rollback like LangGraph time_travel. Consumers
        # should treat only the latest checkpoint as independently resumable.
        metadata["resume_mode"] = "invocation_id"
        metadata["only_latest_resumable"] = True

        return metadata

    async def _resolve_resume_invocation_id(
        self,
        *,
        input_data: Dict[str, Any],
        session_id: str,
        ksadk_invocation_id: str,
    ) -> str:
        """Resolve the ADK invocation_id needed for resume.

        Looks up from framework_ref or session binding mapping.
        Raises ValueError when the resume reference is missing (P1.2:
        never silently downgrade to a new invocation).
        """
        adk_invocation_id = None
        framework_ref = input_data.get("framework_ref") or {}
        if isinstance(framework_ref, dict):
            adk_ref = framework_ref.get("adk") or {}
            if isinstance(adk_ref, dict):
                adk_invocation_id = adk_ref.get("invocation_id")
        # Fallback: resolve from session binding
        if not adk_invocation_id and ksadk_invocation_id:
            adk_invocation_id = await self._resolve_adk_invocation_id(
                session_id=session_id,
                ksadk_invocation_id=ksadk_invocation_id,
            )
        if not adk_invocation_id:
            logger.error(
                "Resume requested but ADK invocation_id not found for " "session=%s ksadk_inv=%s",
                session_id,
                ksadk_invocation_id,
            )
            raise ValueError(
                f"checkpoint_not_resumable: ADK invocation_id not found for "
                f"session={session_id}, ksadk_invocation_id={ksadk_invocation_id}. "
                f"The checkpoint data may have been lost or the invocation was "
                f"never persisted."
            )
        return str(adk_invocation_id)

    async def _prepare_run_events(
        self,
        *,
        input_data: Dict[str, Any],
        session_id: str,
        user_input: str,
        is_resume: bool,
        run_config: Optional[Any] = None,
    ) -> AsyncIterator[Any]:
        """Prepare the wrapped event stream shared by invoke() and stream().

        Handles new_message construction, resume invocation_id resolution,
        run_async (with optional run_config for streaming), and event wrapping
        for checkpoint writing. Returns the wrapped async iterator.
        """
        if not is_resume:
            new_message = self._build_adk_content(
                user_input,
                input_data.get("attachments", []),
                model_metadata=input_data.get("model_metadata"),
            )
        else:
            new_message = None

        ksadk_invocation_id = str(input_data.get("invocation_id") or input_data.get("run_id") or "")

        checkpoint_run_id = (
            str(input_data.get("run_id") or "").strip()
            if is_resume and str(input_data.get("run_id") or "").strip()
            else ""
        )

        run_kwargs: Dict[str, Any] = {
            "session_id": session_id,
            "user_id": "ksadk_user",
        }
        if run_config is not None:
            run_kwargs["run_config"] = run_config

        if is_resume:
            if not self._resumable:
                raise ValueError(
                    "checkpoint_not_resumable: ADK resumability is disabled for "
                    f"session={session_id}, ksadk_invocation_id={ksadk_invocation_id}."
                )
            adk_invocation_id = await self._resolve_resume_invocation_id(
                input_data=input_data,
                session_id=session_id,
                ksadk_invocation_id=ksadk_invocation_id,
            )
            logger.info("Resuming ADK run with adk_invocation_id: %s", adk_invocation_id)
            run_kwargs["invocation_id"] = adk_invocation_id
        else:
            run_kwargs["new_message"] = new_message
            run_kwargs["state_delta"] = self._build_state_delta(input_data) or None

        if self._runner is None:
            raise RuntimeError("ADK runner is not initialized")
        events_async = self._runner.run_async(**run_kwargs)

        if ksadk_invocation_id and self._resumable:
            wrapped_async = self._collect_adk_invocation_id(
                events_async,
                ksadk_invocation_id=ksadk_invocation_id,
                session_id=session_id,
                checkpoint_run_id=checkpoint_run_id,
            )
        else:
            wrapped_async = self._collect_adk_invocation_id_if_present(
                events_async,
                session_id=session_id,
                ksadk_invocation_id=ksadk_invocation_id,
            )
        return cast(AsyncIterator[Any], wrapped_async)

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """调用 ADK Agent"""

        # 判断是否为恢复调用 — 提前判断以避免将 resume_input dict 当作 string 处理
        is_resume = bool(input_data.get("checkpoint_resume"))
        raw_input = input_data.get("input")
        if is_resume and isinstance(raw_input, dict):
            user_input = "[checkpoint resume]"
        else:
            user_input = str(raw_input or "")
        instructions = str(input_data.get("instructions") or "").strip()
        if instructions and not is_resume:
            user_input = f"{instructions}\n\nCurrent user input:\n{user_input or '[empty message]'}"

        # 1. 准备 Metadata (提前以此获取 Agent Name)
        _, _, _, agent_name = self._prepare_trace_metadata(None)
        trace_name = agent_name or "adk.invoke"

        with tracer.start_as_current_span(trace_name) as span:
            # Set input.value for Langfuse top-level input display
            span.set_attribute("input.value", user_input)
            truncated_input = (
                user_input[:200] if isinstance(user_input, str) else str(user_input)[:200]
            )
            span.set_attribute("user.input", truncated_input)

            # Use external session ID if provided
            req_session_id = input_data.get("session_id")
            session_id = await self._ensure_session(req_session_id)

            # 准备 Metadata 并设置 Span Attributes
            # Langfuse Exporter 会读取这些 span attributes
            agent_user_id, tags, _, _ = self._prepare_trace_metadata(session_id)

            span.set_attribute("langfuse.session_id", session_id)
            if agent_user_id:
                span.set_attribute("langfuse.user_id", agent_user_id)
            if tags:
                span.set_attribute("langfuse.tags", ",".join(tags))

            wrapped_async = await self._prepare_run_events(
                input_data=input_data,
                session_id=session_id,
                user_input=user_input,
                is_resume=is_resume,
            )

            final_response = ""

            events_list = []
            # P1.4: Track last_event to avoid UnboundLocalError when ADK returns
            # zero events (e.g. resuming a completed invocation).
            last_event = None
            usage: dict[str, Any] = {}
            last_usage: dict[str, Any] = {}
            async for event in wrapped_async:
                events_list.append(event)
                last_event = event
                event_usage = self._extract_event_usage(event)
                if event_usage:
                    usage = accumulate_usage(usage, event_usage)
                    last_usage = event_usage  # 保留最后一个非空(窗口占用=最后一次 input)
                if hasattr(event, "content") and event.content:
                    if hasattr(event.content, "parts"):
                        for part in event.content.parts:
                            # 过滤掉思考内容 (thought=True)，只保留最终答案
                            is_thought = getattr(part, "thought", False)
                            if hasattr(part, "text") and part.text and not is_thought:
                                final_response += part.text

            # Prefer the last event's text (usually the complete answer).
            # When the loop aborts early (MCP error, phantom tool-call),
            # final_response may hold only intermediate fragments.
            last_event_text = ""
            if last_event is not None and hasattr(last_event, "content") and last_event.content:
                for part in last_event.content.parts or []:
                    is_thought = getattr(part, "thought", False)
                    if hasattr(part, "text") and part.text and not is_thought:
                        last_event_text += part.text
            if last_event_text:
                final_response = last_event_text

            if not final_response and events_list:
                logger.warning(
                    "ADK invoke finished with events but no final text — "
                    "likely mid-loop abort or tool-call error"
                )

            # Set output.value for Langfuse top-level output display
            span.set_attribute("output.value", final_response[:5000] if final_response else "")
            span.set_attribute("agent.output", final_response[:500] if final_response else "")
            result: dict[str, Any] = {
                "output": final_response,
                "events": events_list,
            }
            if usage:
                result["usage"] = usage
                # last_usage = 最后一次 LLM 调用快照(input_tokens = 当前上下文窗口占用)
                result.setdefault("metadata", {})["last_usage"] = last_usage
            return result

    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 ADK Agent

        使用 StreamingMode.SSE 启用真正的流式 token 输出
        """
        from ksadk.compat.adk_compat import RunConfig, StreamingMode

        # 判断是否为恢复调用 — 提前判断以避免将 resume_input dict 当作 string 处理
        is_resume = bool(input_data.get("checkpoint_resume"))
        raw_input = input_data.get("input")
        if is_resume and isinstance(raw_input, dict):
            user_input = "[checkpoint resume]"
        else:
            user_input = str(raw_input or "")
        instructions = str(input_data.get("instructions") or "").strip()
        if instructions and not is_resume:
            user_input = f"{instructions}\n\nCurrent user input:\n{user_input or '[empty message]'}"

        # 1. 准备 Metadata (提前以此获取 Agent Name)
        _, _, _, agent_name = self._prepare_trace_metadata(None)
        trace_name = agent_name or "adk.stream"

        with tracer.start_as_current_span(trace_name) as span:
            # Set input.value for Langfuse top-level input display
            span.set_attribute("input.value", user_input)
            truncated_input = (
                user_input[:200] if isinstance(user_input, str) else str(user_input)[:200]
            )
            span.set_attribute("user.input", truncated_input)

            # Use external session ID if provided
            req_session_id = input_data.get("session_id")
            session_id = await self._ensure_session(req_session_id)

            # 准备 Metadata 并设置 Span Attributes
            agent_user_id, tags, _, _ = self._prepare_trace_metadata(session_id)

            span.set_attribute("langfuse.session_id", session_id)
            if agent_user_id:
                span.set_attribute("langfuse.user_id", agent_user_id)
            if tags:
                span.set_attribute("langfuse.tags", ",".join(tags))

            run_config = RunConfig(streaming_mode=StreamingMode.SSE)
            wrapped_async = await self._prepare_run_events(
                input_data=input_data,
                session_id=session_id,
                user_input=user_input,
                is_resume=is_resume,
                run_config=run_config,
            )

            accumulated_text = ""
            usage: dict[str, Any] = {}
            last_usage: dict[str, Any] = {}
            # handoff/sub-agent(如 RemoteA2aAgent)回复事件 partial=None,不进上面
            # partial 分支;且远端 agent 经 A2A 流式返回时,ADK 会产出多个"累积快照"
            # 事件(后一个含前一个内容)。按 author 记录已输出快照,只补发增量去重。
            sub_agent_snapshots: dict[str, str] = {}
            sub_agent_thought_snapshots: dict[str, str] = {}
            sub_agent_last_output_kind: dict[str, str] = {}
            top_agent_name = getattr(self._agent, "name", None)

            async for event in wrapped_async:
                event_usage = self._extract_event_usage(event)
                if event_usage:
                    usage = accumulate_usage(usage, event_usage)
                    last_usage = event_usage  # 保留最后一个非空
                # Only yield text delta if event is partial to avoid duplication of final summary
                if hasattr(event, "content") and event.content and getattr(event, "partial", False):
                    if hasattr(event.content, "parts"):
                        author = getattr(event, "author", None)
                        is_sub_agent = bool(author and top_agent_name and author != top_agent_name)
                        author_key = str(author)
                        for part in event.content.parts:
                            if hasattr(part, "text") and part.text:
                                is_thought = getattr(part, "thought", False)
                                replace_snapshot = bool(
                                    is_sub_agent
                                    and _part_metadata_flag(part, "ksadk_output_snapshot")
                                )
                                output_delta = part.text
                                # 思考内容只作为 thinking delta 流出,不计入最终输出,
                                # 否则最终回复会把思考过程再重复一遍(与 invoke() 语义对齐)。
                                if is_thought and is_sub_agent:
                                    if sub_agent_last_output_kind.get(author_key) == "text":
                                        # 正文之后的 thought 是一个新的 reasoning segment。
                                        sub_agent_thought_snapshots[author_key] = ""
                                    previous_thought = sub_agent_thought_snapshots.get(
                                        author_key, ""
                                    )
                                    if part.text.startswith(previous_thought):
                                        output_delta = part.text[len(previous_thought) :]
                                        sub_agent_thought_snapshots[author_key] = part.text
                                    else:
                                        sub_agent_thought_snapshots[author_key] = (
                                            previous_thought + part.text
                                        )
                                elif not is_thought:
                                    accumulated_text = (
                                        part.text
                                        if replace_snapshot
                                        else accumulated_text + part.text
                                    )
                                    # sub-agent(RemoteA2aAgent)增量也累积进快照,供后续
                                    # completed 全文消息(同一份结果)在 handoff 分支去重。
                                    if is_sub_agent:
                                        sub_agent_snapshots[author_key] = (
                                            part.text
                                            if replace_snapshot
                                            else sub_agent_snapshots.get(author_key, "") + part.text
                                        )
                                # 标记思考内容，前端可以选择是否展示
                                if output_delta:
                                    output_chunk: dict[str, Any] = {
                                        "delta": output_delta,
                                        "type": "thinking" if is_thought else "text",
                                    }
                                    if replace_snapshot and not is_thought:
                                        output_chunk["replace"] = True
                                    yield output_chunk
                                if is_sub_agent:
                                    sub_agent_last_output_kind[author_key] = (
                                        "thinking" if is_thought else "text"
                                    )
                # handoff/sub-agent 回复:partial 为 None/False,上面分支跳过,这里补上。
                elif hasattr(event, "content") and event.content:
                    author = getattr(event, "author", None)
                    if (
                        author
                        and top_agent_name
                        and author != top_agent_name
                        and hasattr(event.content, "parts")
                    ):
                        author_key = str(author)
                        for part in event.content.parts:
                            if (
                                hasattr(part, "text")
                                and part.text
                                and getattr(part, "thought", False)
                            ):
                                # A2A's terminal artifact is converted by ADK to
                                # partial=False.  Once that sub-agent has already
                                # emitted response text, a trailing thought here is
                                # a terminal snapshot/replacement (not a new
                                # interleaved reasoning step).  Showing it makes
                                # the UI appear to end at "thinking" and hides the
                                # just-emitted final response.
                                if sub_agent_last_output_kind.get(author_key) == "text":
                                    continue
                                # RemoteA2aAgent 将 last_chunk=True 映射成
                                # partial=False；这可能是一个此前 partial thought
                                # 的终态快照。只补发新增内容，避免正文之后再显示一遍
                                # 相同的思考块；若此前没有 partial thought，仍完整透传。
                                previous_thought = sub_agent_thought_snapshots.get(
                                    author_key, ""
                                )
                                if part.text.startswith(previous_thought):
                                    thought_delta = part.text[len(previous_thought) :]
                                    sub_agent_thought_snapshots[author_key] = part.text
                                elif previous_thought.startswith(part.text):
                                    thought_delta = ""
                                else:
                                    thought_delta = part.text
                                    sub_agent_thought_snapshots[author_key] = (
                                        previous_thought + part.text
                                    )
                                if thought_delta:
                                    yield {"delta": thought_delta, "type": "thinking"}
                        snapshot = ""
                        replace_snapshot = False
                        for part in event.content.parts:
                            if hasattr(part, "text") and part.text and not getattr(
                                part, "thought", False
                            ):
                                snapshot += part.text
                                replace_snapshot = replace_snapshot or _part_metadata_flag(
                                    part,
                                    "ksadk_output_snapshot",
                                )
                        if snapshot:
                            prev = sub_agent_snapshots.get(author_key, "")
                            if replace_snapshot:
                                delta = snapshot
                                sub_agent_snapshots[author_key] = snapshot
                                accumulated_text = snapshot
                            elif snapshot.startswith(prev):
                                # 累计快照(如 completed 全文):只补发超出已累积的部分;
                                # 若增量已发全,delta 为空,避免 completed 再渲染一遍。
                                delta = snapshot[len(prev) :]
                                sub_agent_snapshots[author_key] = snapshot
                            else:
                                # final 增量(小块):追加进累积,不覆盖。
                                delta = snapshot
                                sub_agent_snapshots[author_key] = prev + snapshot
                            if delta:
                                if not replace_snapshot:
                                    accumulated_text += delta
                                output_chunk = {"delta": delta, "type": "text"}
                                if replace_snapshot:
                                    output_chunk["replace"] = True
                                yield output_chunk

                # 处理工具调用事件 — ADK 通过 event.content.parts[].function_call
                # 发出工具调用（即 event.get_function_calls()），而非
                # event.actions.tool_calls。此处需同时检测两种路径：
                # (a) get_function_calls() — ADK 标准 Event 模型
                # (b) actions.tool_calls — 某些旧版或自定义 runner 可能使用
                emitted_tool_call_ids: set[str] = set()
                if hasattr(event, "get_function_calls") and event.get_function_calls():
                    for fc in event.get_function_calls():
                        fc_id = getattr(fc, "id", "") or getattr(fc, "name", "unknown")
                        if fc_id in emitted_tool_call_ids:
                            continue
                        emitted_tool_call_ids.add(fc_id)
                        # fc.args 可能是 dict 或 None；确保可序列化
                        fc_args = getattr(fc, "args", None) or {}
                        if not isinstance(fc_args, dict):
                            try:
                                import json as _json

                                fc_args = _json.loads(fc_args) if isinstance(fc_args, str) else {}
                            except Exception:
                                fc_args = {}
                        yield {
                            "type": "tool_call",
                            "tool_name": getattr(fc, "name", "unknown"),
                            "tool_args": fc_args,
                        }
                if hasattr(event, "actions") and event.actions:
                    tool_calls = getattr(event.actions, "tool_calls", None)
                    if tool_calls:
                        for tool_call in tool_calls:
                            tc_name = getattr(tool_call, "name", "unknown")
                            tc_id = getattr(tool_call, "id", "") or tc_name
                            if tc_id in emitted_tool_call_ids:
                                continue
                            emitted_tool_call_ids.add(tc_id)
                            yield {
                                "type": "tool_call",
                                "tool_name": tc_name,
                                "tool_args": getattr(tool_call, "input", {}),
                            }

                # ADK 原生审批 surface(tool-confirmation + v2.0 workflow HITL):
                # 翻译成统一 interrupt chunk,经 runtime:4645 既有 approval_request 通道
                # 消费 → 审批卡。能力探测兼容 1.34.x→2.x,缺字段则跳过。
                emitted_approval_ids: set[str] = set()
                for signal in self._extract_approval_signals(event):
                    aid = signal.get("approval_request_id", "")
                    if not aid or aid in emitted_approval_ids:
                        continue
                    emitted_approval_ids.add(aid)
                    yield {
                        "type": "interrupt",
                        "interrupt_info": signal,
                        "session_id": session_id,
                    }

                # 处理工具返回结果 — ADK 通过 event.content.parts[].function_response
                # 发出工具执行结果。当工具执行完毕后 ADK 会产生一个包含
                # function_response 的事件，此处将其转为 tool_result 语义事件，
                # 让前端可以感知"某个工具已完成执行"并展示结果。
                if hasattr(event, "content") and event.content and hasattr(event.content, "parts"):
                    for part in event.content.parts:
                        fr = getattr(part, "function_response", None)
                        if fr is not None:
                            fr_name = getattr(fr, "name", "unknown")
                            fr_output = getattr(fr, "response", None) or {}
                            if not isinstance(fr_output, dict):
                                try:
                                    import json as _json2

                                    if isinstance(fr_output, str):
                                        fr_output = _json2.loads(fr_output)
                                    else:
                                        fr_output = {"raw": str(fr_output)}
                                except Exception:
                                    fr_output = {"raw": str(fr_output)}
                            yield {
                                "type": "tool_result",
                                "tool_name": fr_name,
                                "tool_output": fr_output,
                            }

            # Set output.value for Langfuse top-level output display
            span.set_attribute("output.value", accumulated_text[:5000] if accumulated_text else "")
            span.set_attribute("agent.output", accumulated_text[:500])
            final_chunk: dict[str, Any] = {"output": accumulated_text, "type": "final"}
            if usage:
                final_chunk["usage"] = usage
                # last_usage = 最后一次 LLM 调用快照(input_tokens = 当前上下文窗口占用)
                final_chunk.setdefault("metadata", {})["last_usage"] = last_usage
            yield final_chunk
