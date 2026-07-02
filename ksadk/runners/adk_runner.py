"""
ADKRunner - Google ADK 框架运行时

参考 adk-python 原生实现，缓存 Runner 和 SessionService。
支持通过环境变量配置记忆体 (ShortTermMemory / LongTermMemory)。
"""

import base64
import inspect
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Mapping, Optional

from opentelemetry import trace

from ksadk.conversations.attachments import classify_attachment_kind, read_attachment_uri_bytes
from ksadk.conversations.model_context import supports_native_image_input
from ksadk.runners.base_runner import BaseRunner
from ksadk.sessions.continuity import ADKSessionAdapter

logger = logging.getLogger(__name__)

tracer = trace.get_tracer(__name__)


class ADKRunner(BaseRunner):
    """ADK 框架运行时"""

    def __init__(self, detection_result: Any, project_dir: str):
        super().__init__(detection_result, project_dir)
        self._runner = None
        self._session_service = None
        # Map external session_ids (e.g. from run_interactive or web) to ADK internal session IDs
        self._session_map: Dict[str, str] = {}
        # Fallback default session
        self._default_session_id: Optional[str] = None
        # Memory integration
        self._short_term_memory = None
        self._long_term_memory = None
        # Knowledge base integration
        self._knowledge_base = None
        # Keep runtime toolsets alive for the lifetime of the runner.
        self._runtime_toolsets: list[Any] = []
        # ADK resumability state
        self._resumable: bool = False
        self._last_adk_invocation_id: Optional[str] = None

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
            import json as _stdlib_json
            import google.adk.models.lite_llm as adk_lite_llm

            _original_fn = adk_lite_llm._message_to_generate_content_response

            def _patched_message_to_generate_content_response(
                message, *, is_partial=False, model_version=None, thought_parts=None
            ):
                """Wrapper that catches JSONDecodeError in final args parsing."""
                result = _original_fn(
                    message,
                    is_partial=is_partial,
                    model_version=model_version,
                    thought_parts=thought_parts,
                )
                # If the response has function_call parts with empty args,
                # fill in {} as fallback (this was what RobustJson used to do,
                # but now only at the final output stage, not during streaming).
                if result.content and result.content.parts:
                    for part in result.content.parts:
                        if part.function_call and part.function_call.args is None:
                            part.function_call.args = {}
                return result

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
        stm_backend = getattr(self._short_term_memory, "backend", None) if self._short_term_memory else None
        if resumable:
            backend = "adk_invocation"
            if stm_backend == "sqlite":
                backend = "adk_invocation+sqlite"
            elif stm_backend == "database":
                backend = "adk_invocation+postgres"
            return {
                "Supported": True,
                "Backend": backend,
                "Scope": "invocation",
                "Durable": stm_backend is not None and stm_backend != "local",
                "SharedAcrossPods": stm_backend == "database",
                "ResumeMode": "invocation_id",
                "Reason": "ADK ResumabilityConfig enabled; resume via invocation_id",
            }
        return {
            "Supported": False,
            "Backend": "none",
            "Scope": "unknown",
            "Durable": False,
            "SharedAcrossPods": False,
            "ResumeMode": "forward_only",
            "Reason": "ADK ResumabilityConfig not enabled; set KSADK_ADK_RESUMABLE=1 or configure App with resumability_config",
        }

    def get_runtime_capabilities(self) -> dict[str, Any]:
        capabilities = super().get_runtime_capabilities()
        resumable = getattr(self, "_resumable", False)
        if resumable:
            capabilities["SessionContinuity"] = {
                "Supported": True,
                "Type": "adk_invocation",
                "Level": "runtime",
                "Reason": "ADK ResumabilityConfig enabled, invocation_id-based checkpoint resume available",
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
                "Reason": "ADK native session can continue conversation context"
                if has_native_session
                else "conversation transcript can be replayed",
            }
        capabilities["ResumeRun"]["Reason"] = capabilities["Checkpoint"]["Reason"]
        return capabilities

    @dataclass
    class _ResolvabilityResult:
        enabled: bool
        source: str          # "agent_module" | "env" | "auto_persistent_session" | "default"
        app: Any             # User module exported App object (if any)

    def _resolve_resumability(self) -> "_ResolvabilityResult":
        """从环境变量或 agent 模块推断是否启用 ADK 可恢复性。"""
        # Priority 1: agent module exports app object with resumability_config
        module = getattr(self, "_module", None)
        if module is not None:
            try:
                from google.adk.apps import App
                for attr_name in ("app", "application"):
                    candidate = getattr(module, attr_name, None)
                    if isinstance(candidate, App) and candidate.resumability_config:
                        if candidate.resumability_config.is_resumable:
                            return self._ResolvabilityResult(
                                enabled=True, source="agent_module", app=candidate)
            except ImportError:
                pass

        # Priority 2: environment variable explicit control
        env_val = os.environ.get("KSADK_ADK_RESUMABLE", "").strip().lower()
        if env_val in ("1", "true", "yes"):
            return self._ResolvabilityResult(enabled=True, source="env", app=None)

        # Priority 3: persistent session backend auto-enable
        stm_backend = getattr(self._short_term_memory, "backend", None) if self._short_term_memory else None
        if stm_backend and stm_backend != "local":
            return self._ResolvabilityResult(
                enabled=True, source="auto_persistent_session", app=None)

        return self._ResolvabilityResult(enabled=False, source="default", app=None)

    def _build_runner(self) -> None:
        """构造 ADK Runner，优先使用 App 对象以启用 ResumabilityConfig。"""
        from google.adk.runners import Runner

        resumable = self._resolve_resumability()

        if resumable.app is not None:
            runner_kwargs = dict(
                app=resumable.app,
                session_service=self._session_service,
            )
        elif resumable.enabled:
            try:
                from google.adk.apps import App, ResumabilityConfig
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
        self._resumable = resumable.enabled

        if resumable.enabled:
            stm_backend = getattr(self._short_term_memory, "backend", None) if self._short_term_memory else None
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
            logger.info(
                f"LongTermMemory initialized: backend={backend}, "
                f"app_name={agent_name}"
            )
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
                f"KnowledgeBase initialized: dataset_id={kb.dataset_id}, "
                f"region={kb.region}"
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
            from google.adk.tools import load_memory

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
        if os.environ.get("KSADK_SANDBOX_TEMPLATE_ID") or os.environ.get("KSADK_SKILL_RUNTIME_TEMPLATE_ID"):
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
            logger.info("Injected MCP toolsets into agent (added: %s)", ", ".join(added))
        except ImportError as exc:
            logger.warning(f"Failed to import MCP runtime helpers: {exc}")
        except Exception as exc:
            logger.warning(f"Failed to inject MCP toolsets: {exc}")

    @staticmethod
    def _tool_name(tool: Any) -> str:
        return getattr(tool, "name", None) or getattr(tool, "__name__", "")

    def _append_tools_by_name(self, tools: list[Any]) -> list[str]:
        if not hasattr(self._agent, "tools"):
            logger.warning("Agent has no 'tools' attribute, cannot inject runtime tools")
            return []

        if self._agent.tools is None:
            self._agent.tools = []

        existing_names = {
            self._tool_name(tool)
            for tool in self._agent.tools
            if self._tool_name(tool)
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
            getattr(tool, key_attr)
            for tool in self._agent.tools
            if getattr(tool, key_attr, None)
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

    def load_agent(self) -> None:
        """加载 ADK Agent"""
        import warnings

        warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.main")

        self._apply_json_patch()

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
        from google.adk.sessions import InMemorySessionService

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
        self._inject_mcp_toolsets()

        # 初始化 Runner (使用 _build_runner 以支持 ResumabilityConfig)
        self._build_runner()
        self._default_model_name = self.normalize_requested_model(
            os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
        )
        self._default_model_reference = self._discover_model_reference(self._agent)
        self._active_model_name = (
            self._default_model_reference
            or self._default_model_name
        )

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
            default_model_name = (
                getattr(self, "_default_model_name", None)
                or self.normalize_requested_model(
                    os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
                )
            )
            if default_model_name:
                self.sync_process_model_env(default_model_name)
            target_reference = (
                getattr(self, "_default_model_reference", None)
                or default_model_name
            )
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
            self._active_model_name = self._discover_model_reference(self._agent) or target_reference
            return
        self._active_model_name = target_reference

    def _prepare_trace_metadata(self, session_id: str):
        """准备 Trace 元数据 (Tags, UserID, etc.)"""
        from ksadk.tracing.span_utils import prepare_trace_metadata
        return prepare_trace_metadata(
            detection_result=getattr(self, "detection_result", None)
        )

    async def _ensure_session(self, external_session_id: str = None) -> str:
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
                session = await self._session_service.create_session(
                    app_name=self._agent.name, user_id="ksadk_user"
                )
            self._session_map[external_session_id] = session.id
            return session.id

        # Case 2: No external ID (use default singleton)
        if self._default_session_id is None:
            if self._short_term_memory:
                session = await self._short_term_memory.create_session(
                    app_name=self._agent.name,
                    user_id="ksadk_user",
                )
            else:
                session = await self._session_service.create_session(
                    app_name=self._agent.name, user_id="ksadk_user"
                )
            self._default_session_id = session.id
        return self._default_session_id

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
    ) -> "types.Content":
        from google.genai import types
        parts = []
        if text:
            parts.append(types.Part(text=text))
        skipped_images: list[str] = []
        image_input_supported = supports_native_image_input(model_metadata)
        for att in attachments:
            mime_type = att.get("mime_type", "application/octet-stream")
            display_name = att.get("display_name", "")
            if classify_attachment_kind(str(mime_type), str(display_name)) == "image" and not image_input_supported:
                skipped_images.append(str(display_name or "未命名图片"))
                continue

            data: Optional[bytes] = None

            inline_data = att.get("data")
            if att.get("transport") == "inline" and inline_data:
                try:
                    data = base64.b64decode(str(inline_data).strip() + "===")
                except Exception as e:
                    logger.warning(f"Failed to decode inline attachment {att.get('display_name', 'uploaded_file')}: {e}")

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
                        "Ignoring direct local attachment reference %s; only resolved storage paths are allowed.",
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
        total_tokens = int(usage_metadata.get("total_token_count") or (input_tokens + output_tokens))
        if not (input_tokens or output_tokens or total_tokens or input_token_details or output_token_details):
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

    async def _collect_adk_invocation_id(
        self,
        events_async,
        *,
        ksadk_invocation_id: str,
        session_id: str,
    ):
        """包装 event 迭代器，在首个 event 到达时采集 ADK invocation_id 并存储映射，
        同时在可恢复边界写入 checkpoint 事件。"""
        first_event_captured = False
        checkpoint_seq = 1
        async for event in events_async:
            if not first_event_captured and hasattr(event, "invocation_id") and event.invocation_id:
                first_event_captured = True
                adk_inv_id = event.invocation_id
                self._last_adk_invocation_id = adk_inv_id
                await self._persist_invocation_mapping(
                    session_id=session_id,
                    ksadk_invocation_id=ksadk_invocation_id,
                    adk_invocation_id=adk_inv_id,
                )
            # Write checkpoint at resumable boundaries
            if self._resumable and first_event_captured:
                next_seq = await self._maybe_write_checkpoint(
                    event=event,
                    session_id=session_id,
                    ksadk_invocation_id=ksadk_invocation_id,
                    adk_invocation_id=self._last_adk_invocation_id or "",
                    checkpoint_seq=checkpoint_seq,
                )
                if next_seq is not None:
                    checkpoint_seq = next_seq
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
                adk_inv_id = event.invocation_id
                self._last_adk_invocation_id = adk_inv_id
                if ksadk_invocation_id:
                    await self._persist_invocation_mapping(
                        session_id=session_id,
                        ksadk_invocation_id=ksadk_invocation_id,
                        adk_invocation_id=adk_inv_id,
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

    async def _maybe_write_checkpoint(
        self,
        *,
        event: Any,
        session_id: str,
        ksadk_invocation_id: str,
        adk_invocation_id: str,
        checkpoint_seq: int,
    ) -> int | None:
        """如果是可恢复边界，写入 run_checkpoint 事件。返回下一个 checkpoint_seq 或 None。"""
        if not self._is_resumable_boundary(event):
            return None

        from ksadk.conversations.runtime import append_run_checkpoint_event

        metadata = self._extract_checkpoint_metadata(event)

        await append_run_checkpoint_event(
            session_id=session_id,
            author=self._agent.name,
            run_id=ksadk_invocation_id,
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
            phase="tool_call" if (hasattr(event, "get_function_calls") and event.get_function_calls()) else "agent_state",
            invocation_id=ksadk_invocation_id,
            metadata=metadata,
        )
        return checkpoint_seq + 1

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
                metadata["agent_state_keys"] = list(agent_state.keys()) if isinstance(agent_state, dict) else []
            if getattr(event.actions, "end_of_agent", False):
                metadata["is_terminal"] = True
                metadata["end_of_agent"] = True

        # 是否可恢复
        metadata["is_resumable"] = self._resumable
        metadata["resume_status"] = "resumable" if self._resumable else "disabled"

        return metadata

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """调用 ADK Agent"""
        from google.genai import types

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
            span.set_attribute("user.input", user_input[:200] if isinstance(user_input, str) else str(user_input)[:200])

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

            # 恢复模式不构建 new_message
            if not is_resume:
                new_message = self._build_adk_content(
                    user_input,
                    input_data.get("attachments", []),
                    model_metadata=input_data.get("model_metadata"),
                )
            else:
                new_message = None
            state_delta = self._build_state_delta(input_data)

            ksadk_invocation_id = str(input_data.get("invocation_id") or input_data.get("run_id") or "")

            if is_resume and self._resumable:
                # 恢复模式：使用 ADK invocation_id
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

                if adk_invocation_id:
                    events_async = self._runner.run_async(
                        session_id=session_id,
                        user_id="ksadk_user",
                        invocation_id=adk_invocation_id,
                    )
                    # Write resume event
                    try:
                        from ksadk.conversations.runtime import append_run_resume_event
                        await append_run_resume_event(
                            session_id=session_id,
                            author=self._agent.name,
                            run_id=ksadk_invocation_id,
                            checkpoint_id=str(input_data.get("checkpoint_id") or ""),
                            resume_attempt_id=f"resume_{os.urandom(8).hex()}",
                            framework="adk",
                            framework_ref={"adk": {"invocation_id": adk_invocation_id}},
                            invocation_id=ksadk_invocation_id,
                        )
                    except Exception as exc:
                        logger.warning("Failed to write ADK resume event: %s", exc)
                else:
                    logger.warning(
                        "Resume requested but ADK invocation_id not found, falling back to new invocation"
                    )
                    events_async = self._runner.run_async(
                        session_id=session_id,
                        user_id="ksadk_user",
                        new_message=new_message or self._build_adk_content("[empty message]", []),
                        state_delta=state_delta or None,
                    )
            else:
                # 正常模式
                events_async = self._runner.run_async(
                    session_id=session_id,
                    user_id="ksadk_user",
                    new_message=new_message or self._build_adk_content("[empty message]", []),
                    state_delta=state_delta or None,
                )

            # Wrap events for invocation_id collection and checkpoint writing
            if ksadk_invocation_id and self._resumable:
                wrapped_async = self._collect_adk_invocation_id(
                    events_async,
                    ksadk_invocation_id=ksadk_invocation_id,
                    session_id=session_id,
                )
            else:
                wrapped_async = self._collect_adk_invocation_id_if_present(
                    events_async,
                    session_id=session_id,
                    ksadk_invocation_id=ksadk_invocation_id,
                )

            final_response = ""

            events_list = []
            usage: dict[str, Any] = {}
            async for event in wrapped_async:
                events_list.append(event)
                event_usage = self._extract_event_usage(event)
                if event_usage:
                    usage = event_usage
                if hasattr(event, "content") and event.content:
                    if hasattr(event.content, "parts"):
                        for part in event.content.parts:
                            # 过滤掉思考内容 (thought=True)，只保留最终答案
                            is_thought = getattr(part, "thought", False)
                            if hasattr(part, "text") and part.text and not is_thought:
                                final_response = part.text

            # Set output.value for Langfuse top-level output display
            span.set_attribute("output.value", final_response[:5000] if final_response else "")
            span.set_attribute("agent.output", final_response[:500] if final_response else "")
            result = {"output": final_response, "events": events_list}
            if usage:
                result["usage"] = usage
            return result

    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 ADK Agent

        使用 StreamingMode.SSE 启用真正的流式 token 输出
        """
        from google.adk.agents.run_config import RunConfig, StreamingMode
        from google.genai import types

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
            span.set_attribute("user.input", user_input[:200] if isinstance(user_input, str) else str(user_input)[:200])

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

            # 恢复模式不构建 new_message
            if not is_resume:
                new_message = self._build_adk_content(
                    user_input,
                    input_data.get("attachments", []),
                    model_metadata=input_data.get("model_metadata"),
                )
            else:
                new_message = None
            state_delta = self._build_state_delta(input_data)

            ksadk_invocation_id = str(input_data.get("invocation_id") or input_data.get("run_id") or "")

            # 使用 StreamingMode.SSE 启用真正的流式输出
            run_config = RunConfig(streaming_mode=StreamingMode.SSE)

            if is_resume and self._resumable:
                adk_invocation_id = None
                framework_ref = input_data.get("framework_ref") or {}
                if isinstance(framework_ref, dict):
                    adk_ref = framework_ref.get("adk") or {}
                    if isinstance(adk_ref, dict):
                        adk_invocation_id = adk_ref.get("invocation_id")
                if not adk_invocation_id and ksadk_invocation_id:
                    adk_invocation_id = await self._resolve_adk_invocation_id(
                        session_id=session_id,
                        ksadk_invocation_id=ksadk_invocation_id,
                    )

                if adk_invocation_id:
                    events_async = self._runner.run_async(
                        session_id=session_id,
                        user_id="ksadk_user",
                        invocation_id=adk_invocation_id,
                        run_config=run_config,
                    )
                    try:
                        from ksadk.conversations.runtime import append_run_resume_event
                        await append_run_resume_event(
                            session_id=session_id,
                            author=self._agent.name,
                            run_id=ksadk_invocation_id,
                            checkpoint_id=str(input_data.get("checkpoint_id") or ""),
                            resume_attempt_id=f"resume_{os.urandom(8).hex()}",
                            framework="adk",
                            framework_ref={"adk": {"invocation_id": adk_invocation_id}},
                            invocation_id=ksadk_invocation_id,
                        )
                    except Exception as exc:
                        logger.warning("Failed to write ADK resume event: %s", exc)
                else:
                    logger.warning(
                        "Resume requested but ADK invocation_id not found, falling back to new invocation"
                    )
                    events_async = self._runner.run_async(
                        session_id=session_id,
                        user_id="ksadk_user",
                        new_message=new_message or self._build_adk_content("[empty message]", []),
                        state_delta=state_delta or None,
                        run_config=run_config,
                    )
            else:
                events_async = self._runner.run_async(
                    session_id=session_id,
                    user_id="ksadk_user",
                    new_message=new_message or self._build_adk_content("[empty message]", []),
                    state_delta=state_delta or None,
                    run_config=run_config,
                )

            # Wrap events for invocation_id collection and checkpoint writing
            if ksadk_invocation_id and self._resumable:
                wrapped_async = self._collect_adk_invocation_id(
                    events_async,
                    ksadk_invocation_id=ksadk_invocation_id,
                    session_id=session_id,
                )
            else:
                wrapped_async = self._collect_adk_invocation_id_if_present(
                    events_async,
                    session_id=session_id,
                    ksadk_invocation_id=ksadk_invocation_id,
                )

            accumulated_text = ""
            usage: dict[str, Any] = {}

            async for event in wrapped_async:
                event_usage = self._extract_event_usage(event)
                if event_usage:
                    usage = event_usage
                # Only yield text delta if event is partial to avoid duplication of final summary
                if hasattr(event, "content") and event.content and getattr(event, "partial", False):
                    if hasattr(event.content, "parts"):
                        for part in event.content.parts:
                            if hasattr(part, "text") and part.text:
                                is_thought = getattr(part, "thought", False)
                                accumulated_text += part.text
                                # 标记思考内容，前端可以选择是否展示
                                yield {
                                    "delta": part.text,
                                    "type": "thinking" if is_thought else "text",
                                }

                # 处理工具调用事件
                if hasattr(event, "actions") and event.actions:
                    tool_calls = getattr(event.actions, "tool_calls", None)
                    if tool_calls:
                        for tool_call in tool_calls:
                            yield {
                                "type": "tool_call",
                                "tool_name": getattr(tool_call, "name", "unknown"),
                                "tool_args": getattr(tool_call, "input", {}),
                            }

            # Set output.value for Langfuse top-level output display
            span.set_attribute("output.value", accumulated_text[:5000] if accumulated_text else "")
            span.set_attribute("agent.output", accumulated_text[:500])
            final_chunk: dict[str, Any] = {"output": accumulated_text, "type": "final"}
            if usage:
                final_chunk["usage"] = usage
            yield final_chunk
