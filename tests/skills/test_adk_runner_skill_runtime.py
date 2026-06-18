from __future__ import annotations

import textwrap
from types import SimpleNamespace
from uuid import uuid4

from ksadk.detection import DetectionResult, FrameworkType


def _write_adk_project(tmp_path, source: str) -> DetectionResult:
    package_name = f"skill_agent_{uuid4().hex[:8]}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "agent.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return DetectionResult(
        type=FrameworkType.ADK,
        name="demo-agent",
        entry_point=f"{package_name}/agent.py",
        package_path=str(package_dir),
        agent_variable="root_agent",
        confidence=1.0,
    )


def _tool_names(tools):
    return [getattr(tool, "name", None) or getattr(tool, "__name__", "") for tool in tools]


class FakeRunner:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeRunner.instances.append(self)


def _patch_runner(monkeypatch):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    FakeRunner.instances.clear()
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)
    return ADKRunner


def test_adk_runner_injects_execute_skills_for_sandbox_mode(monkeypatch, tmp_path):
    ADKRunner = _patch_runner(monkeypatch)
    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )
    monkeypatch.setenv("KSADK_SKILLS_MODE", "sandbox")
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "disabled")
    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-1")

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert _tool_names(runner._agent.tools) == ["execute_skills"]
    assert len(FakeRunner.instances) == 1


def test_adk_runner_injects_remote_skill_manifest_into_instruction(monkeypatch, tmp_path):
    ADKRunner = _patch_runner(monkeypatch)
    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )

    def fake_load_remote_skill_manifests(skill_space_ids=None):
        assert skill_space_ids == ["ss-user", "ss-public"]
        return [
            {
                "name": "demo-skill",
                "description": "Create spreadsheet reports",
                "version": "v1",
            }
        ]

    monkeypatch.setenv("KSADK_SKILLS_MODE", "sandbox")
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "disabled")
    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-user")
    monkeypatch.setenv("KSADK_PUBLIC_SKILL_SPACE_IDS", "ss-public")
    monkeypatch.setattr(
        "ksadk.skills.tool_defs.load_remote_skill_manifests",
        fake_load_remote_skill_manifests,
        raising=False,
    )

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert "demo-skill" in runner._agent.instruction
    assert "Create spreadsheet reports" in runner._agent.instruction
    assert "skill_names" in runner._agent.instruction


def test_remote_skill_manifest_filters_public_skills_with_allowlist(monkeypatch):
    from ksadk.skills.tool_defs import load_remote_skill_manifests

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def list_skills_by_space_id(self, space_id):
            from ksadk.skills.models import SkillListResponse

            assert space_id == "ss-user"
            payload = {
                "Data": {
                    "Skills": [
                        {
                            "SkillId": "sk-demo",
                            "VersionId": "sv-demo-v1",
                            "Version": "v1",
                            "Name": "demo-skill",
                            "Status": "Active",
                        }
                    ]
                }
            }
            return SkillListResponse.from_payload(payload, space_id=space_id)

        def list_available_premade_skills(self):
            from ksadk.skills.models import SkillListResponse

            payload = {
                "Data": {
                    "Skills": [
                        {
                            "SkillId": "premade-pdf",
                            "VersionId": "",
                            "Version": "",
                            "Name": "pdf",
                            "Status": "AVAILABLE",
                        },
                        {
                            "SkillId": "premade-weather",
                            "VersionId": "",
                            "Version": "",
                            "Name": "weather",
                            "Status": "AVAILABLE",
                        },
                    ]
                }
            }
            return SkillListResponse.from_payload(payload, space_id="public")

    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-user")
    monkeypatch.setenv("KSADK_PUBLIC_SKILL_SPACE_IDS", "ss-public")
    monkeypatch.setenv("KSADK_PUBLIC_SKILL_ALLOWLIST", "weather")
    monkeypatch.setattr(
        "ksadk.skills.tool_defs.SkillServiceClient",
        FakeClient,
    )

    manifests = load_remote_skill_manifests()

    assert [item["name"] for item in manifests] == ["demo-skill", "weather"]


