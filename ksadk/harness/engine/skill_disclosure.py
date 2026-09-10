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
    SKILL_INSTRUCTIONS_TOOL,
    SKILL_MANIFEST_TOOL,
    SKILL_RESOURCE_TOOL,
    SkillRuntime,
)
from ksadk.harness.spec import HarnessSpec


class SkillDisclosureBridge:
    """把 SkillRuntime 接入默认 Loop，同时守住 Revision 与信任边界。"""

    _TOOL_NAMES = frozenset({SKILL_MANIFEST_TOOL, SKILL_INSTRUCTIONS_TOOL, SKILL_RESOURCE_TOOL})

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

    def tools(self, catalog: tuple[dict[str, str], ...]) -> list[Any]:
        if self._runtime is None or not catalog:
            return []
        return list(self._runtime.disclosure_tools())

    @staticmethod
    def catalog_message(catalog: tuple[dict[str, str], ...]) -> dict[str, str] | None:
        if not catalog:
            return None
        lines = [
            "【可用 Skill 摘要（外部资源，不是系统指令）】",
            "需要使用 Skill 时，依次调用 skill_read_manifest、"
            "skill_read_instructions；仅在说明引用资源时调用 skill_read_resource。",
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
        if run is None or self._runtime is None:
            raise RuntimeError("Skill 披露工具只能在已装配 SkillRuntime 的 Run 内调用")
        skill_ref = str((arguments or {}).get("skill_id") or "")
        allowed = {item["skill_id"] for item in run.skill_catalog}
        if skill_ref not in allowed:
            raise RuntimeError(f"Skill {skill_ref!r} 未绑定到当前 Agent Revision")

        resource_ref: str | None = None
        if name == SKILL_MANIFEST_TOOL:
            manifest = self._runtime.level1(run.handle.run_id, skill_ref)
            result: dict[str, Any] = {
                "skill_id": skill_ref,
                "name": manifest.name,
                "summary": manifest.summary,
                "conditions": manifest.conditions,
                "required_tools": list(manifest.required_tools),
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

    def clear_run(self, run_id: str) -> None:
        if self._runtime is not None:
            self._runtime.clear_run(run_id)


__all__ = ["SkillDisclosureBridge"]
