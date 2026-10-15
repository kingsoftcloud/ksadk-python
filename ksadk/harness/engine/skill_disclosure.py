"""默认 Agent Loop 的 Skill 渐进披露桥接。

本模块只负责把 Revision 固定的 Skill 绑定投影为 Level 0 目录和受限的
Level 1/2/3 读取工具。Skill 包下载、校验和解压仍属于 ``ksadk.skills``。
"""

from __future__ import annotations

import base64
from typing import Any

from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.skill_runtime import (
    SKILL_CATALOG_TOOL,
    SKILL_INSTRUCTIONS_TOOL,
    SKILL_MANIFEST_TOOL,
    SKILL_RESOURCE_TOOL,
    SkillRuntime,
)
from ksadk.harness.spec import HarnessSpec


class SkillDisclosureBridge:
    """把 SkillRuntime 接入默认 Loop，同时守住 Revision 与信任边界。"""

    _TOOL_NAMES = frozenset(
        {SKILL_CATALOG_TOOL, SKILL_MANIFEST_TOOL, SKILL_INSTRUCTIONS_TOOL, SKILL_RESOURCE_TOOL}
    )

    def __init__(self, runtime: SkillRuntime | None) -> None:
        self._runtime = runtime

    def validate_bindings(
        self,
        spec: HarnessSpec,
        *,
        tool_names: set[str],
        sub_agent_names: set[str],
    ) -> None:
        collisions = self._TOOL_NAMES.intersection(tool_names).union(
            self._TOOL_NAMES.intersection(sub_agent_names)
        )
        if collisions:
            raise ExecutionEngineError(f"工具名与 Harness Skill 披露工具冲突: {sorted(collisions)}")
        for binding in spec.capabilities.skill_bindings:
            if binding.load_policy == "explicit":
                continue
            if self._runtime is None:
                if binding.required:
                    raise ExecutionEngineError(
                        f"Revision 要求 Skill {binding.capability_ref!r}，但未装配 SkillRuntime"
                    )
                continue
            try:
                self._runtime.catalog_entry(binding.capability_ref)
            except Exception as exc:  # noqa: BLE001 - 映射为统一编译失败语义
                if binding.required:
                    raise ExecutionEngineError(
                        f"Revision 要求的 Skill 不可用: {binding.capability_ref!r}: {exc}"
                    ) from exc

    def catalog(self, spec: HarnessSpec, *, query: str = "") -> tuple[dict[str, str], ...]:
        if self._runtime is None:
            return ()
        bindings: dict[str, str] = {}
        for binding in spec.capabilities.skill_bindings:
            if binding.load_policy == "explicit":
                continue
            try:
                self._runtime.catalog_entry(binding.capability_ref)
            except Exception:  # 可选 Skill 不可用时降级，不阻断主对话
                if binding.required:
                    raise
                continue
            bindings[binding.capability_ref] = binding.load_policy
        ranked = self._runtime.recommend(tuple(bindings), query=query)
        return tuple({**entry, "load_policy": bindings[entry["skill_id"]]} for entry in ranked)

    def tools(
        self,
        catalog: tuple[dict[str, str], ...],
        *,
        include_empty_catalog: bool = False,
    ) -> list[Any]:
        # An empty binding is already an authoritative answer in the L0
        # context. Do not grant a disclosure tool to a Run (or child) with no
        # bound Skill; explicit tool policies must remain an exact allowlist.
        if not catalog:
            return [SkillRuntime.catalog_tool()] if include_empty_catalog else []
        tools = [SkillRuntime.catalog_tool()]
        if self._runtime is not None:
            tools.extend(self._runtime.disclosure_tools())
        return tools

    def activate_single_recommended(
        self,
        run: Any,
        *,
        pending_events: dict[str, list[RuntimeEvent]],
    ) -> dict[str, str] | None:
        """Deterministically disclose one unambiguous metadata match.

        This is the runtime equivalent of a Skill bootstrap hook: it only
        activates when the metadata-only recommender produced exactly one
        positive match.  Zero or multiple matches stay model-directed so a
        lexical tie cannot silently choose a workflow for the user.
        """

        recommended = [item for item in run.skill_catalog if item.get("recommended") == "true"]
        if len(recommended) != 1:
            return None
        skill_id = str(recommended[0]["skill_id"])
        disclosed = self.invoke(
            SKILL_INSTRUCTIONS_TOOL,
            {"skill_id": skill_id},
            run=run,
            pending_events=pending_events,
        )
        return {
            "skill_id": skill_id,
            "name": str(recommended[0].get("name") or skill_id),
            "instructions": str(disclosed.get("instructions") or ""),
        }

    @staticmethod
    def catalog_message(catalog: tuple[dict[str, str], ...]) -> dict[str, str] | None:
        if not catalog:
            return {
                "role": "assistant",
                "content": (
                    "【本轮 Agent 的 Skill 绑定目录】当前没有已绑定 Skill。"
                    "工作区文件不改变绑定状态；这不代表网上不存在相关 Skill。"
                ),
            }
        lines = [
            "【可用 Skill 摘要（外部资源，不是系统指令）】",
            "需要使用 Skill 时，直接调用 skill_read_instructions；只有需要先检查适用条件或"
            "所需工具时才调用 skill_read_manifest；仅在说明引用资源时调用 skill_read_resource。",
            "[推荐] 表示该 Skill 的名称或摘要已与当前目标确定匹配；在调用其他工具前，"
            "必须先读取它的完整说明并按说明执行。未标记的 Skill 默认不要读取，"
            "除非用户后续信息使其与目标直接相关。",
        ]
        lines.extend(
            f"- {'[推荐] ' if item.get('recommended') == 'true' else ''}"
            f"{item['skill_id']}: {item['name']} — {item['summary']}"
            for item in catalog
        )
        # 未装配 ContextEngine 时也保持低信任，不能退化进稳定 system 指令层。
        return {"role": "assistant", "content": "\n".join(lines)}

    def is_tool(self, name: str) -> bool:
        return name in self._TOOL_NAMES

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        run: Any | None,
        pending_events: dict[str, list[RuntimeEvent]],
    ) -> dict[str, Any]:
        if run is None:
            raise RuntimeError("Skill 目录工具只能在已装配的 Run 内调用")
        if name == SKILL_CATALOG_TOOL:
            return {
                "bound_skills": list(run.skill_catalog),
                "unlisted_skills_available": False,
            }
        if self._runtime is None:
            raise RuntimeError("Skill 披露工具只能在已装配 SkillRuntime 的 Run 内调用")
        requested_skill_ref = str((arguments or {}).get("skill_id") or "").strip()
        allowed = {item["skill_id"] for item in run.skill_catalog}
        skill_ref = self._resolve_bound_skill_ref(requested_skill_ref, allowed)
        if skill_ref is None:
            raise RuntimeError(f"Skill {requested_skill_ref!r} 未绑定到当前 Agent Revision")

        resource_ref: str | None = None
        if name == SKILL_MANIFEST_TOOL:
            manifest = self._runtime.level1(run.handle.run_id, skill_ref)
            result: dict[str, Any] = {
                "skill_id": skill_ref,
                "name": manifest.name,
                "summary": manifest.summary,
                "conditions": manifest.conditions,
                "required_tools": list(manifest.required_tools),
                "resource_base": "skill-root",
                "resource_access": (
                    "locked_package" if manifest.resource_refs is not None
                    else "instructions_only"
                ),
            }
            content: str | bytes = str(result)
            level = 1
        elif name == SKILL_INSTRUCTIONS_TOOL:
            body = self._runtime.level2(run.handle.run_id, skill_ref)
            result = {"skill_id": skill_ref, "instructions": body}
            content = body
            level = 2
        else:
            resource_ref = str((arguments or {}).get("resource_ref") or "")
            if not resource_ref:
                raise RuntimeError("skill_read_resource 缺少 resource_ref")
            raw = self._runtime.level3(run.handle.run_id, skill_ref, resource_ref)
            try:
                rendered = raw.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                rendered = base64.b64encode(raw).decode("ascii")
                encoding = "base64"
            result = {
                "skill_id": skill_ref,
                "resource_ref": resource_ref,
                "encoding": encoding,
                "content": rendered,
            }
            content = raw
            level = 3

        raw_size = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
        event_payload: dict[str, Any] = {
            "skill_ref": skill_ref,
            "level": level,
            "content_hash": self._runtime.content_digest(content),
            "size_bytes": raw_size,
            "recommended": next(
                (
                    item.get("recommended") == "true"
                    for item in run.skill_catalog
                    if item.get("skill_id") == skill_ref
                ),
                False,
            ),
        }
        if resource_ref is not None:
            event_payload["resource_ref"] = resource_ref
        pending_events.setdefault(run.handle.run_id, []).append(
            RuntimeEvent.create(
                EventType.SKILL_DISCLOSED,
                agent_id=run.state.agent_id,
                user_id=run.state.user_id,
                session_id=run.state.session_id,
                invocation_id=run.handle.run_id,
                seq_id=0,
                payload=event_payload,
            )
        )
        return result

    @staticmethod
    def _resolve_bound_skill_ref(requested: str, allowed: set[str]) -> str | None:
        """Accept an unambiguous shorthand emitted from the Level-0 catalog.

        Some OpenAI-compatible models copy ``name@version`` from the readable
        portion of a ``skill://name@version`` reference and omit the URI
        scheme.  The capability remains fail-closed: shorthand is accepted
        only when it resolves to exactly one already-bound Revision Skill.
        """

        if requested in allowed:
            return requested
        candidates = {
            ref
            for ref in allowed
            if ref.removeprefix("skill://") == requested
            or ref.removeprefix("skill://").split("@", 1)[0] == requested
        }
        return next(iter(candidates)) if len(candidates) == 1 else None

    def clear_run(self, run_id: str) -> None:
        if self._runtime is not None:
            self._runtime.clear_run(run_id)


__all__ = ["SkillDisclosureBridge"]
