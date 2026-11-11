"""Real Chromium checks for Studio's Run-scoped Native browser owner."""

import json
import sqlite3
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from ksadk.harness.native.model import ModelToolCall
from ksadk.harness.native.ports import ToolDefinition, ToolRequest
from ksadk.harness.native.state import RunScope
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    CapabilityBinding,
    ExecutionSpec,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RunStatus,
    RuntimeRef,
    SecuritySpec,
    Usage,
)
from ksadk.studio.model_client import ModelResponse, ToolCall
from ksadk.studio.native_browser_port import (
    StudioNativeBrowserPort,
    _is_launch_label,
    _QuietScopedHandler,
    browser_environment_warning,
)
from ksadk.studio.service import StudioService
from tests.studio.test_dsh_provider_registration import _managed_profile

pytestmark = pytest.mark.local_process_heavy

_SCOPE = RunScope(tenant_id="tenant", workspace_id="workspace", user_id="user", agent_id="agent")
_DEFINITION = ToolDefinition(name="browser_inspect_workspace", description="browser", parameters={})


def test_pointer_lock_environment_classification_requires_exact_observed_error() -> None:
    message = "The root document of this element is not valid for pointer lock."
    assert browser_environment_warning(message)
    assert not browser_environment_warning("app bug: " + message)


def test_settings_entry_is_not_a_launch_control() -> None:
    assert not _is_launch_label("进入设置")


def test_browser_file_server_rejects_workspace_symlink_to_outside(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("fixture-private", encoding="utf-8")
    (workspace / "outside.txt").symlink_to(outside)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietScopedHandler, directory=str(workspace))
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(f"http://127.0.0.1:{server.server_port}/outside.txt", timeout=2)
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(run: str, operation: str, **arguments: object) -> ToolRequest:
    return ToolRequest(
        scope=_SCOPE,
        run_id=run,
        operation_key=operation,
        call=ModelToolCall(
            call_id=f"call-{operation}",
            name="browser_inspect_workspace",
            arguments={"path": "app/index.html", "wait_ms": 0, **arguments},
        ),
        definition=_DEFINITION,
    )


@pytest.mark.asyncio
async def test_visible_control_without_id_has_an_actionable_unique_selector(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        "<style>.craft-slot { cursor: pointer }</style>"
        '<div id="count">0</div>'
        '<section><div class="craft-slot" '
        "onclick=\"document.querySelector('#count').textContent = '1'\">"
        "Craft plank</div></section>",
        encoding="utf-8",
    )
    browser = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="no-id-control")
    try:
        first = await browser.execute(_request("run", "observe"))
        assert first.status == "ok"
        controls = [
            control for control in first.content["controls"] if control["label"] == "Craft plank"
        ]
        assert len(controls) == 1
        selector = controls[0]["selector"]
        assert isinstance(selector, str) and selector.endswith("div:nth-of-type(1)")
        clicked = await browser.execute(
            _request("run", "click", actions=[{"type": "click", "selector": selector}])
        )
        assert clicked.status == "ok"
        assert "1" in clicked.content["body_text_preview"]
    finally:
        await browser.aclose()


