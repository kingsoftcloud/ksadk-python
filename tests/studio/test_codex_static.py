from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app


def test_codex_capabilities_are_integrated_into_the_original_studio_shell(
    tmp_path: Path,
) -> None:
    app = create_studio_app(tmp_path, security_enabled=False)
    with TestClient(app) as client:
        page = client.get("/")
        app_script = client.get("/static/app.js")
        shared_script = client.get("/static/vendor/shared-web.js")
        stylesheet = client.get("/static/app.css")

    assert page.status_code == 200
    assert 'class="app-shell"' in page.text
    assert 'class="primary-nav"' in page.text
    assert 'id="view-agents"' in page.text
    assert 'id="view-create"' in page.text
    assert 'id="view-agent-detail"' in page.text
    assert 'id="view-chat"' in page.text
    assert 'id="view-resources"' in page.text
    assert 'id="view-builds"' in page.text
    assert 'id="view-deployments"' in page.text
    assert 'id="view-observability"' in page.text
    assert 'id="runtimeManifestDigest"' in page.text
    assert 'codex-studio.js' not in page.text
    assert '<script src="/static/app.js" defer></script>' in page.text
    assert '<script type="module" src="/static/vendor/shared-web.js"></script>' in page.text
    assert 'id="quickAgentEditor"' in page.text
    assert 'id="deleteSessionOverlay"' in page.text
    assert 'id="detailEdit"' in page.text
    assert 'id="detailDelete"' in page.text
    assert 'id="deleteAgentOverlay"' in page.text
    assert "<iframe" not in page.text
    assert app_script.status_code == 200
    assert stylesheet.status_code == 200
    assert shared_script.status_code == 200
    assert "normalizeCapabilities" in shared_script.text
    assert "CodexStudio" not in app_script.text
    assert "recoverBrowserSession" in app_script.text
    assert 'error.code === "LOCAL_SESSION_REQUIRED"' in app_script.text
    assert "selectDefaultAgent" in app_script.text
    assert "openEditAgent" in app_script.text
    assert "deleteAgent" in app_script.text
    assert "localStorage" not in app_script.text
    assert "sessionStorage" not in app_script.text


def test_static_modules_stay_small_enough_for_independent_iteration() -> None:
    root = Path(__file__).resolve().parents[2] / "ksadk/studio/static"
    shared = root / "vendor/shared-web.js"

    assert not (root / "codex-studio.js").exists()
    assert shared.stat().st_size <= 100 * 1024


def test_observability_is_an_independent_otlp_trace_explorer(tmp_path: Path) -> None:
    """Break caught: clicking Trace navigates to Chat and renders custom RunEvents."""

    app = create_studio_app(tmp_path, security_enabled=False)
    with TestClient(app) as client:
        markup = client.get("/").text
        script = client.get("/static/app.js").text

    for element_id in (
        "traceExplorer",
        "traceAgentFilter",
        "traceStatusFilter",
        "traceList",
        "traceSpanTree",
        "traceDetail",
        "traceRawOtlp",
        "traceMetricDuration",
        "traceMetricTokens",
    ):
        assert f'id="{element_id}"' in markup
    assert 'data-trace-tab="attributes"' in markup
    assert 'data-trace-tab="events"' in markup
    assert 'data-trace-tab="resource"' in markup
    assert 'data-trace-tab="raw"' in markup
    assert "async function openTrace(" in script
    open_trace = script.split("async function openTrace(", 1)[1].split("\nfunction ", 1)[0]
    assert "openChat(" not in open_trace
    assert 'params.get("traceId")' in script
    assert 'url.searchParams.set("traceId"' in script
    assert "data-open-trace" in script
    assert "data-open-run" not in script


def test_trace_explorer_has_scroll_and_waterfall_layout_contracts() -> None:
    stylesheet = (
        Path(__file__).resolve().parents[2] / "ksadk/studio/static/app.css"
    ).read_text(encoding="utf-8")

    for selector, declarations in {
        ".trace-workbench {": ["grid-template-columns", "min-height: 0", "overflow: hidden"],
        ".trace-list {": ["overflow-y: auto", "min-height: 0"],
        ".trace-span-tree {": ["overflow: auto", "min-width: 0"],
        ".trace-detail-body {": ["overflow: auto", "min-height: 0"],
        ".trace-raw {": ["overflow: auto", "white-space: pre"],
    }.items():
        start = stylesheet.index(selector)
        block = stylesheet[start:stylesheet.index("}", start)]
        for declaration in declarations:
            assert declaration in block


def test_trace_detail_can_expand_and_raw_otlp_scrolls_inside_viewport(
    tmp_path: Path,
) -> None:
    app = create_studio_app(tmp_path, security_enabled=False)
    with TestClient(app) as client:
        markup = client.get("/").text
        script = client.get("/static/app.js").text
        stylesheet = client.get("/static/app.css").text

    assert 'id="traceWorkbench"' in markup
    assert 'id="toggleTraceDetail"' in markup
    assert "function setTraceDetailExpanded(" in script
    assert 'classList.toggle("detail-expanded"' in script
    assert 'classList.toggle("raw-active"' in script
    assert 'if (state.traceTab === "raw") setTraceDetailExpanded(true);' in script

    for selector, declarations in {
        ".trace-workbench.detail-expanded {": ["grid-template-columns"],
        ".trace-detail-body.raw-active {": ["overflow: hidden"],
        ".trace-detail-body.raw-active .trace-raw {": [
            "height: 100%",
            "min-height: 0",
            "max-height: 100%",
            "overflow: auto",
            "scrollbar-gutter: stable",
        ],
    }.items():
        start = stylesheet.index(selector)
        block = stylesheet[start:stylesheet.index("}", start)]
        for declaration in declarations:
            assert declaration in block
