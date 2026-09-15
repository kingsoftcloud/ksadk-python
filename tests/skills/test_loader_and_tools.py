from __future__ import annotations

from pathlib import Path

from ksadk.skills import tool_defs
from ksadk.skills.events import (
    BoundSkillRef,
    SkillBinding,
    SkillEvent,
    SkillExecutionContext,
)
from ksadk.skills.loader import load_local_skill
from ksadk.skills.models import SkillRef
from ksadk.skills.runtime import SkillRuntimeResult
from ksadk.skills.tool_defs import build_execute_skills_tool, build_skills_tool


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
    monkeypatch.setenv("KSADK_SKILL_OUTPUT_TEXT_MAX_BYTES", "32768")

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
    assert backend.calls[0][1]["env"]["KSADK_SKILL_OUTPUT_TEXT_MAX_BYTES"] == "32768"
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


def test_execute_skills_tool_passes_pinned_packages_to_runtime(monkeypatch):
    package = object()
    monkeypatch.setenv("KSADK_SKILL_SERVICE_SECRET_KEY", "must-not-enter-sandbox")

    class Backend:
        def __init__(self):
            self.calls = []

        def run_workflow(self, workflow_prompt: str, **kwargs):
            self.calls.append((workflow_prompt, kwargs))
            return SkillRuntimeResult(exit_code=0)

    backend = Backend()
    tool = build_execute_skills_tool(
        backend=backend,
        skill_space_ids=["selection-space"],
        session_id="sess-1",
        pinned_packages=[package],
    )

    tool("build a page", skill_names=["demo-skill"])

    assert backend.calls[0][1]["pinned_packages"] == [package]
    assert backend.calls[0][1]["skill_space_ids"] == []
    assert backend.calls[0][1]["env"] == {}


def test_execute_skills_tool_adds_trusted_context_events_without_public_arguments():
    skill_ref = SkillRef(
        "skill-1",
        "version-1",
        "1.0.0",
        "report",
        input_schema={"type": "object"},
    )
    context = SkillExecutionContext(
        run_id="run-1",
        trace_id="trace-1",
        binding=SkillBinding("binding-1", (BoundSkillRef(skill_ref, "space-1"),)),
        decision_id="decision-1",
        selected_skill_ids=("skill-1",),
    )

    class Backend:
        def run_workflow(self, workflow_prompt: str, **kwargs):
            assert "execution_context" not in kwargs
            invocation = kwargs["invocation_plan"].entries[0]
            assert invocation.skill_ref == skill_ref
            return SkillRuntimeResult(
                exit_code=0,
                workflow_status="ok",
                executed_skill="report",
                instructions="Use the pinned script.",
                sandbox={"backend": "e2b", "cleanup_status": "completed"},
                skill_events=[
                    SkillEvent.create(
                        "skill.load.completed",
                        status="completed",
                        skill_ref=skill_ref,
                        skill_invocation_id=invocation.skill_invocation_id,
                    )
                ],
            )

    tool = build_execute_skills_tool(
        backend=Backend(),
        skill_space_ids=["space-1"],
        execution_context=context,
    )

    result = tool("write a report", skill_names=["report"])

    assert [event["event_type"] for event in result["skill_events"]] == [
        "skill.candidates.resolved",
        "skill.selection.completed",
        "skill.load.completed",
    ]
    assert result["skill_events"][-1]["binding_snapshot_id"] == "binding-1"
    assert result["skill_events"][-1]["run_id"] == "run-1"
    assert result["workflow_status"] == "ok"
    assert result["executed_skill"] == "report"
    assert result["instructions"] == "Use the pinned script."
    assert result["sandbox"] == {"backend": "e2b", "cleanup_status": "completed"}


