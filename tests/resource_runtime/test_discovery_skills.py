from dataclasses import replace

import pytest

from ksadk.resource_runtime.contracts import ResourceConfig
from ksadk.resource_runtime.discovery_skills import DiscoverySkillService
from ksadk.skills.models import SkillListResponse
from ksadk.skills.package_store import SkillPackageError
from tests.resource_runtime.test_skill_packages import archive, ref
from tests.resource_runtime.test_studio_resource_config import binding


class CatalogClient:
    allow_env_fallback = False

    def __init__(self):
        self.content = archive()
        self.refs = [ref(self.content)]
        self.space = "resource-a"
        self.partial = False
        self.downloads = []
        self.queries = []

    def list_skills_by_space_id(self, space):
        self.queries.append(space)
        return SkillListResponse("request", self.space, "Space", self.refs, truncated=self.partial)

    def download_skill_archive(self, selected):
        self.downloads.append(selected)
        return self.content


def service(tmp_path):
    config = dict(binding("skill-space").config)
    config["selectionMode"] = "discovery"
    client = CatalogClient()
    return DiscoverySkillService(
        ResourceConfig.model_validate(config),
        client,
        cache_directory=tmp_path,
        namespace="test-activation",
    ), client


def test_selected_version_survives_catalog_upgrade(tmp_path):
    bound, client = service(tmp_path)
    original = bound.load_skill({"skillId": "skill-a"})
    client.content = archive({"SKILL.md": "new version"})
    client.refs = [replace(ref(client.content), version_id="version-b")]
    assert bound.load_skill({"skillId": "skill-a"}) == original
    assert bound.list_skills({})["items"][0]["versionId"] == "version-a"
    assert len(client.downloads) == 1
    assert set(client.queries) == {"resource-a"}


def test_search_reaches_later_catalog_entries_and_reports_partial(tmp_path):
    bound, client = service(tmp_path)
    client.refs = [
        replace(
            ref(client.content),
            skill_id=f"skill-{i}",
            name=f"item-{i}",
            description="needle" if i == 140 else "",
        )
        for i in range(150)
    ]
    assert bound.list_skills({})["truncated"]
    assert bound.search_skills({"query": "needle"})["items"][0]["skillId"] == "skill-140"
    assert bound.load_skill({"skillId": "skill-140"})["content"] == "original"
    client.partial = True
    assert bound.search_skills({"query": "needle"})["truncated"]


@pytest.mark.parametrize("case", ["space", "unknown", "hash", "path", "override"])
def test_invalid_selection_cannot_publish_package(tmp_path, case):
    bound, client = service(tmp_path)
    arguments = {"skillId": "skill-a", "path": "SKILL.md"}
    if case == "space":
        client.space = "other-space"
    elif case == "unknown":
        arguments["skillId"] = "not-listed"
    elif case == "hash":
        client.content = archive({"SKILL.md": "tampered"})
    elif case == "path":
        arguments["path"] = "../outside"
    else:
        arguments["spaceId"] = "other-space"
    with pytest.raises((ValueError, SkillPackageError)):
        bound.read_resource(arguments)
    assert not bound._selected
    if case != "hash":
        assert not client.downloads


def test_selected_cache_tampering_fails_without_redownload(tmp_path):
    bound, client = service(tmp_path)
    bound.load_skill({"skillId": "skill-a"})
    package = bound.store.get_cached(client.refs[0])
    (package.root_dir / "SKILL.md").write_text("tampered")
    with pytest.raises(SkillPackageError, match="integrity"):
        bound.load_skill({"skillId": "skill-a"})
    assert len(client.downloads) == 1


def test_execution_requires_admitted_runtime(tmp_path):
    bound, _ = service(tmp_path)
    with pytest.raises(ValueError, match="admitted runtime adapter"):
        bound.execute({})
