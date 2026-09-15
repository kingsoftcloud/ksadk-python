from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.service import StudioService


def _write_skill(root: Path, name: str = "release-review") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Review a release using verifiable evidence.\n"
        "version: 1.2.0\n"
        "---\n\n"
        "Use the checklist and report blockers.\n",
        encoding="utf-8",
    )
    (skill / "scripts").mkdir()
    (skill / "scripts/check.py").write_text("print('check')\n", encoding="utf-8")
    return skill


def test_skill_discovery_is_inspect_then_confirm_import(tmp_path: Path) -> None:
    source = _write_skill(tmp_path / "skills")
    studio = StudioService(tmp_path)

    inspection = studio.catalog.discover_skills(scan_paths=["skills"])

    assert inspection["requiresConfirmation"] is True
    assert len(inspection["candidates"]) == 1
    candidate = inspection["candidates"][0]
    assert candidate["name"] == "release-review"
    assert candidate["status"] == "ready"
    assert candidate["risk"]["containsScripts"] is True
    assert not (tmp_path / "capabilities/skills/release-review").exists()

    imported = studio.catalog.commit_discovered_skill(
        inspection["inspectionToken"],
        candidate["candidateId"],
    )

    assert imported.kind == "skill"
    assert imported.status == "ready"
    installed = tmp_path / "capabilities/skills/release-review"
    assert installed.is_dir()
    assert (installed / "SKILL.md").read_text() == (source / "SKILL.md").read_text()


def test_skill_discovery_reports_conflict_without_overwriting(tmp_path: Path) -> None:
    _write_skill(tmp_path / "skills")
    installed = _write_skill(tmp_path / "capabilities/skills")
    (installed / "SKILL.md").write_text(
        (installed / "SKILL.md").read_text().replace("1.2.0", "1.1.0")
    )
    studio = StudioService(tmp_path)

    inspection = studio.catalog.discover_skills(scan_paths=["skills"])
    candidate = inspection["candidates"][0]

    assert candidate["status"] == "conflict"
    try:
        studio.catalog.commit_discovered_skill(
            inspection["inspectionToken"],
            candidate["candidateId"],
        )
    except Exception as exc:
        assert getattr(exc, "code", "") == "SKILL_IMPORT_CONFLICT"
    else:
        raise AssertionError("conflicting Skill import must require explicit overwrite")


def test_skill_discovery_api_returns_candidates_and_commit(tmp_path: Path) -> None:
    _write_skill(tmp_path / ".agents/skills", "api-skill")
    app = create_studio_app(tmp_path, security_enabled=False)

    with TestClient(app) as client:
        discovered = client.post(
            "/api/v1/catalog/skills:discover",
            json={"scanPaths": [".agents/skills"]},
        )
        assert discovered.status_code == 200
        candidate = discovered.json()["candidates"][0]

        committed = client.post(
            f"/api/v1/catalog/skills/discoveries/{discovered.json()['inspectionToken']}:commit",
            json={"candidateId": candidate["candidateId"], "overwrite": False},
        )
        assert committed.status_code == 201
        assert committed.json()["name"] == "api-skill"


def test_skill_discovery_token_commits_multiple_candidates_in_scan_order(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path / "skills", "alpha-skill")
    _write_skill(tmp_path / "skills", "beta-skill")
    studio = StudioService(tmp_path)

    inspection = studio.catalog.discover_skills(scan_paths=["skills"])
    candidates = inspection["candidates"]

    imported = [
        studio.catalog.commit_discovered_skill(
            inspection["inspectionToken"],
            candidate["candidateId"],
        )
        for candidate in candidates
    ]

    assert [item.name for item in imported] == ["alpha-skill", "beta-skill"]
    assert (tmp_path / "capabilities/skills/alpha-skill/SKILL.md").is_file()
    assert (tmp_path / "capabilities/skills/beta-skill/SKILL.md").is_file()