def test_execute_skills_tool_rejects_unbound_events_and_overwrites_untrusted_correlation():
    skill_ref = SkillRef("skill-1", "version-1", "1.0.0", "report")
    context = SkillExecutionContext(
        run_id="run-1",
        trace_id="trace-1",
        binding=SkillBinding("binding-1", (BoundSkillRef(skill_ref, "space-1"),)),
        decision_id="decision-1",
        selected_skill_ids=("skill-1",),
    )

    class Backend:
        def run_workflow(self, workflow_prompt: str, **kwargs):
            invocation = kwargs["invocation_plan"].entries[0]
            return SkillRuntimeResult(
                exit_code=0,
                skill_events=[
                    SkillEvent.create(
                        "skill.load.completed",
                        status="completed",
                        skill_ref=skill_ref,
                        skill_invocation_id=invocation.skill_invocation_id,
                        trace_id="forged-trace",
                        run_id="forged-run",
                        binding_snapshot_id="forged-binding",
                        decision_id="forged-decision",
                    ),
                    SkillEvent.create(
                        "skill.load.completed",
                        status="completed",
                        skill_ref=SkillRef("other", "v", "1", "other"),
                        skill_invocation_id="inv-2",
                    ),
                ],
            )

    result = build_execute_skills_tool(
        backend=Backend(), skill_space_ids=["space-1"], execution_context=context
    )("write a report")

    assert [event["event_type"] for event in result["skill_events"]] == [
        "skill.candidates.resolved",
        "skill.selection.completed",
        "skill.load.completed",
        "sandbox.envelope.rejected",
    ]
    accepted = result["skill_events"][2]
    assert accepted["trace_id"] == "trace-1"
    assert accepted["run_id"] == "run-1"
    assert accepted["binding_snapshot_id"] == "binding-1"
    assert accepted["decision_id"] == "decision-1"


def test_execute_skills_tool_accepts_matching_identity_with_richer_runtime_metadata():
    skill_ref = SkillRef("skill-1", "version-1", "1.0.0", "report")
    runtime_ref = SkillRef(
        "skill-1",
        "version-1",
        "1.0.0",
        "report",
        description="Runtime description",
        status="active",
        aliases=("reporting",),
        tags=("analytics",),
    )
    context = SkillExecutionContext(
        binding=SkillBinding("binding-1", (BoundSkillRef(skill_ref, "space-1"),)),
        decision_id="decision-1",
        selected_skill_ids=("skill-1",),
    )

    class Backend:
        def run_workflow(self, workflow_prompt: str, **kwargs):
            invocation = kwargs["invocation_plan"].entries[0]
            return SkillRuntimeResult(
                exit_code=0,
                output_text="report body",
                skill_events=[
                    SkillEvent.create(
                        "skill.execution.completed",
                        status="completed",
                        skill_ref=runtime_ref,
                        skill_invocation_id=invocation.skill_invocation_id,
                    )
                ],
            )

    result = build_execute_skills_tool(
        backend=Backend(), skill_space_ids=["space-1"], execution_context=context
    )("write a report")

    assert [event["event_type"] for event in result["skill_events"]] == [
        "skill.candidates.resolved",
        "skill.selection.completed",
        "skill.execution.completed",
    ]
    assert result["skill_events"][-1]["skill_ref"]["description"] == ""
    assert result["skill_events"][-1]["skill_ref"]["aliases"] == []
    assert result["output_text"] == "report body"


def test_execute_skills_tool_projects_accepted_skill_events(monkeypatch) -> None:
    event = SkillEvent.create(
        "skill.load.completed", status="completed", skill_invocation_id="inv-1"
    )
    projected: list[list[SkillEvent]] = []
    monkeypatch.setattr(
        tool_defs, "project_skill_events", lambda events: projected.append(list(events))
    )

    class Backend:
        def run_workflow(self, workflow_prompt: str, **kwargs):
            return SkillRuntimeResult(exit_code=0, skill_events=[event])

    tool = build_execute_skills_tool(backend=Backend(), skill_space_ids=["space-1"])

    tool("write a report")

    assert projected == [[event]]


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
