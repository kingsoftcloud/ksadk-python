from __future__ import annotations

from pathlib import Path

import pytest

from ksadk.skills.loader import load_local_skill
from ksadk.skills.tool_defs import build_execute_skills_tool, build_skills_tool
from ksadk.skills.runtime import SkillRuntimeResult


def test_load_local_skill_reads_frontmatter(tmp_path: Path):
    root = tmp_path / "web-artifacts-builder"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: web-artifacts-builder\ndescription: Build artifacts\n---\n# Body\n",
        encoding="utf-8",
    )

    skill = load_local_skill(root)

    assert skill.name == "web-artifacts-builder"
    assert skill.description == "Build artifacts"
    assert skill.root_dir == root


def test_execute_skills_tool_delegates_to_runtime_without_leaking_secret(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "secret-token")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_SECRET_KEY", "skill-secret")

    class Backend:
        def __init__(self):
            self.calls = []

        def run_workflow(self, workflow_prompt: str, **kwargs):
            self.calls.append((workflow_prompt, kwargs))
            return SkillRuntimeResult(
                runtime_id="sbx-1",
                exit_code=0,
                stdout="artifact ready\n",
                stderr="",
                duration_ms=15,
            )

    backend = Backend()
    tool = build_execute_skills_tool(backend=backend, skill_space_ids=["ss-1"], session_id="sess-1")

    output = tool("build a page")

    assert output["stdout"] == "artifact ready\n"
    assert output["runtime_id"] == "sbx-1"
    assert "secret-token" not in repr(output)
    assert backend.calls[0][0] == "build a page"
    assert backend.calls[0][1]["skill_space_ids"] == ["ss-1"]
    assert backend.calls[0][1]["env"]["KSADK_SKILL_SERVICE_URL"] == "https://skill.example/api/v1"
    assert backend.calls[0][1]["env"]["KSADK_SKILL_SERVICE_SECRET_KEY"] == "skill-secret"
    assert "E2B_API_KEY" not in backend.calls[0][1]["env"]


def test_execute_skills_tool_passes_explicit_skill_names_to_runtime():
    class Backend:
        def __init__(self):
            self.calls = []

        def run_workflow(self, workflow_prompt: str, **kwargs):
            self.calls.append((workflow_prompt, kwargs))
            return SkillRuntimeResult(exit_code=0)

    backend = Backend()
    tool = build_execute_skills_tool(backend=backend, skill_space_ids=["ss-1"], session_id="sess-1")

    tool("build a page", skill_names=["demo-skill"])

    assert backend.calls[0][1]["skill_names"] == ["demo-skill"]


def test_execute_skills_tool_maps_ksyun_fallbacks_to_skill_service_env(monkeypatch):
    monkeypatch.delenv("KSADK_SKILL_SERVICE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("KSADK_SKILL_SERVICE_REGION", raising=False)
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")
    monkeypatch.setenv("KSYUN_REGION", "cn-beijing-6")
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "generic-ak-should-not-cross")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "generic-sk-should-not-cross")

    class Backend:
        def __init__(self):
            self.calls = []

        def run_workflow(self, workflow_prompt: str, **kwargs):
            self.calls.append((workflow_prompt, kwargs))
            return SkillRuntimeResult(exit_code=0)

    backend = Backend()
    tool = build_execute_skills_tool(backend=backend, skill_space_ids=["ss-1"], session_id="sess-1")

    tool("build a page")

    env = backend.calls[0][1]["env"]
    assert env["KSADK_SKILL_SERVICE_ACCOUNT_ID"] == "2000003485"
    assert env["KSADK_SKILL_SERVICE_REGION"] == "cn-beijing-6"
    assert "KSYUN_ACCESS_KEY" not in env
    assert "KSYUN_SECRET_KEY" not in env
    assert env["KSADK_SKILL_SERVICE_ACCESS_KEY"] == "generic-ak-should-not-cross"
    assert env["KSADK_SKILL_SERVICE_SECRET_KEY"] == "generic-sk-should-not-cross"


def test_execute_skills_tool_auto_resolves_skill_service_url_for_runtime(monkeypatch):
    monkeypatch.delenv("KSADK_SKILL_SERVICE_URL", raising=False)
    monkeypatch.delenv("KSADK_SKILL_SERVICE_ENDPOINT", raising=False)
    monkeypatch.delenv("KSADK_SKILL_SERVICE_SCHEME", raising=False)
    monkeypatch.setenv("KSADK_AICP_ENDPOINT_MODE", "internal")
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")
    monkeypatch.setenv("KSYUN_REGION", "cn-beijing-6")

    class Backend:
        def __init__(self):
            self.calls = []

        def run_workflow(self, workflow_prompt: str, **kwargs):
            self.calls.append((workflow_prompt, kwargs))
            return SkillRuntimeResult(exit_code=0)

    backend = Backend()
    tool = build_execute_skills_tool(backend=backend, skill_space_ids=["ss-1"], session_id="sess-1")

    tool("build a page")

    env = backend.calls[0][1]["env"]
    assert env["KSADK_SKILL_SERVICE_URL"] == "http://aicp.internal.api.ksyun.com"
    assert env["KSADK_SKILL_SERVICE_ACCOUNT_ID"] == "2000003485"
    assert env["KSADK_SKILL_SERVICE_REGION"] == "cn-beijing-6"


def test_execute_skills_tool_leaves_auto_endpoint_detection_to_runtime(monkeypatch):
    monkeypatch.delenv("KSADK_SKILL_SERVICE_URL", raising=False)
    monkeypatch.delenv("KSADK_SKILL_SERVICE_ENDPOINT", raising=False)
    monkeypatch.delenv("KSADK_SKILL_SERVICE_SCHEME", raising=False)
    monkeypatch.setenv("KSADK_AICP_ENDPOINT_MODE", "auto")

    class Backend:
        def __init__(self):
            self.calls = []

        def run_workflow(self, workflow_prompt: str, **kwargs):
            self.calls.append((workflow_prompt, kwargs))
            return SkillRuntimeResult(exit_code=0)

    backend = Backend()
    tool = build_execute_skills_tool(backend=backend, skill_space_ids=["ss-1"], session_id="sess-1")

    tool("build a page")

    assert "KSADK_SKILL_SERVICE_URL" not in backend.calls[0][1]["env"]


def test_execute_skills_tool_passes_public_skill_allowlist_to_runtime(monkeypatch):
    monkeypatch.setenv("KSADK_PUBLIC_SKILL_ALLOWLIST", "pdf,weather")

    class Backend:
        def __init__(self):
            self.calls = []

        def run_workflow(self, workflow_prompt: str, **kwargs):
            self.calls.append((workflow_prompt, kwargs))
            return SkillRuntimeResult(exit_code=0)

    backend = Backend()
    tool = build_execute_skills_tool(backend=backend, skill_space_ids=["ss-1"], session_id="sess-1")

    tool("build a page")

    assert backend.calls[0][1]["env"]["KSADK_PUBLIC_SKILL_ALLOWLIST"] == "pdf,weather"


def test_skills_tool_reports_loaded_local_skills(tmp_path: Path):
    root = tmp_path / "demo-skill"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill\n---\n# Demo Body\nUse this carefully.\n",
        encoding="utf-8",
    )

    tool = build_skills_tool([load_local_skill(root)])

    result = tool("list")

    assert result["skills"][0]["name"] == "demo-skill"
    assert result["skills"][0]["description"] == "Demo skill"
    assert "Use this carefully" in result["skills"][0]["body"]
