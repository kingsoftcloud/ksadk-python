from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from ksadk.skills.manifest_cache import ManifestCache, ManifestItem
from ksadk.skills.mcp_server import register, server
from ksadk.skills.models import SkillListResponse
from ksadk.toolsets import skills as skill_tools


@pytest.mark.skipif(os.name != "posix", reason="Hosted workspace uses POSIX files")
@pytest.mark.asyncio
async def test_instruction_first_skill_is_loadable_through_mcp_and_advertised_by_hosts(
    monkeypatch, tmp_path
):
    body = "Read references/details.md and run scripts/process.py with your outer agent tools."
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(
            "example/SKILL.md", "---\nname: example\ndescription: Example\n---\n" + body
        )
        package.writestr("example/scripts/process.py", 'print("example")\n')
        package.writestr("example/references/details.md", "Full reference instructions")

    class Client:
        def list_skills_by_space_id(self, space_id):
            return SkillListResponse.from_payload(
                {
                    "Data": {
                        "Skills": [
                            {
                                "SkillId": "example",
                                "VersionId": "v1",
                                "Version": "1",
                                "Name": "example",
                                "Status": "Active",
                            }
                        ]
                    }
                },
                space_id=space_id,
            )

        def download_skill_archive(self, skill):
            return archive.getvalue()

    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "space-example")
    monkeypatch.setenv("KSADK_SKILL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(skill_tools, "_skill_service_client", Client)
    items = [ManifestItem("example", "Example", "1", "space-example")]
    cache = ManifestCache()
    monkeypatch.setattr(cache, "get_all", lambda: items)
    manifest_text = cache.build_instruction_text()
    for host in ("hermes", "openclaw"):
        root = tmp_path / host
        root.mkdir()
        monkeypatch.setenv(f"{host.upper()}_SKILL_HUB_DIR", str(root))
        getattr(register, f"_inject_{host}_skill_hub")(manifest_text, items)
        entry = (root / "example" / "SKILL.md").read_text()
        assert "mcp__ksadk_skill_center__load_skill" in entry
        assert "instruction-first" in entry
        assert "outer agent tools" in entry.replace("\n", " ")
        assert "supported isolated workflow entrypoint" in entry
        assert "you MUST call" not in entry
    assert "load_skill" in manifest_text and "instruction-first" in manifest_text
    assert "You MUST use the MCP tool execute_skills" not in manifest_text

    mcp = server._create_mcp_server()
    names = {tool.name for tool in await mcp.list_tools()}
    assert {"load_skill", "preview_skill", "execute_skills", "list_skills"} <= names
    response = await mcp.call_tool("load_skill", {"skill_name": "example"})
    blocks = response[0] if isinstance(response, tuple) else response
    result = json.loads(next(block.text for block in blocks if block.type == "text"))
    assert result["ok"]
    assert result["instructions"] == body
    assert result["has_scripts_dir"]
    assert result["script_files"] == ["scripts/process.py"]
    root = Path(result["root_dir"])
    assert (root / "scripts" / "process.py").is_file()
    assert (root / "references" / "details.md").read_text() == "Full reference instructions"