@pytest.mark.asyncio
async def test_studio_browser_uses_available_chrome_for_webgl_pointer_lock(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        '<canvas id="world"></canvas><button id="play">Play</button><p id="lock">pending</p>'
        '<script>const world = document.querySelector("#world");'
        'world.getContext("webgl");'
        'document.querySelector("#play").onclick = async () => {'
        "try { await world.requestPointerLock();"
        'document.querySelector("#lock").textContent = document.pointerLockElement'
        ' === world ? "locked" : "denied"; }'
        'catch (error) { document.querySelector("#lock").textContent = error.name; }'
        "};</script>",
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="webgl-lock")
    try:
        result = await port.execute(
            _request("run", "pointer-lock", actions=[{"type": "click", "selector": "#play"}])
        )
        assert "browser_channel" in result.content
        if result.content.get("browser_channel") != "chrome":
            pytest.skip("system Chrome is unavailable; bundled Chromium cannot test pointer lock")
        assert result.status == "ok"
        assert result.content["browser_version"]
        assert "locked" in result.content["body_text_preview"]
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_bounded_hold_actions_move_real_page_and_invalid_batch_has_no_effect(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        "<body tabindex='0'><button id='mine'>Mine</button>"
        "<p id='steps'>0</p><p id='hits'>0</p>"
        "<script>let key=false,mouse=false,steps=0,hits=0;"
        "document.addEventListener('keydown',e=>{if(e.key==='w')key=true});"
        "document.addEventListener('keyup',e=>{if(e.key==='w')key=false});"
        "document.querySelector('#mine').addEventListener('mousedown',()=>mouse=true);"
        "document.querySelector('#mine').addEventListener('mouseup',()=>mouse=false);"
        "setInterval(()=>{if(key)document.querySelector('#steps').textContent=++steps;"
        "if(mouse)document.querySelector('#hits').textContent=++hits},20);"
        "</script></body>",
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="test-browser")
    try:
        for index, broken in enumerate(
            (
                {"type": "press", "selector": "body", "key": "w", "duration_ms": 5001},
                {"type": "press", "selector": "body"},
                {"type": "click"},
                {"type": "fill", "selector": "#mine"},
                {"type": "click", "selector": "#mine", "button": "other"},
                {"type": "move", "x": 1280, "y": 0},
            )
        ):
            invalid = await port.execute(
                _request(
                    "run",
                    f"invalid-{index}",
                    actions=[{"type": "click", "selector": "#mine", "duration_ms": 300}, broken],
                )
            )
            assert invalid.status == "error"
            assert invalid.content["code"] == "browser_action_invalid"
        over_budget = await port.execute(
            _request(
                "run",
                "over-budget",
                actions=[
                    {"type": "click", "selector": "#mine"},
                    *({"type": "wait", "ms": 10000} for _ in range(4)),
                ],
            )
        )
        assert over_budget.status == "error"
        assert over_budget.content["code"] == "browser_action_time_budget_exceeded"
        held = await port.execute(
            _request(
                "run",
                "held",
                actions=[
                    {"type": "press", "selector": "body", "key": "w", "duration_ms": 300},
                    {"type": "click", "selector": "#mine", "duration_ms": 300},
                ],
            )
        )
        assert held.status == "ok", held.content
        text = held.content["body_text_preview"]
        assert "Mine" in text
        assert int(text.split("Mine", 1)[1].split()[0]) >= 5
        assert int(text.split("Mine", 1)[1].split()[1]) >= 5
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_right_click_and_mouse_move_reach_real_page(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        "<body><button id='block'>Block</button><p id='move'>Move0</p>"
        "<p id='right'>Right0</p><script>let moved=0,right=0;"
        "document.addEventListener('mousemove',()=>{"
        "document.querySelector('#move').textContent='Move'+ ++moved});"
        "document.querySelector('#block').addEventListener('mousedown',e=>{"
        "if(e.button===2)document.querySelector('#right').textContent='Right'+ ++right});"
        "document.addEventListener('contextmenu',e=>e.preventDefault());"
        "</script></body>",
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="test-browser")
    try:
        result = await port.execute(
            _request(
                "run",
                "pointer-actions",
                actions=[
                    {"type": "move", "x": 20, "y": 40, "steps": 3},
                    {"type": "click", "selector": "#block", "button": "right"},
                ],
            )
        )
        assert result.status == "ok", result.content
        assert "Right1" in result.content["body_text_preview"]
        assert "Move" in result.content["body_text_preview"]
        assert "Move0" not in result.content["body_text_preview"]
        assert [action["ok"] for action in result.content["actions"]] == [True, True]
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_browser_page_survives_tool_turns_but_isolated_by_run(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        "<button id='increment' onclick='document.querySelector(\"#count\").textContent++'>"
        "increment</button><span id='count'>0</span>",
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="test-browser")
    try:
        first = await port.execute(
            _request("run-one", "first", actions=[{"type": "click", "selector": "#increment"}])
        )
        second = await port.execute(
            _request("run-one", "second", actions=[{"type": "click", "selector": "#increment"}])
        )
        other = await port.execute(_request("run-two", "other"))
        assert first.status == second.status == other.status == "ok"
        assert "increment1" in first.content["body_text_preview"]
        assert "increment2" in second.content["body_text_preview"]
        assert "increment0" in other.content["body_text_preview"]
        assert first.content["browser_session_id"] == second.content["browser_session_id"]
        assert first.content["browser_session_id"] != other.content["browser_session_id"]
        assert first.content["page_navigation_reason"] == "initial_open"
        assert second.content["page_navigation_reason"] == "none"
        assert second.content_blocks and (tmp_path / ".artifacts/browser-check.png").is_file()
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_workspace_root_screenshot_does_not_reload_active_page(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        "<button id='increment' onclick='document.querySelector(\"#count\").textContent++'>"
        "increment</button><span id='count'>0</span>",
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="root-screenshot")
    try:
        first = await port.execute(
            _request(
                "run",
                "first",
                path=".",
                screenshot_path="screenshot_initial.png",
                actions=[{"type": "click", "selector": "#increment"}],
            )
        )
        second = await port.execute(
            _request(
                "run",
                "second",
                path=".",
                screenshot_path="screenshot_after_click.png",
                actions=[{"type": "click", "selector": "#increment"}],
            )
        )
        assert first.status == second.status == "ok"
        assert "increment1" in first.content["body_text_preview"]
        assert "increment2" in second.content["body_text_preview"]
        assert second.content["page_navigation_reason"] == "none"
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_editing_a_captured_image_invalidates_the_served_page(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text('<span id="state">ready</span>', encoding="utf-8")
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="edited-capture")
    try:
        first = await port.execute(
            _request("run", "first", path=".", screenshot_path="capture.png")
        )
        assert first.status == "ok"
        capture = tmp_path / "capture.png"
        capture.write_bytes(capture.read_bytes() + b"edited")
        second = await port.execute(_request("run", "second", path=".", screenshot_path="next.png"))
        assert second.status == "ok"
        assert second.content["page_navigation_reason"] == "source_changed"
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_browser_reloads_same_page_after_served_asset_changes(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        '<span id="version">loading</span><script src="game.js"></script>',
        encoding="utf-8",
    )
    script = app / "game.js"
    script.write_text(
        "document.querySelector('#version').textContent='version-one'", encoding="utf-8"
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="test-browser")
    try:
        first = await port.execute(_request("run", "before"))
        assert first.status == "ok"
        assert "version-one" in first.content["body_text_preview"]
        script.write_text(
            "document.querySelector('#version').textContent='version-two'", encoding="utf-8"
        )
        second = await port.execute(_request("run", "after"))
        assert second.status == "ok"
        assert "version-two" in second.content["body_text_preview"]
        assert first.content["browser_session_id"] == second.content["browser_session_id"]
        assert second.content["page_navigation_reason"] == "source_changed"
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_source_edit_explains_in_game_selector_disappearing_after_reload(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        '<button id="start" onclick="this.hidden=true; '
        "document.querySelector('#mine').hidden=false\">"
        'Start</button><button id="mine" hidden>Mine</button><script src="game.js"></script>',
        encoding="utf-8",
    )
    script = app / "game.js"
    script.write_text("window.version = 1", encoding="utf-8")
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="test-browser")
    try:
        started = await port.execute(
            _request("run", "start", actions=[{"type": "click", "selector": "#start"}])
        )
        assert started.status == "ok"
        script.write_text("window.version = 2", encoding="utf-8")
        after_edit = await port.execute(
            _request("run", "after-edit", actions=[{"type": "click", "selector": "#mine"}])
        )
        assert after_edit.status == "error"
        assert after_edit.content["page_navigation_reason"] == "source_changed"
        assert after_edit.content["browser_session_id"] == started.content["browser_session_id"]
        assert after_edit.content["error_code"] == "selector_not_visible"
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_browser_blocks_external_requests_and_preserves_error_evidence(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        "<p>local</p><script>fetch('https://example.com/private')</script>", encoding="utf-8"
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="test-browser")
    try:
        result = await port.execute(_request("run", "first"))
        assert result.status == "error"
        assert result.content["request_failures"]
        assert result.content["screenshot_path"] == ".artifacts/browser-check.png"
        assert result.content_blocks
        outside = await port.execute(_request("run", "outside", screenshot_path="../outside.png"))
        assert outside.status == "error"
        assert outside.content["code"] == "screenshot_path_invalid"
        assert not (tmp_path.parent / "outside.png").exists()
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_browser_console_log_is_observation_not_execution_failure(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        "<button id='start'>Start</button><script>console.log('ready')</script>",
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="test-browser")
    try:
        result = await port.execute(_request("run", "first"))
        assert result.status == "ok"
        assert result.content["console_errors"] == []
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_missing_script_reports_exact_asset_http_failure(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        '<p>Loading</p><script type="module" src="missing/game.js"></script>',
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="test-browser")
    try:
        result = await port.execute(_request("run", "first"))
        assert result.status == "error"
        assert any(
            "asset_http_error:404: /missing/game.js" in failure
            for failure in result.content["request_failures"]
        )
        assert result.content["screenshot_path"] == ".artifacts/browser-check.png"
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_browser_script_error_reports_bounded_source_location(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        '<script type="module" src="broken.js"></script>', encoding="utf-8"
    )
    (app / "broken.js").write_text("export const value = ;", encoding="utf-8")
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="test-browser")
    try:
        result = await port.execute(_request("run", "script-location"))
        assert result.status == "error"
        assert result.content["page_errors"]
        assert any(
            location.startswith("/broken.js:1:")
            for location in result.content["page_error_locations"]
        )
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_host_verification_reload_keeps_evidence_outside_agent_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app = workspace / "app"
    app.mkdir(parents=True)
    (app / "index.html").write_text(
        "<p>built</p><script>throw new Error('broken page')</script>", encoding="utf-8"
    )
    evidence = tmp_path / "evidence"
    port = StudioNativeBrowserPort(
        scope=_SCOPE, root=workspace, runtime_id="test-browser", evidence_root=evidence
    )
    try:
        observation = await port.verify_page(
            run_id="run", entry_path="app/index.html", candidate_id="candidate"
        )
        assert observation.status == "ok"
        assert observation.page_errors
        assert list(evidence.glob("*.png"))
        assert not (workspace / ".artifacts").exists()
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_host_verification_observes_controls_after_page_settles(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text(
        "<p>Loading</p><script>"
        "setTimeout(() => {document.body.innerHTML = '<button id=\"start\">Start</button>';}, 100)"
        "</script>",
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(
        scope=_SCOPE,
        root=workspace,
        runtime_id="test-browser",
        evidence_root=tmp_path / "evidence",
    )
    try:
        observation = await port.verify_page(
            run_id="run", entry_path="index.html", candidate_id="candidate"
        )
        assert observation.status == "ok"
        assert any(control.label == "Start" for control in observation.controls)
        assert "Start" in observation.text
    finally:
        await port.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "launch_label", ["新游戏", "▶ 新游戏", "新的世界", "开始新世界", "进入星界 ▶", "New Game"]
)
async def test_host_verification_launches_one_visible_start_control_and_catches_late_error(
    tmp_path: Path,
    launch_label: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text(
        '<meta charset="utf-8"><button id="new" '
        "onclick=\"setTimeout(() => { throw new Error('game-start-broken') }, 30)\">"
        f'{launch_label}</button><canvas id="world"></canvas>',
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence"
    port = StudioNativeBrowserPort(
        scope=_SCOPE, root=workspace, runtime_id="test-browser", evidence_root=evidence
    )
    try:
        observation = await port.verify_page(
            run_id="run", entry_path="index.html", candidate_id="candidate"
        )
        assert any("game-start-broken" in error for error in observation.page_errors)
        assert observation.action_id.endswith(":3")
        assert list(evidence.glob("*.png"))
        assert not (workspace / ".artifacts").exists()
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_pointer_lock_result_is_classified_by_observed_browser_channel(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        '<button id="new" onclick="document.body.requestPointerLock()">New Game</button>',
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="test-browser")
    try:
        result = await port.execute(
            _request(
                "run",
                "click",
                wait_ms=300,
                actions=[{"type": "click", "selector": "#new"}],
            )
        )
        assert result.status == "ok"
        assert result.content["page_errors"] == []
        if result.content["browser_channel"] == "chrome":
            assert result.content["environment_warnings"] == []
        else:
            assert result.content["browser_channel"] == "bundled-chromium"
            assert any(
                "not valid for pointer lock" in item
                for item in result.content["environment_warnings"]
            )
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_obscured_click_returns_safe_feedback_then_succeeds_after_repair(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    entry = app / "index.html"
    button = (
        '<button id="new" onclick="document.querySelector(\'#count\').textContent++">'
        'New Game</button><span id="count">0</span>'
    )
    entry.write_text(
        button + '<div id="overlay" style="position:fixed;inset:0;z-index:10;background:#fff8">'
        "Loading</div>",
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="test-browser")
    try:
        blocked = await port.execute(
            _request("run", "blocked", actions=[{"type": "click", "selector": "#new"}])
        )
        assert blocked.status == "error"
        assert blocked.content["error_code"] == "selector_not_actionable"
        assert blocked.content["pointer_lock_active"] is False
        assert "New Game0" in blocked.content["body_text_preview"]
        covered = next(
            control for control in blocked.content["controls"] if control["selector"] == "#new"
        )
        assert covered["clickable"] is True
        assert covered["hit_test_reachable"] is False
        entry.write_text(button, encoding="utf-8")
        repaired = await port.execute(
            _request("run", "repaired", actions=[{"type": "click", "selector": "#new"}])
        )
        assert repaired.status == "ok"
        assert "New Game1" in repaired.content["body_text_preview"]
        uncovered = next(
            control for control in repaired.content["controls"] if control["selector"] == "#new"
        )
        assert uncovered["hit_test_reachable"] is True
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_pointer_locked_click_exposes_actionable_browser_state(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        '<canvas id="world" style="width:500px;height:300px"></canvas>'
        '<button id="start">Start</button><button id="craft" style="display:none">Craft</button>'
        '<span id="lock">pending</span><script>'
        'const world=document.querySelector("#world");world.getContext("webgl");'
        'document.querySelector("#start").onclick=()=>{'
        'document.querySelector("#craft").style.display="block";'
        "world.requestPointerLock()};"
        'document.onpointerlockchange=()=>{document.querySelector("#lock").textContent='
        'document.pointerLockElement?"locked":"unlocked"};'
        "</script>",
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="locked-click")
    try:
        opened = await port.execute(
            _request("run", "lock", actions=[{"type": "click", "selector": "#start"}])
        )
        if opened.content.get("browser_channel") != "chrome":
            pytest.skip("system Chrome is unavailable; bundled Chromium cannot test pointer lock")
        assert opened.status == "ok"
        assert "locked" in opened.content["body_text_preview"]
        blocked = await port.execute(
            _request("run", "craft", actions=[{"type": "click", "selector": "#craft"}])
        )
        assert blocked.status == "error"
        assert blocked.content["error_code"] == "selector_not_actionable"
        assert blocked.content["pointer_lock_active"] is True
        assert "Craft" in blocked.content["body_text_preview"]
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_menu_load_is_reachable_while_hud_load_is_occluded(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text(
        '<meta charset="utf-8"><style>'
        "#hud-load{position:absolute;top:20px;left:20px;z-index:10}"
        "#menu{position:fixed;inset:0;z-index:50;background:#222;"
        "display:grid;place-items:center}"
        '</style><button id="hud-load">读档 (R)</button>'
        '<div id="menu"><button id="menu-load" '
        "onclick=\"document.querySelector('#menu').remove()\">读取存档</button></div>",
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(scope=_SCOPE, root=tmp_path, runtime_id="dual-load")
    try:
        observed = await port.execute(_request("run", "observe"))
        assert observed.status == "ok"
        controls = {row["selector"]: row for row in observed.content["controls"]}
        assert controls["#hud-load"]["clickable"] is True
        assert controls["#hud-load"]["hit_test_reachable"] is False
        assert controls["#menu-load"]["hit_test_reachable"] is True
        opened = await port.execute(
            _request("run", "open", actions=[{"type": "click", "selector": "#menu-load"}])
        )
        assert opened.status == "ok"
        controls = {row["selector"]: row for row in opened.content["controls"]}
        assert controls["#hud-load"]["hit_test_reachable"] is True
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_host_verification_launches_unique_pointer_control_with_id(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text(
        '<meta charset="utf-8"><div id="btn-new" style="cursor:pointer" '
        "onclick=\"setTimeout(() => { throw new Error('div-start-broken') }, 30)\">"
        "开始新世界</div><canvas id=world></canvas>",
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(
        scope=_SCOPE, root=workspace, runtime_id="test-browser", evidence_root=tmp_path / "evidence"
    )
    try:
        observation = await port.verify_page(
            run_id="run", entry_path="index.html", candidate_id="candidate"
        )
        assert any("div-start-broken" in error for error in observation.page_errors)
        assert observation.action_id.endswith(":3")
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_duplicate_visible_launch_ids_are_observable_and_fail_cold_check(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app = workspace / "app"
    app.mkdir(parents=True)
    (app / "index.html").write_text(
        '<meta charset="utf-8"><button id="btn-new">开始新世界</button>'
        '<button id="btn-new">开始新世界</button><canvas id="world"></canvas>',
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(
        scope=_SCOPE, root=workspace, runtime_id="test-browser", evidence_root=tmp_path / "evidence"
    )
    try:
        tool_result = await port.execute(_request("run", "inspect"))
        controls = tool_result.content["controls"]
        duplicate_buttons = [control for control in controls if control["label"] == "开始新世界"]
        assert len(duplicate_buttons) == 2
        assert all(control["selector"] is None for control in duplicate_buttons)
        assert all(control["duplicate_id"] is True for control in duplicate_buttons)
        observation = await port.verify_page(
            run_id="run", entry_path="app/index.html", candidate_id="candidate"
        )
        assert observation.status == "error"
        assert observation.error_code == "browser_launch_control_ambiguous"
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_host_verification_uses_fresh_browser_storage_without_resetting_agent_page(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app = workspace / "app"
    app.mkdir(parents=True)
    (app / "index.html").write_text(
        '<button id="new">New Game</button><script>'
        "const firstVisit = !localStorage.getItem('visited');"
        "localStorage.setItem('visited', 'yes');"
        "document.querySelector('#new').onclick = () => {"
        "if (firstVisit) throw new Error('cold-start-broken');"
        "};</script>",
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(
        scope=_SCOPE,
        root=workspace,
        runtime_id="test-browser",
        evidence_root=tmp_path / "evidence",
    )
    try:
        first = await port.execute(_request("run", "opened"))
        assert first.status == "ok"
        warmed = await port.execute(_request("run", "warmed", actions=[{"type": "reload"}]))
        assert warmed.status == "ok"
        observed = await port.verify_page(
            run_id="run", entry_path="app/index.html", candidate_id="candidate"
        )
        assert any("cold-start-broken" in error for error in observed.page_errors)
        assert observed.session_ref.session_id != first.content["browser_session_id"]
        resumed = await port.execute(
            _request("run", "continued", actions=[{"type": "click", "selector": "#new"}])
        )
        assert resumed.status == "ok"
        assert resumed.content["browser_session_id"] == first.content["browser_session_id"]
    finally:
        await port.aclose()


@pytest.mark.asyncio
async def test_host_verification_does_not_click_ambiguous_launch_controls(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text(
        '<button id="one" onclick="throw new Error(\'wrong-click\')">Start</button>'
        '<button id="two" onclick="throw new Error(\'wrong-click\')">Play</button>',
        encoding="utf-8",
    )
    port = StudioNativeBrowserPort(
        scope=_SCOPE,
        root=workspace,
        runtime_id="test-browser",
        evidence_root=tmp_path / "evidence",
    )
    try:
        observation = await port.verify_page(
            run_id="run", entry_path="index.html", candidate_id="candidate"
        )
        assert observation.action_id.endswith(":1")
        assert observation.status == "ok"
        assert not observation.page_errors
    finally:
        await port.aclose()


class _TwoTurnBrowserClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def complete(self, _model, **kwargs):
        self.requests.append(kwargs)
        turn = len(self.requests)
        calls = (
            [
                ToolCall(
                    id=f"browser-{turn}",
                    name="browser_inspect_workspace",
                    arguments=(
                        '{"path":"app/index.html","wait_ms":0,'
                        '"actions":[{"type":"click","selector":"#increment"}]}'
                    ),
                )
            ]
            if turn <= 2
            else []
        )
        return ModelResponse(
            content="2" if turn == 3 else "",
            finish_reason="stop" if turn == 3 else "tool_calls",
            usage=Usage(input_tokens=20, output_tokens=8, total_tokens=28, reported=True),
            tool_calls=calls,
            raw_message={},
        )


@pytest.mark.asyncio
async def test_ordinary_studio_native_keeps_browser_page_across_model_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "local")
    monkeypatch.setenv("NATIVE_PRODUCT_TEST_KEY", "fixture-key")
    root = tmp_path / "studio"
    app = root / ".harness-tools" / "workspace" / "app"
    app.mkdir(parents=True)
    (app / "index.html").write_text(
        "<button id='increment' onclick='document.querySelector(\"#count\").textContent++'>"
        "increment</button><span id='count'>0</span>",
        encoding="utf-8",
    )
    client = _TwoTurnBrowserClient()
    studio = StudioService(
        root, model_client=client, native_harness_agent_ids=("browser-native-agent",)
    )
    try:
        browser = next(
            item
            for item in studio.catalog.list(kind="tool", limit=200)
            if item.name == "browser_inspect_workspace"
        )
        studio.create_agent(
            agent_id="browser-native-agent",
            name="Browser continuity",
            spec=AgentSpec(
                runtime=RuntimeRef(type="harness"),
                instructions=Instructions(system="Use the bound browser."),
                model=ModelSpec(
                    model="fixture-model",
                    endpoint_url="https://example.invalid/v1/chat/completions",
                    credential_ref="env://NATIVE_PRODUCT_TEST_KEY",
                ),
                bindings=AgentBindings(
                    policy_template="loose",
                    tools=[CapabilityBinding(resource_id=browser.resource_id)],
                ),
                execution=ExecutionSpec(max_steps=8, timeout_seconds=180),
                security=SecuritySpec(
                    allowed_permissions=["process:host-user"],
                    network=NetworkPolicy(allowed_hosts=["example.invalid"]),
                ),
            ),
        )
        build = await studio.ensure_current_build("browser-native-agent")
        result = await studio.run_build(
            build.id,
            "Click increment twice in the same browser page, then report the count.",
            "browser-continuity-session",
            approval_mode="full",
        )
        assert result.status == RunStatus.COMPLETED, result.error
        assert len(client.requests) == 3
        assert "increment1" in str(client.requests[1]["messages"])
        assert "increment2" in str(client.requests[2]["messages"]), client.requests[2]["messages"][
            -1
        ]
    finally:
        await studio.aclose()


class _DeliveryCheckpointClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def complete(self, _model, **kwargs):
        self.requests.append(kwargs)
        turn = len(self.requests)
        if turn in {1, 2}:
            path = "app/index.html" if turn == 1 else "app/autotest.html"
            content = "<button>Start</button>" if turn == 1 else "<p>test fixture</p>"
            calls = [
                ToolCall(
                    id=f"write-{turn}",
                    name="write_workspace_file",
                    arguments=json.dumps({"path": path, "content": content}),
                )
            ]
        elif turn in {3, 4}:
            path = "app/autotest.html" if turn == 3 else "app/index.html"
            calls = [
                ToolCall(
                    id=f"browser-{turn}",
                    name="browser_inspect_workspace",
                    arguments=json.dumps({"path": path, "wait_ms": 0}),
                )
            ]
        else:
            calls = []
        return ModelResponse(
            content="Created and inspected the page." if not calls else "",
            finish_reason="stop" if not calls else "tool_calls",
            usage=Usage(input_tokens=20, output_tokens=8, total_tokens=28, reported=True),
            tool_calls=calls,
            raw_message={},
        )


def _verification_observation(messages):
    for message in reversed(messages):
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except ValueError:
            continue
        if isinstance(value, dict) and {"failed", "blocked", "not_run"} <= value.keys():
            return value
    raise AssertionError("Expected structured verification feedback")


async def _native_snapshot(studio, record):
    metadata = await studio.session_service.get_session_metadata(record.session_id)
    scope = RunScope(
        tenant_id="local-studio", workspace_id=studio.workspace_record.workspace_id,
        user_id=metadata.user_id, agent_id=metadata.agent_id,
    )
    return studio._native_product_composer.store.load(scope, record.runtime_handle["run_id"])


@pytest.mark.asyncio
async def test_browser_on_auxiliary_page_triggers_delivery_checkpoint_before_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "local")
    monkeypatch.setenv("NATIVE_PRODUCT_TEST_KEY", "fixture-key")
    client = _DeliveryCheckpointClient()
    studio = StudioService(
        tmp_path / "studio",
        model_client=client,
        native_harness_agent_ids=("delivery-checkpoint-agent",),
    )
    try:
        resources = {item.name: item for item in studio.catalog.list(kind="tool", limit=200)}
        studio.create_agent(
            agent_id="delivery-checkpoint-agent",
            name="Delivery checkpoint",
            spec=AgentSpec(
                runtime=RuntimeRef(type="harness"),
                instructions=Instructions(system="Complete the user's web task."),
                model=ModelSpec(
                    model="fixture-model",
                    endpoint_url="https://example.invalid/v1/chat/completions",
                    credential_ref="env://NATIVE_PRODUCT_TEST_KEY",
                ),
                bindings=AgentBindings(
                    policy_template="loose",
                    tools=[
                        CapabilityBinding(resource_id=resources[name].resource_id)
                        for name in ("write_workspace_file", "browser_inspect_workspace")
                    ],
                ),
                execution=ExecutionSpec(max_steps=12, timeout_seconds=180),
                security=SecuritySpec(
                    allowed_permissions=["process:host-user", "workspace:file:write"],
                    network=NetworkPolicy(allowed_hosts=["example.invalid"]),
                ),
            ),
        )
        build = await studio.ensure_current_build("delivery-checkpoint-agent")
        result = await studio.run_build(
            build.id,
            "Create a working web page in app/index.html and inspect it.",
            "delivery-checkpoint-session",
            approval_mode="full",
        )
        assert result.status == RunStatus.COMPLETED, result.error
        assert len(client.requests) == 5
        feedback = _verification_observation(client.requests[3]["messages"])
        assert feedback["failed"] == feedback["not_run"] == []
        (blocked,) = feedback["blocked"]
        assert blocked["status"] == "blocked"
        assert "changed files belong to multiple pages" in blocked["safe_summary"]
        snapshot = await _native_snapshot(studio, result)
        assert result.verification_outcome == snapshot.outcome == "blocked"
        assert snapshot.repair_attempts == 0 and not snapshot.verification["repairable"]
        with sqlite3.connect(
            tmp_path / "studio" / ".agentengine" / "ui" / "sessions.sqlite"
        ) as connection:
            checkpoint_rows = connection.execute(
                "SELECT content_json FROM ksadk_events "
                "WHERE content_json LIKE '%verification.checkpoint_started%'"
            ).fetchall()
        checkpoint_ids = {
            json.loads(row[0])["runtime_event"]["source"]["native_event_id"]
            for row in checkpoint_rows
        }
        assert len(checkpoint_ids) == 1
    finally:
        await studio.aclose()


class _BrokenThenRepairedPageClient:
    def __init__(
        self,
        *,
        interactive: bool = False,
        duplicate_launch: bool = False,
        obscured_launch: bool = False,
    ) -> None:
        self.requests: list[dict] = []
        self.interactive = interactive
        self.duplicate_launch = duplicate_launch
        self.obscured_launch = obscured_launch

    async def complete(self, _model, **kwargs):
        self.requests.append(kwargs)
        turn = len(self.requests)
        if turn in {1, 3}:
            if self.duplicate_launch:
                content = (
                    '<button id="start">Start</button><button id="start">Start</button>'
                    if turn == 1
                    else '<p>repaired</p><button id="start">Start</button>'
                )
            elif self.obscured_launch:
                content = (
                    '<button id="start">Start</button>'
                    '<div style="position:fixed;inset:0;z-index:10;background:#fff8">Loading</div>'
                    if turn == 1
                    else '<p>repaired</p><button id="start">Start</button>'
                )
            elif self.interactive:
                behavior = (
                    "setTimeout(() => {throw new Error('start-broken')}, 30)"
                    if turn == 1
                    else "window.ready = true"
                )
                content = (
                    f"<p>{'built' if turn == 1 else 'repaired'}</p>"
                    f'<button id="start" onclick="{behavior}">Start</button>'
                )
            else:
                content = (
                    "<p>built</p><script>throw new Error('broken page')</script>"
                    if turn == 1
                    else "<p>repaired</p><script>window.ready = true</script>"
                )
            calls = [
                ToolCall(
                    id=f"write-{turn}",
                    name="write_workspace_file",
                    arguments='{"path":"app/index.html","content":' + json.dumps(content) + "}",
                )
            ]
        elif turn == 4:
            calls = [
                ToolCall(
                    id="observe-repaired-page",
                    name="browser_inspect_workspace",
                    arguments=json.dumps(
                        {
                            "path": "app/index.html",
                            "actions": (
                                [{"type": "click", "selector": "#start"}]
                                if self.interactive
                                else []
                            ),
                            "wait_ms": 100,
                        }
                    ),
                )
            ]
        else:
            calls = []
        return ModelResponse(
            content="The page is ready." if not calls else "",
            finish_reason="tool_calls" if calls else "stop",
            usage=Usage(input_tokens=20, output_tokens=8, total_tokens=28, reported=True),
            tool_calls=calls,
            raw_message={},
        )


class _LateRepairPageClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def complete(self, _model, **kwargs):
        self.requests.append(kwargs)
        turn = len(self.requests)
        if turn in {1, 9}:
            content = (
                "<p>built</p><script>throw new Error('late broken page')</script>"
                if turn == 1
                else "<p>repaired</p><script>window.ready = true</script>"
            )
            name = "write_workspace_file"
            arguments = json.dumps({"path": "app/index.html", "content": content})
        elif turn <= 8:
            name = "read_workspace_file"
            arguments = json.dumps({"path": "app/index.html"})
        elif turn == 10:
            name = "browser_inspect_workspace"
            arguments = json.dumps({"path": "app/index.html", "wait_ms": 100})
        else:
            name = None
            arguments = ""
        calls = (
            [ToolCall(id=f"call-{turn}", name=name, arguments=arguments)]
            if name is not None
            else []
        )
        return ModelResponse(
            content="The page is ready." if not calls else "",
            finish_reason="tool_calls" if calls else "stop",
            usage=Usage(input_tokens=20, output_tokens=8, total_tokens=28, reported=True),
            tool_calls=calls,
            raw_message={},
        )


@pytest.mark.asyncio
async def test_studio_native_checks_dirty_workspace_before_model_budget_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "local")
    monkeypatch.setenv("NATIVE_PRODUCT_TEST_KEY", "fixture-key")
    root = tmp_path / "studio"
    client = _LateRepairPageClient()
    studio = StudioService(
        root, model_client=client, native_harness_agent_ids=("browser-native-agent",)
    )
    try:
        catalog = {item.name: item for item in studio.catalog.list(kind="tool", limit=200)}
        studio.create_agent(
            agent_id="browser-native-agent",
            name="Late browser repair",
            spec=AgentSpec(
                runtime=RuntimeRef(type="harness"),
                instructions=Instructions(system="Build and check the page."),
                model=ModelSpec(
                    model="fixture-model",
                    endpoint_url="https://example.invalid/v1/chat/completions",
                    credential_ref="env://NATIVE_PRODUCT_TEST_KEY",
                ),
                bindings=AgentBindings(
                    policy_template="loose",
                    tools=[
                        CapabilityBinding(resource_id=catalog[name].resource_id)
                        for name in (
                            "write_workspace_file",
                            "read_workspace_file",
                            "browser_inspect_workspace",
                        )
                    ],
                ),
                execution=ExecutionSpec(max_steps=12, timeout_seconds=180),
                security=SecuritySpec(
                    allowed_permissions=["process:host-user"],
                    network=NetworkPolicy(allowed_hosts=["example.invalid"]),
                ),
            ),
        )
        build = await studio.ensure_current_build("browser-native-agent")
        result = await studio.run_build(
            build.id,
            "Build a working page in app/index.html.",
            "late-browser-repair-session",
            approval_mode="full",
        )
        assert result.status == RunStatus.COMPLETED, result.error
        assert len(client.requests) == 11
        assert "Browser reported an error" in str(client.requests[5]["messages"])
        assert "Browser reported an error" in str(client.requests[8]["messages"])
        assert "repaired" in (root / ".harness-tools/workspace/app/index.html").read_text()
        assert len(list((root / ".harness-tools/state/browser-verification").glob("*.png"))) >= 2
    finally:
        await studio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["script_error", "late_click_error", "duplicate_launch", "obscured_launch"]
)
async def test_ordinary_studio_native_browser_failure_reenters_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "local")
    monkeypatch.setenv("NATIVE_PRODUCT_TEST_KEY", "fixture-key")
    root = tmp_path / "studio"
    client = _BrokenThenRepairedPageClient(
        interactive=mode == "late_click_error",
        duplicate_launch=mode == "duplicate_launch",
        obscured_launch=mode == "obscured_launch",
    )
    observations = []
    verify_page = StudioNativeBrowserPort.verify_page

    async def observe_verification(port, **kwargs):
        observation = await verify_page(port, **kwargs)
        observations.append(observation)
        return observation

    monkeypatch.setattr(StudioNativeBrowserPort, "verify_page", observe_verification)
    studio = StudioService(
        root, model_client=client, native_harness_agent_ids=("browser-native-agent",)
    )
    try:
        catalog = {item.name: item for item in studio.catalog.list(kind="tool", limit=200)}
        studio.create_agent(
            agent_id="browser-native-agent",
            name="Browser repair",
            spec=AgentSpec(
                runtime=RuntimeRef(type="harness"),
                instructions=Instructions(system="Build and check the page."),
                model=ModelSpec(
                    model="fixture-model",
                    endpoint_url="https://example.invalid/v1/chat/completions",
                    credential_ref="env://NATIVE_PRODUCT_TEST_KEY",
                ),
                bindings=AgentBindings(
                    policy_template="loose",
                    tools=[
                        CapabilityBinding(resource_id=catalog[name].resource_id)
                        for name in ("write_workspace_file", "browser_inspect_workspace")
                    ],
                ),
                execution=ExecutionSpec(max_steps=12, timeout_seconds=180),
                security=SecuritySpec(
                    allowed_permissions=["process:host-user"],
                    network=NetworkPolicy(allowed_hosts=["example.invalid"]),
                ),
            ),
        )
        build = await studio.ensure_current_build("browser-native-agent")
        result = await studio.run_build(
            build.id,
            "Build a working page in app/index.html.",
            "browser-repair-session",
            approval_mode="full",
        )
        assert result.status == RunStatus.COMPLETED, result.error
        if mode == "obscured_launch":
            assert len(client.requests) == 2
            (observation,) = observations
            assert observation.status == "error"
            assert observation.error_code == "selector_not_actionable"
            assert observation.complete and observation.retryability == "safe"
            (start,) = [control for control in observation.controls if control.label == "Start"]
            assert start.clickable and start.hit_test_reachable is False
            snapshot = await _native_snapshot(studio, result)
            assert result.verification_outcome == snapshot.outcome == "blocked"
            assert snapshot.repair_attempts == 0 and not snapshot.verification["repairable"]
            (item,) = snapshot.verification["items"]
            assert item["status"] == "blocked" and item["evidence_refs"]
            assert "incomplete host observation" in item["safe_summary"]
            assert "Loading" in (root / ".harness-tools/workspace/app/index.html").read_text()
            assert observation.screenshot is not None
            return
        assert len(client.requests) == 5
        expected = (
            "multiple visible start controls"
            if mode == "duplicate_launch"
            else "Browser reported an error"
        )
        assert expected in str(client.requests[2]["messages"])
        assert "repaired" in (root / ".harness-tools/workspace/app/index.html").read_text()
        evidence = root / ".harness-tools/state/browser-verification"
        assert len(list(evidence.glob("*.png"))) >= 2
    finally:
        await studio.aclose()