def test_skill_discovery_includes_allowlisted_codex_and_claude_user_roots(
    monkeypatch, tmp_path: Path
) -> None:
    """默认扫描兼容项目和用户级 Codex / Claude Skill，不开放任意 home 路径。"""
    user_home = tmp_path / "user-home"
    monkeypatch.setenv("HOME", str(user_home))
    _write_skill(user_home / ".codex/skills", "codex-user-skill")
    _write_skill(user_home / ".claude/skills", "claude-user-skill")
    studio = StudioService(tmp_path / "workspace")

    inspection = studio.catalog.discover_skills()

    candidates = {candidate["name"]: candidate for candidate in inspection["candidates"]}
    codex = candidates["codex-user-skill"]
    claude = candidates["claude-user-skill"]
    assert codex["source"] == "user:codex"
    assert codex["path"] == "~/.codex/skills/codex-user-skill"
    assert claude["source"] == "user:claude"
    assert claude["path"] == "~/.claude/skills/claude-user-skill"

    imported = studio.catalog.commit_discovered_skill(
        inspection["inspectionToken"], codex["candidateId"]
    )
    assert imported.name == "codex-user-skill"
    assert (tmp_path / "workspace/capabilities/skills/codex-user-skill/SKILL.md").is_file()


def test_skill_discovery_accepts_one_explicit_skill_inside_allowlisted_user_root(
    monkeypatch, tmp_path: Path
) -> None:
    user_home = tmp_path / "user-home"
    monkeypatch.setenv("HOME", str(user_home))
    skill = _write_skill(user_home / ".claude/skills", "baoyu-design")
    studio = StudioService(tmp_path / "workspace")

    inspection = studio.catalog.discover_skills(scan_paths=[str(skill)])

    assert len(inspection["candidates"]) == 1
    candidate = inspection["candidates"][0]
    assert candidate["name"] == "baoyu-design"
    assert candidate["source"] == "user:claude"
    assert candidate["path"] == "~/.claude/skills/baoyu-design"
    imported = studio.catalog.commit_discovered_skill(
        inspection["inspectionToken"], candidate["candidateId"]
    )
    assert imported.name == "baoyu-design"


def test_skill_discovery_rejects_non_allowlisted_user_path(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)

    try:
        studio.catalog.discover_skills(scan_paths=[str(tmp_path.parent)])
    except Exception as exc:
        assert getattr(exc, "code", "") == "SKILL_DISCOVERY_PATH_FORBIDDEN"
    else:
        raise AssertionError("arbitrary external skill paths must stay forbidden")


def test_skill_import_allows_symlink_into_another_allowlisted_user_root(
    monkeypatch, tmp_path: Path
) -> None:
    """扫描允许跟随用户 Skill 软链，确认阶段必须接受仍在白名单内的目标。"""
    user_home = tmp_path / "user-home"
    monkeypatch.setenv("HOME", str(user_home))
    target = _write_skill(user_home / ".agents/skills", "linked-skill")
    claude_root = user_home / ".claude/skills"
    claude_root.mkdir(parents=True)
    (claude_root / "linked-skill").symlink_to(target, target_is_directory=True)
    studio = StudioService(tmp_path / "workspace")

    inspection = studio.catalog.discover_skills(scan_paths=["user:claude"])
    candidate = inspection["candidates"][0]
    assert candidate["source"] == "user:claude"

    imported = studio.catalog.commit_discovered_skill(
        inspection["inspectionToken"], candidate["candidateId"]
    )

    assert imported.name == "linked-skill"
    assert (tmp_path / "workspace/capabilities/skills/linked-skill/scripts/check.py").is_file()


def test_skill_discovery_api_previews_markdown_and_script_files(tmp_path: Path) -> None:
    _write_skill(tmp_path / "skills", "preview-skill")
    app = create_studio_app(tmp_path, security_enabled=False)

    with TestClient(app) as client:
        discovered = client.post(
            "/api/v1/catalog/skills:discover",
            json={"scanPaths": ["skills"]},
        ).json()
        candidate = discovered["candidates"][0]
        base = (
            "/api/v1/catalog/skills/discoveries/"
            f"{discovered['inspectionToken']}/candidates/{candidate['candidateId']}/files"
        )

        listing = client.get(base)
        assert listing.status_code == 200, listing.text
        assert listing.json()["files"] == [
            {"path": "SKILL.md", "size": listing.json()["files"][0]["size"], "kind": "markdown"},
            {"path": "scripts/check.py", "size": 15, "kind": "script"},
        ]

        markdown = client.get(base, params={"path": "SKILL.md"})
        assert markdown.status_code == 200, markdown.text
        assert markdown.json()["kind"] == "markdown"
        assert "Use the checklist" in markdown.json()["content"]

        script = client.get(base, params={"path": "scripts/check.py"})
        assert script.status_code == 200, script.text
        assert script.json()["kind"] == "script"
        assert script.json()["content"] == "print('check')\n"

        escaped = client.get(base, params={"path": "../agentkit.yaml"})
        assert escaped.status_code in {403, 422}
