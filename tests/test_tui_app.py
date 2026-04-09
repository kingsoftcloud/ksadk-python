from ksadk.tui.app import AgentTUI


class _DummyRunner:
    session_id = "sess-demo"


def test_agent_tui_prefers_runner_session_id():
    app = AgentTUI(runner=_DummyRunner(), project_dir=".")

    assert app.session_id == "sess-demo"
