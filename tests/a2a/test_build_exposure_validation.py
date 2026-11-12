from pathlib import Path

from ksadk.a2a.build_validation import validate_a2a_exposure_sources
from ksadk.builders.code_builder import CodeBuilder

CONFLICTING_AGENT = '''
from google.adk.agents import Agent
from ksadk.a2a.exposure import compile_exposure_plan
from ksadk.a2a.invocation_types import RemoteAgentSpec

SPACE = "a2a-space-00000000000040008000000000000041"
TARGET = "a2a-agent-00000000000040008000000000000041"
VERSION = "a2a-version-00000000000040008000000000000041"

native = RemoteAgentSpec(
    local_name="fixed_native", binding_id="binding-native", space_id=SPACE,
    target_agent_id=TARGET, expected_version_id=VERSION, exposure="native",
)
tool = RemoteAgentSpec(
    local_name="dynamic_tool", binding_id="binding-tool", space_id=SPACE,
    target_agent_id=TARGET, expected_version_id=VERSION, exposure="tool",
)
compile_exposure_plan([native, tool], framework="adk", consumer_id="root")
root_agent = Agent(name="conflicting", model="gemini-2.0-flash")
'''


def _write_project(path: Path, source: str = CONFLICTING_AGENT) -> None:
    package = path / "conflicting_agent"
    package.mkdir()
    (package / "agent.py").write_text(source, encoding="utf-8")
    (path / "agentengine.yaml").write_text(
        "name: conflicting_agent\nframework: adk\n"
        "entry_point: conflicting_agent/agent.py\nagent_variable: root_agent\n",
        encoding="utf-8",
    )


def test_static_exposure_validation_rejects_same_target_plan(tmp_path: Path) -> None:
    _write_project(tmp_path)
    try:
        validate_a2a_exposure_sources(tmp_path)
    except ValueError as exc:
        assert "A2A_EXPOSURE_CONFLICT" in str(exc)
        assert "conflicting_agent/agent.py" in str(exc)
    else:
        raise AssertionError("same-target native/tool plan must be rejected")


def test_code_builder_rejects_exposure_conflict_before_dependency_install(
    tmp_path: Path, monkeypatch
) -> None:
    _write_project(tmp_path)
    builder = CodeBuilder(tmp_path)
    monkeypatch.setattr(
        builder,
        "_install_dependencies",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dependency installation must not start")
        ),
    )
    result = builder.build()
    assert result.success is False
    assert "A2A_EXPOSURE_CONFLICT" in str(result.error_message)


def test_static_exposure_validation_allows_distinct_targets(tmp_path: Path) -> None:
    source = CONFLICTING_AGENT.replace(
        'TARGET = "a2a-agent-00000000000040008000000000000041"',
        'TARGET = "a2a-agent-00000000000040008000000000000041"\n'
        'OTHER = "a2a-agent-00000000000040008000000000000042"',
    ).replace(
        'target_agent_id=TARGET, expected_version_id=VERSION, exposure="tool"',
        'target_agent_id=OTHER, expected_version_id=VERSION, exposure="tool"',
    )
    _write_project(tmp_path, source)
    validate_a2a_exposure_sources(tmp_path)
