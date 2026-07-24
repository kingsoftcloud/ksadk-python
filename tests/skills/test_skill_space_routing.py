from __future__ import annotations

import pytest

from ksadk.skills.models import SkillListResponse
from ksadk.toolsets import skills as skills_toolset


def _skill(skill_id: str, name: str, description: str = "", tags=(), aliases=()) -> dict:
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
    """按 space 返回不同 skill 的 fake SkillServiceClient。"""

    def __init__(self, spaces_skills: dict, spaces_meta: list | None = None):
        self._spaces_skills = spaces_skills
        self._spaces_meta = spaces_meta if spaces_meta is not None else []

    def list_skills_by_space_id(self, space_id: str) -> SkillListResponse:
        return SkillListResponse.from_payload(
            {"Data": {"SkillSpaceId": space_id, "Skills": self._spaces_skills.get(space_id, [])}},
            space_id=space_id,
        )

    def list_available_premade_skills(self) -> SkillListResponse:
        return SkillListResponse.from_payload(
            {"Data": {"SkillSpaceId": "public", "Skills": self._spaces_skills.get("public", [])}},
            space_id="public",
            space_name="Public Skills",
        )

    def list_skill_spaces(self, **_kwargs) -> dict:
        return {"Data": {"SkillSpaces": self._spaces_meta}}


@pytest.fixture
def multi_space_env(monkeypatch):
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-a,ss-b")
    monkeypatch.setenv("KSADK_PUBLIC_SKILL_SPACE_IDS", "ss-pub")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_TOKEN", "")


def _patch_client(monkeypatch, client: _FakeClient):
    monkeypatch.setattr(skills_toolset, "_skill_service_client", lambda: client)


def test_parse_skill_spaces_accepts_multiple_field_namings():
    payload = {
        "Data": {
            "SkillSpaces": [
                {"SkillSpaceId": "ss-1", "SkillSpaceName": "office", "Description": "办公"},
                {"space_id": "ss-2", "space_name": "dev", "description": "开发"},
                {"SpaceId": "ss-3", "SpaceName": "ops", "Description": "运维"},
            ]
        }
    }
    spaces = skills_toolset._parse_skill_spaces(payload)
    assert [s["space_id"] for s in spaces] == ["ss-1", "ss-2", "ss-3"]
    assert spaces[0]["space_name"] == "office"
    assert spaces[1]["description"] == "开发"


def test_list_skill_spaces_marks_configured(multi_space_env, monkeypatch):
    client = _FakeClient(
        {},
        spaces_meta=[
            {"SkillSpaceId": "ss-a", "SkillSpaceName": "A 空间", "Description": "第一个"},
            {"SkillSpaceId": "ss-b", "SkillSpaceName": "B 空间", "Description": "第二个"},
            {"SkillSpaceId": "ss-other", "SkillSpaceName": "其他", "Description": "未配置"},
        ],
    )
    _patch_client(monkeypatch, client)

    result = skills_toolset.list_skill_spaces()

    assert result["ok"] is True
    assert result["configured_space_ids"] == ["ss-a", "ss-b", "ss-pub"]
    by_id = {s["space_id"]: s for s in result["spaces"]}
    assert by_id["ss-a"]["configured"] is True
    assert by_id["ss-b"]["configured"] is True
    assert by_id["ss-other"]["configured"] is False
    assert by_id["ss-a"]["description"] == "第一个"
    assert "usage" in result


def test_list_skill_spaces_falls_back_to_configured_when_api_empty(multi_space_env, monkeypatch):
    # 服务返回空 space 列表(不支持 ListSkillSpaces)时,退化为已配置 space id。
    _patch_client(monkeypatch, _FakeClient({}, spaces_meta=[]))

    result = skills_toolset.list_skill_spaces()

    assert result["ok"] is True
    assert [s["space_id"] for s in result["spaces"]] == ["ss-a", "ss-b", "ss-pub"]
    assert all(s["configured"] for s in result["spaces"])


def test_search_skills_aggregates_all_spaces_and_tags_space_id(multi_space_env, monkeypatch):
    client = _FakeClient(
        {
            "ss-a": [_skill("sk-1", "web-artifacts-builder", "构建 web artifact", tags=("web",))],
            "ss-b": [_skill("sk-2", "data-analyzer", "数据分析", tags=("data",))],
            "public": [_skill("sk-3", "web-artifacts-builder", "public 版", tags=("web",))],
        }
    )
    _patch_client(monkeypatch, client)

    result = skills_toolset.search_skills("web")

    assert result["ok"] is True
    assert result["space_id"] is None
    names = {r["name"] for r in result["results"]}
    assert "web-artifacts-builder" in names
    # 结果回填了来源 space_id
    for r in result["results"]:
        assert "space_id" in r


def test_search_skills_filters_to_one_space(multi_space_env, monkeypatch):
    client = _FakeClient(
        {
            "ss-a": [_skill("sk-1", "web-artifacts-builder", "构建 web artifact", tags=("web",))],
            "ss-b": [_skill("sk-2", "web-scraper", "B 空间 web 抓取", tags=("web",))],
        }
    )
    _patch_client(monkeypatch, client)

    result = skills_toolset.search_skills("web", space_id="ss-b")

    assert result["ok"] is True
    assert result["space_id"] == "ss-b"
    names = {r["name"] for r in result["results"]}
    assert names == {"web-scraper"}
    assert all(r["space_id"] == "ss-b" for r in result["results"])


def test_load_skill_resolves_same_name_by_space(multi_space_env, monkeypatch, tmp_path):
    client = _FakeClient(
        {
            "ss-a": [_skill("sk-a", "common-skill", "A 空间的 common")],
            "ss-b": [_skill("sk-b", "common-skill", "B 空间的 common")],
        }
    )
    _patch_client(monkeypatch, client)

    # 不下载真实包:patch 下载与 loader,只验证按 space 命中。
    class _Pkg:
        root_dir = tmp_path
        cache_hit = True

    class _Local:
        def __init__(self, name, description):
            self.name = name
            self.description = description
            self.root_dir = tmp_path
            self.body = "instructions"

    monkeypatch.setattr(skills_toolset, "_get_or_download_package", lambda c, s: _Pkg())
    monkeypatch.setattr(
        skills_toolset,
        "load_local_skill",
        lambda _root: _Local("common-skill", "desc"),
    )

    result_b = skills_toolset.load_skill("common-skill", space_id="ss-b")
    assert result_b["ok"] is True
    assert result_b["space_id"] == "ss-b"
    assert result_b["skill_id"] == "sk-b"

    result_a = skills_toolset.load_skill("common-skill", space_id="ss-a")
    assert result_a["space_id"] == "ss-a"
    assert result_a["skill_id"] == "sk-a"

    # 不带 space_id 时按配置顺序取第一个(user space ss-a 在前)。
    result_default = skills_toolset.load_skill("common-skill")
    assert result_default["space_id"] == "ss-a"


def test_list_skills_filters_to_one_space(multi_space_env, monkeypatch):
    client = _FakeClient(
        {
            "ss-a": [_skill("sk-1", "skill-a")],
            "ss-b": [_skill("sk-2", "skill-b"), _skill("sk-3", "skill-b2")],
        }
    )
    _patch_client(monkeypatch, client)

    result = skills_toolset.list_skills(space_id="ss-b")

    assert result["ok"] is True
    assert result["space_id"] == "ss-b"
    names = {s["name"] for s in result["skills"]}
    assert names == {"skill-b", "skill-b2"}
    assert all(s["space_id"] == "ss-b" for s in result["skills"])
