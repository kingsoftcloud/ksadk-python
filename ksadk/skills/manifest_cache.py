"""TTL cache for skill manifests fetched from Skill Service.

Provides a lightweight, thread-safe TTL cache so that repeated tool calls
(list_skills, search_skills) within the same MCP server process do not each
trigger a full HTTP round-trip to the Skill Service.

The cache stores manifest items (name/description/version/tags/space_id) per
skill space.  When the TTL expires the next call re-fetches fresh data.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from ksadk.skills.mcp_server._routing import skill_routing_instructions
from ksadk.skills.service_client import SkillServiceClient
from ksadk.skills.service_env import (
    public_skill_space_ids,
    resolve_skill_service_url,
    user_skill_space_ids,
)

_DEFAULT_TTL = 60
_DEFAULT_LIMIT = 30


def _ttl_seconds() -> int:
    raw = os.environ.get("KSADK_SKILL_MANIFEST_TTL", "").strip()
    if not raw:
        return _DEFAULT_TTL
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_TTL


def _manifest_limit() -> int:
    raw = os.environ.get("KSADK_SKILL_MANIFEST_LIMIT", "").strip()
    if not raw:
        return _DEFAULT_LIMIT
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_LIMIT


@dataclass
class ManifestItem:
    name: str
    description: str
    version: str
    space_id: str
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "space_id": self.space_id,
        }
        if self.aliases:
            item["aliases"] = ", ".join(self.aliases)
        if self.tags:
            item["tags"] = ", ".join(self.tags)
        return item


@dataclass
class _CacheEntry:
    items: list[ManifestItem]
    fetched_at: float


class ManifestCache:
    """Thread-safe TTL cache for skill manifests."""

    def __init__(self, *, ttl: int | None = None, limit: int | None = None):
        self._ttl = ttl if ttl is not None else _ttl_seconds()
        self._limit = limit if limit is not None else _manifest_limit()
        self._lock = threading.Lock()
        self._cache: dict[str, _CacheEntry] = {}
        self._client: SkillServiceClient | None = None

    def _get_client(self) -> SkillServiceClient:
        if self._client is not None:
            return self._client
        service_url = resolve_skill_service_url(require_spaces=True)
        if not service_url:
            raise ValueError("Skill Service URL is not configured")
        self._client = SkillServiceClient(
            base_url=service_url,
            token=os.environ.get("KSADK_SKILL_SERVICE_TOKEN", ""),
            timeout=float(os.environ.get("KSADK_SKILL_MANIFEST_TIMEOUT", "10")),
        )
        return self._client

    def _fetch_space(self, space_id: str) -> list[ManifestItem]:
        client = self._get_client()
        if space_id == "public":
            listing = client.list_available_premade_skills()
        else:
            listing = client.list_skills_by_space_id(space_id)
        items: list[ManifestItem] = []
        for skill in listing.active_skills():
            if not skill.name:
                continue
            items.append(
                ManifestItem(
                    name=skill.name,
                    description=skill.description or "",
                    version=skill.version or "",
                    space_id=str(listing.space_id or space_id),
                    aliases=tuple(skill.aliases),
                    tags=tuple(skill.tags),
                )
            )
        return items

    def get_space(self, space_id: str, *, force_refresh: bool = False) -> list[ManifestItem]:
        """Return manifest items for a single space, using cached data if fresh."""
        with self._lock:
            entry = self._cache.get(space_id)
            now = time.monotonic()
            if entry is not None and not force_refresh and (now - entry.fetched_at) < self._ttl:
                return list(entry.items)
        try:
            items = self._fetch_space(space_id)
        except Exception:
            with self._lock:
                entry = self._cache.get(space_id)
                if entry is not None:
                    return list(entry.items)
            raise
        if space_id == "public" or space_id in public_skill_space_ids():
            allowlist = {
                name.lower()
                for name in os.environ.get("KSADK_PUBLIC_SKILL_ALLOWLIST", "").split(",")
                if name.strip()
            }
            if allowlist:
                items = [item for item in items if item.name.lower() in allowlist]
        with self._lock:
            self._cache[space_id] = _CacheEntry(items=list(items), fetched_at=time.monotonic())
        return items

    def get_all(self, *, force_refresh: bool = False) -> list[ManifestItem]:
        """Return manifest items for all configured spaces (user + public)."""
        all_items: list[ManifestItem] = []
        seen_names: set[str] = set()
        for space_id in user_skill_space_ids():
            for item in self.get_space(space_id, force_refresh=force_refresh):
                key = item.name.lower()
                if key and key not in seen_names:
                    seen_names.add(key)
                    all_items.append(item)
        for space_id in public_skill_space_ids():
            for item in self.get_space(space_id, force_refresh=force_refresh):
                key = item.name.lower()
                if key and key not in seen_names:
                    seen_names.add(key)
                    all_items.append(item)
        return all_items[: self._limit]

    def _get_all_untruncated(self, *, force_refresh: bool = False) -> list[ManifestItem]:
        """Return all manifest items without the display limit (for search)."""
        all_items: list[ManifestItem] = []
        seen_names: set[str] = set()
        for space_id in user_skill_space_ids():
            for item in self.get_space(space_id, force_refresh=force_refresh):
                key = item.name.lower()
                if key and key not in seen_names:
                    seen_names.add(key)
                    all_items.append(item)
        for space_id in public_skill_space_ids():
            for item in self.get_space(space_id, force_refresh=force_refresh):
                key = item.name.lower()
                if key and key not in seen_names:
                    seen_names.add(key)
                    all_items.append(item)
        return all_items

    def invalidate(self, space_id: str | None = None) -> None:
        with self._lock:
            if space_id is None:
                self._cache.clear()
            else:
                self._cache.pop(space_id, None)

    def search(
        self, query: str, *, max_results: int = 10, space_id: str | None = None
    ) -> list[ManifestItem]:
        query_lower = query.lower().strip()
        if not query_lower:
            return []
        # R4 fix: search the full candidate set, not the truncated get_all() result.
        if space_id:
            source = self.get_space(space_id)
        else:
            source = self._get_all_untruncated()
        results: list[tuple[int, ManifestItem]] = []
        for item in source:
            score = _match_score(item, query_lower)
            if score > 0:
                results.append((score, item))
        results.sort(key=lambda pair: (-pair[0], pair[1].name.lower()))
        return [item for _, item in results[: max(max_results, 1)]]

    def build_instruction_text(self) -> str:
        """Build a system-prompt instruction block from cached manifests."""
        items = self.get_all()
        if not items:
            return ""
        lines = [
            "",
            "## Available Skills (Skill Center)",
            "",
            "Current Skill Center usage (replaces legacy routing notes):",
            skill_routing_instructions(),
            "Available skills:",
            "",
        ]
        for item in items:
            desc = item.description or "No description"
            version_suffix = f" ({item.version})" if item.version else ""
            lines.append(f"- {item.name}{version_suffix}: {desc}")
        lines.append("")
        return "\n".join(lines)


def _match_score(item: ManifestItem, query_lower: str) -> int:
    if item.name and item.name.lower() in query_lower:
        return 90
    if item.name and query_lower in item.name.lower():
        return 85
    for alias in item.aliases:
        if alias.lower() in query_lower or query_lower in alias.lower():
            return 80
    for tag in item.tags:
        tag_lower = tag.lower()
        if tag_lower and (tag_lower in query_lower or query_lower in tag_lower):
            return 65
    desc_lower = item.description.lower()
    if query_lower in desc_lower:
        return 50
    return 0


_global_cache: ManifestCache | None = None
_global_lock = threading.Lock()


def get_manifest_cache() -> ManifestCache:
    global _global_cache
    with _global_lock:
        if _global_cache is None:
            _global_cache = ManifestCache()
        return _global_cache