def test_execute_skills_passes_public_skill_spaces_through_env(monkeypatch):
    from ksadk.skills.tool_defs import build_execute_skills_tool

    calls = []

    class FakeBackend:
        def run_workflow(self, workflow_prompt, **kwargs):
            calls.append((workflow_prompt, kwargs))
            return SimpleNamespace(to_dict=lambda: {"status": "ok"})

    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-user")
    monkeypatch.setenv("KSADK_PUBLIC_SKILL_SPACE_IDS", "ss-public-a, ss-public-b")

    tool = build_execute_skills_tool(backend=FakeBackend(), session_id="sess-1")
    result = tool("use demo-skill")

    assert result == {"status": "ok"}
    assert calls[0][1]["skill_space_ids"] == ["ss-user"]
    assert calls[0][1]["env"]["KSADK_PUBLIC_SKILL_SPACE_IDS"] == "ss-public-a, ss-public-b"


def test_adk_runner_auto_mode_prefers_configured_runtime_backend_over_cache_dir(monkeypatch, tmp_path):
    ADKRunner = _patch_runner(monkeypatch)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )
    monkeypatch.delenv("KSADK_SKILLS_MODE", raising=False)
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "disabled")
    monkeypatch.setenv("KSADK_SKILL_CACHE_DIR", str(cache_dir))

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()
    assert _tool_names(runner._agent.tools) == []

    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "e2b")
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_TEMPLATE_ID", "tpl-1")
    monkeypatch.setattr(
        "ksadk.skills.runtime.backends.e2b.E2BSkillRuntimeBackend.from_env",
        lambda: type("Backend", (), {"run_workflow": lambda self, *args, **kwargs: None})(),
    )

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()
    assert _tool_names(runner._agent.tools) == ["execute_skills"]


def test_adk_runner_auto_mode_uses_generic_sandbox_template(monkeypatch, tmp_path):
    ADKRunner = _patch_runner(monkeypatch)
    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )
    monkeypatch.delenv("KSADK_SKILLS_MODE", raising=False)
    monkeypatch.delenv("KSADK_SKILL_RUNTIME_BACKEND", raising=False)
    monkeypatch.delenv("KSADK_SKILL_RUNTIME_TEMPLATE_ID", raising=False)
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-aio")
    monkeypatch.setattr(
        "ksadk.skills.runtime.backends.e2b.E2BSkillRuntimeBackend.from_env",
        lambda: type("Backend", (), {"run_workflow": lambda self, *args, **kwargs: None})(),
    )

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert _tool_names(runner._agent.tools) == ["execute_skills"]


def test_adk_runner_auto_mode_respects_explicit_disabled_runtime_backend(monkeypatch, tmp_path):
    ADKRunner = _patch_runner(monkeypatch)
    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )
    monkeypatch.delenv("KSADK_SKILLS_MODE", raising=False)
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "disabled")
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-aio")

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert _tool_names(runner._agent.tools) == []


def test_adk_runner_no_longer_injects_legacy_sandbox_tools_by_default(monkeypatch, tmp_path):
    ADKRunner = _patch_runner(monkeypatch)
    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )
    monkeypatch.delenv("KSADK_SKILLS_MODE", raising=False)
    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert "execute_python" not in _tool_names(runner._agent.tools)
    assert "execute_bash" not in _tool_names(runner._agent.tools)
    assert "execute_javascript" not in _tool_names(runner._agent.tools)


def test_adk_runner_deduplicates_existing_execute_skills(monkeypatch, tmp_path):
    ADKRunner = _patch_runner(monkeypatch)
    detection = _write_adk_project(
        tmp_path,
        """
        def execute_skills(workflow_prompt: str) -> dict:
            return {"stdout": workflow_prompt}

        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = [execute_skills]
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )
    monkeypatch.setenv("KSADK_SKILLS_MODE", "sandbox")
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "disabled")
    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-1")

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert _tool_names(runner._agent.tools).count("execute_skills") == 1


def test_adk_runner_injects_local_skills_tool_for_local_mode(monkeypatch, tmp_path):
    ADKRunner = _patch_runner(monkeypatch)
    skill_root = tmp_path / "skills" / "demo-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo\n---\n# Demo\n",
        encoding="utf-8",
    )
    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )
    monkeypatch.setenv("KSADK_SKILLS_MODE", "local")
    monkeypatch.setenv("KSADK_LOCAL_SKILLS_DIR", str(tmp_path / "skills"))

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert _tool_names(runner._agent.tools) == ["skills_tool"]
