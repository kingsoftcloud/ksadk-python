from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContentHash:
    algorithm: str
    value: str

    @classmethod
    def parse(cls, raw: str | None) -> "ContentHash | None":
        normalized = str(raw or "").strip()
        if not normalized:
            return None
        if ":" not in normalized:
            return cls("sha256", normalized.lower())
        algorithm, value = normalized.split(":", 1)
        return cls(algorithm.strip().lower(), value.strip().lower())

    def render(self) -> str:
        return f"{self.algorithm}:{self.value}"


@dataclass(frozen=True)
class SkillRef:
    skill_id: str
    version_id: str
    version: str
    name: str
    description: str = ""
    status: str = ""
    content_hash: ContentHash | None = None
    archive_uri: str = ""
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    input_schema: dict[str, Any] | None = None
    runtime_requirements: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SkillRef":
        return cls(
            skill_id=str(payload.get("SkillId") or payload.get("skill_id") or ""),
            version_id=str(payload.get("VersionId") or payload.get("version_id") or ""),
            version=str(payload.get("Version") or payload.get("version") or ""),
            name=str(payload.get("Name") or payload.get("name") or ""),
            description=str(payload.get("Description") or payload.get("description") or ""),
            status=str(payload.get("Status") or payload.get("status") or ""),
            content_hash=ContentHash.parse(payload.get("ContentHash") or payload.get("content_hash")),
            archive_uri=str(payload.get("ArchiveUri") or payload.get("archive_uri") or ""),
            aliases=_string_tuple(payload.get("Aliases") or payload.get("aliases")),
            tags=_string_tuple(payload.get("Tags") or payload.get("tags")),
            examples=_string_tuple(payload.get("Examples") or payload.get("examples")),
            input_schema=_dict_or_none(payload.get("InputSchema") or payload.get("input_schema")),
            runtime_requirements=_dict_or_none(
                payload.get("RuntimeRequirements") or payload.get("runtime_requirements")
            ),
        )

    @property
    def cache_key(self) -> str:
        identity = self.skill_id or self.name
        version_key = self.version_id or self.version
        if not version_key and self.content_hash:
            version_key = self.content_hash.value
        return "__".join(part.replace("/", "_").replace(":", "_") for part in (identity, version_key) if part)

    @property
    def is_active(self) -> bool:
        return not self.status or self.status.lower() in {"active", "available", "enabled"}


@dataclass(frozen=True)
class SkillListResponse:
    request_id: str
    space_id: str
    space_name: str
    skills: list[SkillRef]
    code: int = 0
    message: str = ""

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        space_id: str = "",
        space_name: str = "",
    ) -> "SkillListResponse":
        data = payload.get("Data") or payload.get("data") or {}
        skills_payload = data.get("Skills") or data.get("skills") or data.get("Items") or data.get("items") or []
        return cls(
            code=int(payload.get("Code") or payload.get("code") or 0),
            message=str(payload.get("Message") or payload.get("message") or ""),
            request_id=str(payload.get("RequestId") or payload.get("request_id") or ""),
            space_id=str(data.get("SkillSpaceId") or data.get("skill_space_id") or space_id),
            space_name=str(data.get("SkillSpaceName") or data.get("skill_space_name") or space_name),
            skills=[SkillRef.from_payload(item) for item in skills_payload if isinstance(item, dict)],
        )

    def active_skills(self) -> list[SkillRef]:
        return [skill for skill in self.skills if skill.is_active]


def _string_tuple(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        return ()
    return tuple(value for value in (str(item).strip() for item in values) if value)


def _dict_or_none(raw: Any) -> dict[str, Any] | None:
    return dict(raw) if isinstance(raw, dict) else None
