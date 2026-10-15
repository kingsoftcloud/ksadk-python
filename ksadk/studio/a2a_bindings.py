"""Studio A2A catalog projection, binding freeze, and drift validation."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator

from ksadk.a2a.ids import require_a2a_resource_id
from ksadk.api import AgentEngineAPIError
from ksadk.studio.contracts import ContractModel, RemoteAgentBinding
from ksadk.studio.errors import StudioError

_PREFIXES = {
    "space": "a2a-space-",
    "agent": "a2a-agent-",
    "version": "a2a-version-",
}


def require_execution_policy(value: str) -> None:
    from ksadk.a2a.policy_refs import resolve_policy_ref
    try:
        resolve_policy_ref(value)
    except ValueError as exc:
        raise StudioError("A2A_POLICY_REF_UNSUPPORTED", "A2A_POLICY_REF_UNSUPPORTED: 未知执行策略引用", status_code=422, field="policyRef") from exc


def bindings_digest(bindings: Sequence[RemoteAgentBinding]) -> str | None:
    from ksadk.studio.capabilities import canonical_json, sha256_digest

    if not bindings:
        return None
    payload = [RemoteAgentBinding.model_validate(binding.model_dump()).model_dump(by_alias=True, mode="json")
               for binding in sorted(bindings, key=lambda item: item.binding_id)]
    return sha256_digest(canonical_json(payload))


class A2ABindingFreezeRequest(ContractModel):
    binding_id: str | None = Field(
        default=None,
        pattern=r"^a2a-binding-[0-9a-f]{32}$",
    )
    space_id: str
    target_agent_id: str
    expected_version_id: str | None = None
    local_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    exposure: Literal["native", "tool", "auto"] = "auto"
    policy_ref: str = Field(
        min_length=10,
        max_length=256,
        pattern=r"^policy://[A-Za-z0-9][A-Za-z0-9._/-]*$",
    )
    follow_current: bool = False

    @field_validator("space_id", "target_agent_id", "expected_version_id")
    @classmethod
    def validate_resource_id(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        prefix = {
            "space_id": _PREFIXES["space"],
            "target_agent_id": _PREFIXES["agent"],
            "expected_version_id": _PREFIXES["version"],
        }[str(info.field_name)]
        return require_a2a_resource_id(value, prefix, field_name=str(info.field_name))

    @model_validator(mode="after")
    def validate_version_selection(self) -> "A2ABindingFreezeRequest":
        if self.follow_current == (self.expected_version_id is not None):
            raise ValueError("exactly one of expectedVersionId or followCurrent=true is required")
        return self


class A2ACatalogService:
    """Expose only safe catalog fields from process-authenticated cloud Actions."""

    def __init__(self, gateway: Callable[[], Any]) -> None:
        self._gateway = gateway

    async def list_spaces(self, *, page: int, size: int) -> dict[str, Any]:
        data = await self._call("list_a2a_spaces", page=page, size=size)
        items = []
        for raw in self._list_field(data, "spaces"):
            items.append(
                {
                    "spaceId": self._catalog_id("space", raw),
                    "name": str(raw.get("name") or "").strip(),
                    "status": str(raw.get("status") or "unknown").strip().lower(),
                    "memberCount": self._int_field(raw, "agent_count", default=0),
                }
            )
        return {
            "items": items,
            "total": self._int_field(data, "total_count", default=len(items)),
            "page": self._int_field(data, "page_number", default=page),
            "size": self._int_field(data, "page_size", default=size),
        }

    async def list_space_agents(self, space_id: str) -> dict[str, Any]:
        canonical_space = self._canonical_input("space", space_id)
        space = await self._call("get_a2a_space", canonical_space)
        if self._catalog_id("space", space) != canonical_space:
            raise self._protocol_error("space response identity mismatch")
        if str(space.get("status") or "").lower() != "available":
            raise StudioError("A2A_BINDING_TARGET_UNAVAILABLE", "所选 A2A Space 已不可用", status_code=409,
                              details={"spaceId": canonical_space})
        data = await self._call(
            "list_a2a_space_available_agents",
            self._wire_id("space", canonical_space),
        )
        items = []
        for raw in self._list_field(data, "agents"):
            if not self._is_callable(raw):
                continue
            items.append(self._agent_item(raw, space_id=canonical_space))
        return {"items": items}

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        canonical_agent = self._canonical_input("agent", agent_id)
        raw = await self._call(
            "get_a2a_agent",
            self._wire_id("agent", canonical_agent),
        )
        if not isinstance(raw, dict):
            raise self._protocol_error("agent detail is not an object")
        return {
            "targetAgentId": self._catalog_id("agent", raw),
            "versionId": self._canonical("version", raw.get("latest_version_id")),
            "name": str(raw.get("name") or "").strip(),
            "status": str(raw.get("status") or "unknown").strip().lower(),
            "callable": self._is_callable(raw),
        }

    async def list_agent_versions(
        self,
        agent_id: str,
        *,
        page: int,
        size: int,
    ) -> dict[str, Any]:
        canonical_agent = self._canonical_input("agent", agent_id)
        data = await self._call(
            "list_a2a_agent_versions",
            self._wire_id("agent", canonical_agent),
            page=page,
            size=size,
        )
        items = []
        for raw in self._list_field(data, "versions"):
            items.append(
                {
                    "versionId": self._canonical("version", raw.get("version_id")),
                    "sequence": self._int_field(raw, "sequence", default=0),
                    "agentVersion": str(raw.get("agent_version") or "").strip(),
                    "cardSha256": self._sha256_field(raw, "card_sha256"),
                }
            )
        return {
            "items": items,
            "total": self._int_field(data, "total_count", default=len(items)),
            "page": page,
            "size": size,
        }

    async def freeze_binding(
        self,
        request: A2ABindingFreezeRequest,
    ) -> RemoteAgentBinding:
        require_execution_policy(request.policy_ref)
        available = await self.list_space_agents(request.space_id)
        current = next(
            (
                item
                for item in available["items"]
                if item["targetAgentId"] == request.target_agent_id
            ),
            None,
        )
        if current is None:
            raise StudioError(
                "A2A_BINDING_TARGET_UNAVAILABLE",
                "远端 Agent 不在所选 Space 的可调用成员中",
                status_code=422,
                details={
                    "spaceId": request.space_id,
                    "targetAgentId": request.target_agent_id,
                },
            )
        resolved_version = str(current["versionId"])
        expected_version = request.expected_version_id or resolved_version
        self._require_version(
            binding_id=request.binding_id,
            expected=expected_version,
            actual=resolved_version,
        )
        return RemoteAgentBinding.freeze(
            binding_id=request.binding_id,
            space_id=request.space_id,
            target_agent_id=request.target_agent_id,
            expected_version_id=expected_version,
            local_name=request.local_name,
            exposure=request.exposure,
            policy_ref=request.policy_ref,
        )

    async def validate_bindings(
        self,
        bindings: Sequence[RemoteAgentBinding],
    ) -> None:
        by_space: dict[str, dict[str, dict[str, Any]]] = {}
        for candidate in bindings:
            binding = RemoteAgentBinding.model_validate(candidate.model_dump())
            require_execution_policy(binding.policy_ref)
            if binding.space_id not in by_space:
                listing = await self.list_space_agents(binding.space_id)
                by_space[binding.space_id] = {
                    item["targetAgentId"]: item for item in listing["items"]
                }
            current = by_space[binding.space_id].get(binding.target_agent_id)
            if current is None:
                raise StudioError(
                    "A2A_BINDING_TARGET_UNAVAILABLE",
                    "远端 Agent 已不再是可调用成员",
                    status_code=409,
                    details={"bindingId": binding.binding_id},
                )
            self._require_version(
                binding_id=binding.binding_id,
                expected=binding.expected_version_id,
                actual=str(current["versionId"]),
            )

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            target = getattr(self._gateway(), method)
            result = await target(*args, **kwargs)
        except AgentEngineAPIError as exc:
            if exc.code in {401, 403}:
                raise StudioError(
                    "A2A_CATALOG_FORBIDDEN",
                    "当前账号无权读取 A2A 目录",
                    status_code=403,
                    details={"operation": method},
                ) from exc
            raise StudioError(
                "A2A_CATALOG_UNAVAILABLE",
                "远端 A2A 目录暂不可用",
                status_code=502,
                details={"operation": method},
            ) from exc
        except StudioError as exc:
            if exc.code == "A2A_CATALOG_UNAVAILABLE":
                raise StudioError(
                    exc.code,
                    "当前未配置可用的云端签名账号，不能读取 A2A 目录",
                    status_code=exc.status_code,
                ) from exc
            raise StudioError(
                "A2A_CATALOG_UNAVAILABLE",
                "远端 A2A 目录暂不可用",
                status_code=502,
                details={"operation": method},
            ) from exc
        except Exception as exc:
            raise StudioError(
                "A2A_CATALOG_UNAVAILABLE",
                "远端 A2A 目录暂不可用",
                status_code=502,
                details={"operation": method},
            ) from exc
        if not isinstance(result, dict):
            raise self._protocol_error("catalog Action returned a non-object")
        return result

    def _catalog_id(self, kind: str, raw: Mapping[str, Any]) -> str:
        # AgentEngineClient turns A2ASpaceId into a2_a_space_id. Some
        # gateways already return domain snake_case; accept both explicitly.
        names = (f"a2a_{kind}_id", f"a2_a_{kind}_id")
        values = [self._canonical(kind, raw[name]) for name in names if name in raw]
        if not values:
            return self._canonical(kind, None)
        if len(set(values)) != 1:
            raise self._protocol_error("catalog resource identity aliases conflict")
        return values[0]

    def _canonical(self, kind: str, value: Any) -> str:
        try:
            return require_a2a_resource_id(
                str(value or "").strip(), _PREFIXES[kind], field_name=kind,
            )
        except ValueError as exc:
            raise StudioError(
                "A2A_CATALOG_RESOURCE_ID_UNMAPPED",
                "A2A 目录返回了无效的资源标识",
                status_code=502,
                details={"kind": kind},
            ) from exc

    def _canonical_input(self, kind: str, value: Any) -> str:
        try:
            return require_a2a_resource_id(
                str(value or "").strip(), _PREFIXES[kind], field_name=kind,
            )
        except ValueError as exc:
            raise StudioError(
                "A2A_CATALOG_RESOURCE_ID_INVALID",
                "A2A 资源标识无效",
                status_code=422,
                details={"kind": kind},
            ) from exc

    def _wire_id(self, kind: str, canonical: str) -> str:
        return canonical

    @classmethod
    def _list_field(
        cls,
        payload: Mapping[str, Any],
        field: str,
    ) -> list[dict[str, Any]]:
        raw = payload.get(field)
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise cls._protocol_error("catalog collection field is invalid")
        return raw

    @classmethod
    def _int_field(
        cls,
        payload: Mapping[str, Any],
        field: str,
        *,
        default: int,
    ) -> int:
        raw = payload.get(field)
        if raw is None:
            return default
        if isinstance(raw, bool):
            raise cls._protocol_error("catalog numeric field is invalid")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise cls._protocol_error("catalog numeric field is invalid") from exc
        if value < 0:
            raise cls._protocol_error("catalog numeric field is invalid")
        return value

    @classmethod
    def _sha256_field(cls, payload: Mapping[str, Any], field: str) -> str:
        value = str(payload.get(field) or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise cls._protocol_error("catalog digest field is invalid")
        return value

    def _agent_item(self, raw: dict[str, Any], *, space_id: str) -> dict[str, Any]:
        card = raw.get("agent_card") if isinstance(raw.get("agent_card"), dict) else {}
        return {
            "spaceId": space_id,
            "targetAgentId": self._catalog_id("agent", raw),
            "versionId": self._canonical("version", raw.get("version_id")),
            "name": str(card.get("name") or "").strip(),
            "description": str(card.get("description") or "").strip(),
            "cardSha256": self._sha256_field(raw, "card_sha256"),
            "callable": True,
        }

    @staticmethod
    def _is_callable(raw: Mapping[str, Any]) -> bool:
        return (
            str(raw.get("status") or "").strip().lower() == "available"
            and str(raw.get("invocation_status") or "").strip().lower() == "available"
        )

    @staticmethod
    def _require_version(*, binding_id: str | None, expected: str, actual: str) -> None:
        if expected != actual:
            details = {
                "bindingId": binding_id,
                "expectedVersionId": expected,
                "actualVersionId": actual,
            }
            if binding_id is None:
                details.pop("bindingId")
            raise StudioError(
                "A2A_BINDING_VERSION_DRIFT",
                "远端 Agent 当前版本与冻结 binding 不一致",
                status_code=409,
                details=details,
            )

    @staticmethod
    def _protocol_error(reason: str) -> StudioError:
        return StudioError(
            "A2A_CATALOG_PROTOCOL_INVALID",
            "远端 A2A 目录响应不符合冻结合同",
            status_code=502,
            details={"reason": reason},
        )


__all__ = ["A2ABindingFreezeRequest", "A2ACatalogService"]
