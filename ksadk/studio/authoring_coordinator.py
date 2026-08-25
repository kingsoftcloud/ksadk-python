"""Application-level commits for Agent authoring inspections."""

from __future__ import annotations

import copy
import logging
import os
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, cast

from ksadk.studio.authoring import AgentAuthoringService
from ksadk.studio.codex_manifest import CodexAgentManifest
from ksadk.studio.contracts import (
    AgentBindings,
    AgentDraft,
    AgentSpec,
    RuntimeRef,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.identifiers import generate_agent_slug, is_generated_agent_slug
from ksadk.studio.templates import default_agent_spec

LOGGER = logging.getLogger(__name__)

# 对话创建阶段推进序列。前端只消费阶段名展示两段式文案，不解析内容。
CONVERSATION_STAGES = (
    "resolving_model",
    "generating",
    "codex_writing",
    "validating",
    "correcting",
    "done",
    "failed",
)
# 进度记录只保留最近的少量请求，避免长驻进程无限增长。
_CONVERSATION_STATUS_LIMIT = 64


def _apply_profile_model_endpoint(proposal: Any, model: Any) -> Any:
    """用选中模型 Profile 的真实 endpoint 覆写 spec.model 中的占位 URL。

    ``parse_conversation_proposal`` 的 sanitize 层会把模型照抄的 example.com
    占位 URL 替换为 ``https://model-profile.invalid/placeholder`` 标记；此处
    在返回给前端前替换为 Profile 的真实 baseUrl/endpointUrl，保证 spec 自洽。
    """
    marker = "model-profile.invalid"
    spec = getattr(proposal, "spec", None)
    model_spec = getattr(spec, "model", None) if spec is not None else None
    if model_spec is None:
        return proposal
    if marker not in str(getattr(model_spec, "base_url", "") or ""):
        return proposal
    base = str(getattr(model, "endpoint_url", "") or "").rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    if not base:
        return proposal
    return proposal.model_copy(
        update={
            "spec": spec.model_copy(
                update={"model": model_spec.model_copy(update={"base_url": base})}
            )
        }
    )


class StudioAuthoringCoordinator:
    """Coordinates repositories without expanding the StudioService façade."""

    def __init__(self, studio: Any) -> None:
        self.studio = studio
        self.backend = AgentAuthoringService(studio.workspace)
        # Codex authoring 执行器可注入替换（测试）；默认惰性探测可用性。
        self.codex_authoring: Any = getattr(studio, "codex_authoring_executor", None)
        self._id_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._conversation_status: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _codex_authoring_executor(self) -> Any:
        if self.codex_authoring is not None:
            return self.codex_authoring
        from ksadk.studio.codex_authoring import CodexAuthoringExecutor

        self.codex_authoring = CodexAuthoringExecutor(
            self.studio.workspace,
            credential_resolver=self.studio.credentials,
        )
        return self.codex_authoring

    def _use_codex_authoring(self) -> bool:
        """选择 codex / chat authoring 链路（chat 永远保留为 fallback）。"""

        mode = str(os.environ.get("KSADK_STUDIO_AUTHORIZER") or "").strip().lower()
        if mode in {"codex", "codex-writing", "1", "true"}:
            return True
        if mode in {"chat", "off", "0", "false", "none"}:
            return False
        # 未显式指定：探测 codex 可用性，失败自动降级 chat 链。
        try:
            self._codex_authoring_executor().probe()
        except Exception as exc:
            LOGGER.warning("codex authoring unavailable, using chat chain: %s", exc)
            return False
        return True

    # ------------------------------------------------------------------
    # Conversation authoring stage tracking
    # ------------------------------------------------------------------

    def _record_conversation_stage(
        self,
        request_id: str | None,
        stage: str,
        *,
        detail: str | None = None,
    ) -> None:
        if not request_id:
            return
        entry = {
            "requestId": request_id,
            "stage": stage,
            "updatedAt": time.time(),
            **({"detail": detail} if detail else {}),
        }
        with self._status_lock:
            self._conversation_status[request_id] = entry
            self._conversation_status.move_to_end(request_id)
            while len(self._conversation_status) > _CONVERSATION_STATUS_LIMIT:
                self._conversation_status.popitem(last=False)

    def conversation_status(self, request_id: str) -> dict[str, Any] | None:
        with self._status_lock:
            entry = self._conversation_status.get(request_id)
            return dict(entry) if entry else None

    def create(
        self,
        *,
        name: str,
        slug: str | None = None,
        runtime_type: str,
        template: str = "blank",
        description: str = "",
        spec: AgentSpec | None = None,
    ) -> AgentDraft:
        with self._id_lock:
            agent_id = self._allocate_agent_id(slug)
            resolved_slug = slug or agent_id
            resolved = (spec or default_agent_spec(template, description=description)).model_copy(
                deep=True
            )
            canonical_runtime = self.backend.runtime_ref(agent_id, runtime_type)
            proposed_runtime = resolved.runtime
            if proposed_runtime is not None and proposed_runtime.type != runtime_type:
                raise StudioError(
                    "AGENT_RUNTIME_MISMATCH",
                    "AgentSpec Runtime 与创建方式不一致",
                    status_code=422,
                    field="runtimeType",
                    details={
                        "runtimeType": runtime_type,
                        "specRuntimeType": proposed_runtime.type,
                    },
                )
            if proposed_runtime is not None:
                if runtime_type == "codex" and proposed_runtime.version:
                    canonical_runtime.version = proposed_runtime.version
                elif runtime_type in {"adk", "langgraph"}:
                    canonical_runtime.entry_point = (
                        proposed_runtime.entry_point or canonical_runtime.entry_point
                    )
                    canonical_runtime.agent_variable = proposed_runtime.agent_variable
                    canonical_runtime.version = proposed_runtime.version
                    canonical_runtime.detection = proposed_runtime.detection
            resolved.runtime = canonical_runtime
            if description:
                resolved.description = description
            draft = self.studio.create_studio_agent(
                agent_id=agent_id,
                name=name,
                description=description,
                template=template,
                spec=resolved,
                labels={"agentkit.ksyun.com/slug": self.backend.normalize_slug(resolved_slug)},
            )
        return cast(AgentDraft, draft)

    def inspect_import(self, content: bytes, *, filename: str) -> dict:
        return self.backend.inspect_import(content, filename=filename)

    def commit_import(
        self,
        inspection_token: str,
        *,
        name: str | None = None,
        slug: str | None = None,
    ) -> AgentDraft:
        inspection = self.backend.load_import(inspection_token)
        display_name = str(name or inspection.display_name).strip()
        agent_id = self._allocate_agent_id(slug)
        resolved_slug = slug or agent_id
        if inspection.kind == "codex-manifest":
            created = self._commit_codex_import(
                agent_id,
                display_name,
                CodexAgentManifest.model_validate(inspection.payload),
                resolved_slug=resolved_slug,
            )
            self.backend.consume_import(inspection_token)
            return created

        imported = AgentDraft.model_validate(inspection.payload)
        spec = imported.spec.model_copy(deep=True)
        source = self.backend.source_directory(inspection)
        if spec.runtime is None:
            raise StudioError(
                "AGENT_RUNTIME_REQUIRED",
                "导入 Agent 必须声明 RuntimeRef",
                status_code=422,
            )
        if spec.runtime.type in {"adk", "langgraph"}:
            imported_runtime = spec.runtime
            canonical_runtime = self.backend.runtime_ref(agent_id, imported_runtime.type)
            canonical_runtime.entry_point = (
                imported_runtime.entry_point or canonical_runtime.entry_point
            )
            canonical_runtime.agent_variable = imported_runtime.agent_variable
            canonical_runtime.version = imported_runtime.version
            canonical_runtime.detection = imported_runtime.detection
            spec.runtime = canonical_runtime
        created = self.studio.create_agent(
            agent_id=agent_id,
            name=display_name,
            description=spec.description,
            template=imported.metadata.labels.get(
                "agentkit.ksyun.com/template",
                "blank",
            ),
            spec=spec,
            labels={
                "agentkit.ksyun.com/slug": self.backend.normalize_slug(resolved_slug),
                "agentkit.ksyun.com/source": "import",
                "agentkit.ksyun.com/source-digest": inspection.source_digest,
            },
        )
        if source is not None and created.spec.runtime and created.spec.runtime.project_path:
            target = self.studio.workspace.resolve(created.spec.runtime.project_path)
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(
                source,
                target,
                symlinks=False,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
            )
        self.backend.consume_import(inspection_token)
        return cast(AgentDraft, self.studio.drafts.get(agent_id))

    def inspect_project(self, project_path: str) -> dict:
        inspection = self.backend.inspect_project(project_path)
        spec, unresolved = self._project_agent_spec(
            inspection,
            agent_id="agentkit-preview",
            model_profile_id=None,
        )
        return {
            **inspection,
            "agentSpec": spec.model_dump(by_alias=True, exclude_none=True, mode="json"),
            "bindingProjection": {
                "preserved": spec.bindings.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "unresolved": unresolved,
            },
        }

    def commit_project(
        self,
        inspection_token: str,
        *,
        name: str | None = None,
        slug: str | None = None,
        model_profile_id: str | None = None,
    ) -> AgentDraft:
        inspection = self.backend.load_project(inspection_token)
        runtime_type = str(inspection["runtimeType"])
        display_name = str(name or inspection.get("name") or "Imported Agent").strip()
        resolved_slug = slug
        if runtime_type == "codex":
            manifest_path = self.studio.workspace.resolve(
                Path(str(inspection["projectPath"])) / "agentengine.yaml",
                must_exist=True,
            )
            created = self.commit_import(
                self.inspect_import(
                    manifest_path.read_bytes(),
                    filename="agentengine.yaml",
                )["inspectionToken"],
                name=display_name,
                slug=resolved_slug,
            )
            self.backend.consume_project(inspection_token)
            return created

        agent_id = self._allocate_agent_id(resolved_slug)
        resolved_slug = resolved_slug or agent_id
        spec, unresolved = self._project_agent_spec(
            inspection,
            agent_id=agent_id,
            model_profile_id=model_profile_id,
        )
        if unresolved:
            raise StudioError(
                "PROJECT_BINDINGS_UNRESOLVED",
                "项目中的 Tool、MCP 或 Skill 绑定无法无损映射，请先安装或修正对应资源",
                status_code=422,
                details={"unresolved": unresolved},
            )
        created = self.studio.create_agent(
            agent_id=agent_id,
            name=display_name,
            spec=spec,
            labels={
                "agentkit.ksyun.com/slug": self.backend.normalize_slug(resolved_slug),
                "agentkit.ksyun.com/source": "project-detection",
                "agentkit.ksyun.com/source-digest": str(inspection["sourceDigest"]),
            },
        )
        self.backend.consume_project(inspection_token)
        return cast(AgentDraft, created)

    async def compose_conversation(
        self,
        *,
        messages: list[dict[str, str]],
        model_profile_id: str,
        request_id: str | None = None,
    ) -> dict:
        started = time.monotonic()
        self._record_conversation_stage(request_id, "resolving_model")
        LOGGER.info(
            "conversation authoring started: modelProfileId=%s messages=%d requestId=%s",
            model_profile_id,
            len(messages),
            request_id or "-",
        )
        try:
            result = await self._compose_conversation_inner(
                messages=messages,
                model_profile_id=model_profile_id,
                request_id=request_id,
                started=started,
            )
        except Exception as exc:
            self._record_conversation_stage(request_id, "failed", detail=str(exc))
            LOGGER.warning(
                "conversation authoring failed after %.2fs: modelProfileId=%s reason=%s",
                time.monotonic() - started,
                model_profile_id,
                exc,
            )
            raise
        return result

    async def _compose_conversation_inner(
        self,
        *,
        messages: list[dict[str, str]],
        model_profile_id: str,
        request_id: str | None,
        started: float,
    ) -> dict:
        model_spec = self.studio.catalog.resolve_model(
            AgentBindings(model_profile_id=model_profile_id)
        )
        if model_spec is None:
            raise StudioError(
                "AGENT_MODEL_REQUIRED",
                "对话构建需要选择一个 Model Profile",
                status_code=422,
            )
        model = self.studio.catalog.resolver.resolve_model(model_spec)
        # authoring 自身的 max_tokens 跟随 profile：未配置则 payload 不带该字段
        # （服务端默认）；finishReason=length 截断由 model_client 一次性扩容重试兜底。
        LOGGER.info(
            "conversation authoring model resolved: model=%s endpoint=%s",
            getattr(model, "model", "-"),
            getattr(model, "endpoint_url", "-"),
        )
        normalized_messages = self.backend.conversation_messages(messages)
        previous_proposal = None
        for item in reversed(messages):
            if str(item.get("role") or "").strip() != "assistant":
                continue
            try:
                previous_proposal = self.backend.parse_conversation_proposal(
                    str(item.get("content") or "")
                )
            except StudioError:
                continue
            break

        if self._use_codex_authoring():
            codex_result = await self._compose_conversation_codex(
                messages=messages,
                model=model,
                previous_proposal=previous_proposal,
                request_id=request_id,
                started=started,
                model_profile_id=model_profile_id,
            )
            if codex_result is not None:
                return codex_result

        request_options = {
            "network_policy": self.backend.authoring_network_policy(model.endpoint_url),
            "timeout_seconds": 30,
            "backoff_seconds": 1,
            "response_format": {"type": "json_object"},
            # finishReason=length 截断时在 model_client 内自动扩容 max_tokens
            # 重发一次（一次性），避免大 JSON 被截断成空响应。
            "retry_on_length": True,
        }
        self._record_conversation_stage(request_id, "generating")
        response = await self.studio.model_client.complete(
            model,
            messages=normalized_messages,
            max_attempts=2,
            **request_options,
        )
        self._record_conversation_stage(request_id, "validating")
        try:
            proposal = self.backend.parse_conversation_proposal(
                response.content,
                base=previous_proposal,
            )
        except StudioError as exc:
            if exc.code != "AUTHORING_MODEL_OUTPUT_INVALID":
                raise
            validation_error = str(exc.details.get("reason") or exc.message)
            self._record_conversation_stage(request_id, "correcting")
            LOGGER.warning(
                "conversation authoring patch invalid, retrying once: reason=%s",
                validation_error,
            )
            retry_messages = [
                *normalized_messages,
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": (
                        "上一次输出未通过 Agent Draft Patch 校验。请只返回一个 JSON 对象，"
                        "不要解释或使用 Markdown。首轮必须包含 name、slug、runtimeType、"
                        "description、spec；后续轮次可以只返回需要变更的字段。"
                        f"校验错误细节如下，请逐条修正：\n{validation_error}"
                    ),
                },
            ]
            response = await self.studio.model_client.complete(
                model,
                messages=retry_messages,
                max_attempts=1,
                **request_options,
            )
            try:
                proposal = self.backend.parse_conversation_proposal(
                    response.content,
                    base=previous_proposal,
                )
            except StudioError as retry_exc:
                if retry_exc.code != "AUTHORING_MODEL_OUTPUT_INVALID":
                    raise
                raise StudioError(
                    "AUTHORING_MODEL_OUTPUT_INVALID",
                    "对话构建模型没有返回合法的 Agent Draft Patch",
                    status_code=502,
                    details={
                        "validationError": str(
                            retry_exc.details.get("reason") or retry_exc.message
                        ),
                        "attemptedCorrections": 1,
                    },
                ) from retry_exc
        self._record_conversation_stage(request_id, "done")
        LOGGER.info(
            "conversation authoring finished in %.2fs: modelProfileId=%s slug=%s",
            time.monotonic() - started,
            model_profile_id,
            getattr(proposal, "slug", "-"),
        )
        return {
            "proposal": _apply_profile_model_endpoint(proposal, model).model_dump(
                by_alias=True, mode="json"
            ),
            "requiresConfirmation": True,
            "authoringMode": "chat",
            "usage": response.usage.model_dump(by_alias=True, mode="json"),
        }

    async def _compose_conversation_codex(
        self,
        *,
        messages: list[dict[str, str]],
        model: Any,
        previous_proposal: Any,
        request_id: str | None,
        started: float,
        model_profile_id: str,
    ) -> dict | None:
        """让真实 Codex 会话在工作区写 agentkit.yaml；任何失败降级 chat 链。

        返回 ``None`` 表示应降级（探测失败/超时/重试后仍不合法），调用方继续走
        既有 chat 链路，保证零回归。
        """

        self._record_conversation_stage(request_id, "codex_writing")
        executor = self._codex_authoring_executor()
        try:
            result = await executor.compose(
                messages=messages,
                model=model,
                base=previous_proposal,
                request_id=request_id,
            )
        except Exception as exc:
            LOGGER.warning(
                "codex authoring failed after %.2fs, falling back to chat chain: "
                "modelProfileId=%s requestId=%s reason=%s",
                time.monotonic() - started,
                model_profile_id,
                request_id or "-",
                exc,
            )
            self._record_conversation_stage(
                request_id, "generating", detail=f"codex authoring 降级: {exc}"
            )
            return None
        self._record_conversation_stage(request_id, "done")
        LOGGER.info(
            "conversation authoring finished in %.2fs via codex: modelProfileId=%s "
            "slug=%s attempts=%d",
            time.monotonic() - started,
            model_profile_id,
            getattr(result.proposal, "slug", "-"),
            result.attempts,
        )
        return {
            "proposal": _apply_profile_model_endpoint(result.proposal, model).model_dump(
                by_alias=True, mode="json"
            ),
            "requiresConfirmation": True,
            "authoringMode": "codex",
            "usage": result.usage.model_dump(by_alias=True, mode="json"),
        }

    def _project_agent_spec(
        self,
        inspection: dict[str, Any],
        *,
        agent_id: str,
        model_profile_id: str | None,
    ) -> tuple[AgentSpec, list[dict[str, Any]]]:
        """Project detected project config without silently dropping authoring fields."""

        config = inspection.get("evidence", {}).get("config", {})
        if not isinstance(config, dict):
            config = {}
        embedded = config.get("spec")
        payload: dict[str, Any] = copy.deepcopy(embedded) if isinstance(embedded, dict) else {}

        for field in (
            "description",
            "model",
            "capabilities",
            "bindings",
            "execution",
            "context",
            "memory",
            "security",
            "evaluation",
        ):
            if field not in payload and field in config:
                payload[field] = copy.deepcopy(config[field])

        instructions = payload.get("instructions")
        if not isinstance(instructions, dict):
            instructions = {}
        instructions.setdefault(
            "system",
            str(
                config.get("prompt")
                or config.get("instruction")
                or "You are a reliable assistant imported from an existing project."
            ),
        )
        instructions.setdefault("task", str(config.get("task_prompt") or config.get("task") or ""))
        payload["instructions"] = instructions

        model_payload = payload.get("model")
        if isinstance(model_payload, str) and model_payload.strip():
            upstream = (
                (os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE") or "")
                .strip()
                .rstrip("/")
            )
            endpoint = (
                {"endpointUrl": upstream}
                if upstream.endswith("/chat/completions")
                else {"baseUrl": upstream or "https://api.openai.com/v1"}
            )
            payload["model"] = {
                "model": model_payload.strip(),
                "credentialRef": "env://AGENTKIT_MODEL_API_KEY",
                **endpoint,
            }

        bindings = payload.get("bindings")
        if not isinstance(bindings, dict):
            bindings = {}
        else:
            bindings = copy.deepcopy(bindings)
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}
        else:
            capabilities = copy.deepcopy(capabilities)
        unresolved: list[dict[str, Any]] = []
        legacy_fields = (
            ("tools", "tools"),
            ("mcpServers", "mcp_servers"),
            ("skills", "skills"),
        )
        for canonical, legacy in legacy_fields:
            if canonical in bindings or canonical in capabilities:
                continue
            raw = config.get(canonical, config.get(legacy))
            if raw is None:
                continue
            if not isinstance(raw, list):
                unresolved.append(
                    {"kind": canonical, "value": copy.deepcopy(raw), "reason": "not-a-list"}
                )
                continue
            projected_bindings: list[dict[str, Any]] = []
            projected_capabilities: list[dict[str, Any]] = []
            for item in raw:
                if isinstance(item, str) and item.strip():
                    projected_bindings.append({"resourceId": item.strip()})
                elif isinstance(item, dict) and (item.get("resourceId") or item.get("resource_id")):
                    projected_bindings.append(copy.deepcopy(item))
                elif isinstance(item, dict) and item.get("name") and item.get("version"):
                    projected_capabilities.append(copy.deepcopy(item))
                else:
                    unresolved.append(
                        {
                            "kind": canonical,
                            "value": copy.deepcopy(item),
                            "reason": "unsupported-binding-shape",
                        }
                    )
            if projected_bindings:
                bindings[canonical] = projected_bindings
            if projected_capabilities:
                capabilities[canonical] = projected_capabilities

        if model_profile_id:
            existing_profiles = list(
                bindings.get("modelProfileIds") or bindings.get("model_profile_ids") or []
            )
            bindings["modelProfileId"] = model_profile_id
            bindings["modelProfileIds"] = list(
                dict.fromkeys([model_profile_id, *existing_profiles])
            )
        if "modelParameters" not in bindings and "model_parameters" in config:
            bindings["modelParameters"] = copy.deepcopy(config["model_parameters"])
        if "policyTemplate" not in bindings:
            policy = config.get("policy_template", config.get("policy"))
            if isinstance(policy, str) and policy:
                bindings["policyTemplate"] = policy
        payload["bindings"] = bindings
        payload["capabilities"] = capabilities

        try:
            spec = AgentSpec.model_validate(payload)
        except ValueError as exc:
            raise StudioError(
                "PROJECT_AGENT_SPEC_INVALID",
                "项目配置无法无损转换为 AgentSpec",
                status_code=422,
                details={"reason": str(exc)},
            ) from exc

        runtime_type = str(inspection["runtimeType"])
        detected_runtime = self.backend.runtime_ref(agent_id, runtime_type)
        supplied_runtime = spec.runtime
        if supplied_runtime is not None and supplied_runtime.type != runtime_type:
            raise StudioError(
                "PROJECT_RUNTIME_MISMATCH",
                "项目配置中的 Runtime 与检测结果不一致",
                status_code=422,
                details={
                    "detected": runtime_type,
                    "configured": supplied_runtime.type,
                },
            )
        if runtime_type in {"adk", "langgraph"}:
            detected_runtime = RuntimeRef(
                type=cast(Any, runtime_type),
                project_path=str(inspection["projectPath"]),
                entry_point=(
                    supplied_runtime.entry_point
                    if supplied_runtime and supplied_runtime.entry_point
                    else str(inspection.get("entryPoint") or "agent.py")
                ),
                agent_variable=(
                    supplied_runtime.agent_variable
                    if supplied_runtime
                    else str(
                        inspection.get("agentVariable")
                        or ("graph" if runtime_type == "langgraph" else "root_agent")
                    )
                ),
                version=supplied_runtime.version if supplied_runtime else None,
                detection="auto",
            )
        elif supplied_runtime is not None and supplied_runtime.version:
            detected_runtime.version = supplied_runtime.version
        spec.runtime = detected_runtime

        catalog_ids = {
            kind: {item.resource_id for item in self.studio.catalog.list(kind=kind, limit=500)}
            for kind in ("tool", "mcp", "skill")
        }
        for kind, values in (
            ("tool", spec.bindings.tools),
            ("mcp", spec.bindings.mcp_servers),
            ("skill", spec.bindings.skills),
        ):
            for binding in values:
                if binding.resource_id not in catalog_ids[kind]:
                    unresolved.append(
                        {
                            "kind": kind,
                            "value": binding.model_dump(
                                by_alias=True,
                                exclude_none=True,
                                mode="json",
                            ),
                            "reason": "not-in-resource-catalog",
                        }
                    )
        return spec, unresolved

    def _agent_exists(self, agent_id: str) -> bool:
        return bool(
            self.studio.codex_manifests.exists(agent_id)
            or self.studio._draft_exists(agent_id)
            or (self.studio.workspace.resolve("agents") / agent_id).exists()
        )

    def _allocate_agent_id(self, slug: str | None) -> str:
        if slug and slug.strip():
            if is_generated_agent_slug(slug):
                candidate = slug.strip()
                if self._agent_exists(candidate):
                    raise StudioError(
                        "AGENT_ALREADY_EXISTS",
                        "本地标识已存在，请重新生成",
                        status_code=409,
                        field="slug",
                        details={"id": candidate},
                    )
                return candidate
            return self.backend.allocate_agent_id(slug)
        return generate_agent_slug(self._agent_exists)

    def _commit_codex_import(
        self,
        agent_id: str,
        display_name: str,
        manifest: CodexAgentManifest,
        *,
        resolved_slug: str,
    ) -> AgentDraft:
        imported = manifest.model_copy(update={"name": agent_id}, deep=True)
        snapshot = self.studio.codex_manifests.save(imported)
        draft = self.studio.codex_agents._project(snapshot)
        draft.metadata.name = display_name
        draft.metadata.labels.update(
            {
                "agentkit.ksyun.com/slug": self.backend.normalize_slug(resolved_slug),
                "agentkit.ksyun.com/source": "import",
            }
        )
        self.studio.codex_drafts.save(draft)
        return cast(AgentDraft, self.studio.codex_agents._project(snapshot, current=draft))


__all__ = ["StudioAuthoringCoordinator"]
