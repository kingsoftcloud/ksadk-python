from __future__ import annotations

import time

import pytest

from ksadk.skills.manifest_cache import ManifestCache, ManifestItem
from ksadk.skills.models import SkillListResponse


def _skill(skill_id, name, description="", tags=(), aliases=()):
    return {
        "SkillId": skill_id,
        "VersionId": f"{skill_id}-v1",
        "Version": "v1",
        "Name": name,
        "Description": description,
        "Status": "Active",
        "Tags": list(tags),
        "Aliases": list(aliases),
    }


class _FakeClient:
    def __init__(self, spaces_skills):
        self._spaces_skills = spaces_skills
        self.call_count = 0

    def list_skills_by_space_id(self, space_id):
        self.call_count += 1
        return SkillListResponse.from_payload(
            {"Data": {"SkillSpaceId": space_id, "Skills": self._spaces_skills.get(space_id, [])}},
            space_id=space_id,
        )

    def list_available_premade_skills(self):
        self.call_count += 1
        return SkillListResponse.from_payload(
            {"Data": {"SkillSpaceId": "public", "Skills": self._spaces_skills.get("public", [])}},
            space_id="public",
            space_name="Public Skills",
        )


@pytest.fixture
def cache_env(monkeypatch):
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-a")
    monkeypatch.delenv("KSADK_PUBLIC_SKILL_SPACE_IDS", raising=False)
    monkeypatch.delenv("KSADK_PUBLIC_SKILL_ALLOWLIST", raising=False)


def _patch_client(monkeypatch, cache, client):
    monkeypatch.setattr(cache, "_client", client)


def test_ttl_cache_avoids_repeated_fetch(cache_env, monkeypatch):
    client = _FakeClient({"ss-a": [_skill("sk-1", "web-builder", "Build web pages")]})
    cache = ManifestCache(ttl=60)
    _patch_client(monkeypatch, cache, client)

    items1 = cache.get_space("ss-a")
    assert len(items1) == 1
    assert client.call_count == 1

    items2 = cache.get_space("ss-a")
    assert client.call_count == 1  # no new fetch
    assert items2[0].name == "web-builder"


def test_ttl_expiry_triggers_refetch(cache_env, monkeypatch):
    client = _FakeClient({"ss-a": [_skill("sk-1", "web-builder")]})
    cache = ManifestCache(ttl=1)
    _patch_client(monkeypatch, cache, client)

    cache.get_space("ss-a")
    assert client.call_count == 1

    time.sleep(1.1)
    cache.get_space("ss-a")
    assert client.call_count == 2


def test_search_by_name(cache_env, monkeypatch):
    client = _FakeClient({
        "ss-a": [
            _skill("sk-1", "web-builder", "Build web pages", tags=("web",)),
            _skill("sk-2", "data-analyzer", "Analyze data", tags=("data",)),
        ]
    })
    cache = ManifestCache(ttl=60)
    _patch_client(monkeypatch, cache, client)

    results = cache.search("web")
    assert len(results) == 1
    assert results[0].name == "web-builder"


def test_get_all_deduplicates_across_spaces(cache_env, monkeypatch):
    monkeypatch.setenv("KSADK_PUBLIC_SKILL_SPACE_IDS", "ss-pub")
    client = _FakeClient({
        "ss-a": [_skill("sk-1", "common-skill", "User version")],
        "ss-pub": [_skill("sk-2", "common-skill", "Public version")],
    })
    cache = ManifestCache(ttl=60)
    _patch_client(monkeypatch, cache, client)

    items = cache.get_all()
    assert len(items) == 1  # dedup by name
    assert items[0].space_id == "ss-a"  # user space wins


def test_build_instruction_text_returns_empty_when_no_skills(cache_env, monkeypatch):
    client = _FakeClient({"ss-a": []})
    cache = ManifestCache(ttl=60)
    _patch_client(monkeypatch, cache, client)

    text = cache.build_instruction_text()
    assert text == ""


def test_build_instruction_text_includes_skill_names(cache_env, monkeypatch):
    client = _FakeClient({"ss-a": [_skill("sk-1", "web-builder", "Build web pages")]})
    cache = ManifestCache(ttl=60)
    _patch_client(monkeypatch, cache, client)

    text = cache.build_instruction_text()
    assert "web-builder" in text
    assert "Build web pages" in text


def test_invalidate_clears_cache(cache_env, monkeypatch):
    client = _FakeClient({"ss-a": [_skill("sk-1", "web-builder")]})
    cache = ManifestCache(ttl=60)
    _patch_client(monkeypatch, cache, client)

    cache.get_space("ss-a")
    assert client.call_count == 1

    cache.invalidate("ss-a")
    cache.get_space("ss-a")
    assert client.call_count == 2
