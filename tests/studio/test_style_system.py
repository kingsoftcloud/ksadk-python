from __future__ import annotations

import re
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not (REPOSITORY_ROOT / "ksadk/studio/react-ui/src").is_dir(),
    reason="editable Studio source is intentionally absent from the public candidate",
)
STYLESHEET = REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "studio.css"
REACT_STYLESHEET = REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "index.css"
APP_SOURCE = REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "App.tsx"
RESPONSIVE_STYLESHEET = REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "responsive.css"
THEME_STYLESHEET = REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "theme.css"
CHAT_SOURCE = (
    REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "components" / "ChatWorkspace.tsx"
)
CHAT_COMPOSER_SOURCE = (
    REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "components" / "ChatComposer.tsx"
)
ORCHESTRATION_SOURCE = (
    REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "pages" / "OrchestrationPage.tsx"
)
OBSERVABILITY_SOURCE = (
    REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "pages" / "ObservabilityPage.tsx"
)
RUNTIME_RESOURCES_SOURCE = (
    REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "pages" / "RuntimeResourcesPage.tsx"
)
PACKAGE = REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "package.json"
MAKEFILE = REPOSITORY_ROOT / "Makefile"


def _stylesheet_without_root() -> tuple[str, str]:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    match = re.search(r":root\s*\{(?P<tokens>.*?)\n\}", stylesheet, re.DOTALL)
    assert match is not None
    return stylesheet, stylesheet[: match.start()] + stylesheet[match.end() :]


def _block(stylesheet: str, selector: str) -> str:
    start = stylesheet.find(selector)
    assert start >= 0, f"missing selector {selector}"
    return stylesheet[start : stylesheet.find("}", start)]


def test_react_ci_gate_runs_protocol_and_component_suites() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    target = makefile.split("studio-react-test:\n", 1)[1].split("\n\n", 1)[0]

    assert "npm --prefix ksadk/studio/react-ui test" in target
    assert "npm --prefix ksadk/studio/react-ui run test:ui" in target


def test_typography_tokens_form_the_complete_product_scale() -> None:
    stylesheet, _ = _stylesheet_without_root()
    required_tokens = {
        "--font-sans",
        "--font-mono",
        "--font-size-caption",
        "--font-size-meta",
        "--font-size-control",
        "--font-size-body",
        "--font-size-subtitle",
        "--font-size-card-metric",
        "--font-size-section-title",
        "--font-size-page-title",
        "--font-size-metric",
        "--font-weight-regular",
        "--font-weight-medium",
        "--font-weight-semibold",
        "--line-height-caption",
        "--line-height-control",
        "--line-height-body",
        "--line-height-editor",
        "--line-height-title",
    }

    for token in required_tokens:
        assert re.search(rf"{re.escape(token)}\s*:", stylesheet), f"missing {token}"


def test_component_surface_and_code_tokens_exist_in_both_themes() -> None:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    theme = THEME_STYLESHEET.read_text(encoding="utf-8")
    light_root = _block(stylesheet, ":root {")
    dark_root = _block(theme, ":root.dark {")
    required = {
        "--surface-raised",
        "--surface-sunken",
        "--surface-hover",
        "--text-primary",
        "--text-label",
        "--text-faint",
        "--accent-strong",
        "--button-primary-bg",
        "--button-primary-bg-hover",
        "--button-primary-bg-active",
        "--button-primary-text",
        "--border-card",
        "--warning-text",
        "--code-token-keyword",
        "--code-token-string",
        "--code-token-function",
    }
    for token in required:
        assert f"{token}:" in light_root, f"missing light token {token}"
        assert f"{token}:" in dark_root, f"missing dark token {token}"
    assert "--code-bg: #202631" not in light_root
    assert "--code-bg: #090f19" not in dark_root


def test_shared_component_css_uses_semantic_typography_tokens() -> None:
    _, component_css = _stylesheet_without_root()
    forbidden_declarations = {
        "font-size": r"font-size\s*:\s*\d",
        "font-weight": r"font-weight\s*:\s*\d",
        "line-height": r"line-height\s*:\s*(?:\d|\.\d)",
    }

    for name, pattern in forbidden_declarations.items():
        match = re.search(pattern, component_css)
        assert match is None, f"{name} must use a semantic token: {match.group(0)!r}"

    font_shorthands = re.findall(r"(?<!-)font\s*:\s*([^;]+);", component_css)
    assert font_shorthands
    assert set(font_shorthands) == {"inherit"}


def test_core_components_keep_shared_visual_contracts() -> None:
    stylesheet, _ = _stylesheet_without_root()
    expected_contracts = {
        ".page-header h1": "--font-size-page-title",
        ".button,\n.icon-button": "--button-height",
        ".field label": "--font-size-control",
        ".status-badge": "--status-height",
        "input,\nselect {": "--control-height",
    }

    for selector, token in expected_contracts.items():
        assert token in _block(stylesheet, selector)


def test_trace_explorer_keeps_nested_scroll_and_waterfall_layouts() -> None:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    for selector, declarations in {
        ".trace-workbench {": ["grid-template-columns", "min-height: 0", "overflow: hidden"],
        ".trace-list {": ["overflow-y: auto", "min-height: 0"],
        ".trace-span-tree {": ["overflow: auto", "min-width: 0"],
        ".trace-detail-body {": ["overflow: auto", "min-height: 0"],
        ".trace-raw {": ["overflow: hidden", "background: var(--surface)"],
        ".trace-raw-tree {": ["overflow: auto", "min-height: 0"],
    }.items():
        block = _block(stylesheet, selector)
        for declaration in declarations:
            assert declaration in block


def test_react_chat_and_batch_surfaces_keep_bounded_layouts() -> None:
    stylesheet = REACT_STYLESHEET.read_text(encoding="utf-8")
    for selector, declarations in {
        ".chat-wrap {": ["min-height: 0", "overflow: hidden"],
        ".chat-host {": ["min-height: 0", "overflow: hidden"],
        ".chat-run-timeline {": ["overflow-y: auto"],
        ".skill-selection-toolbar {": ["display: flex", "justify-content: space-between"],
    }.items():
        block = _block(stylesheet, selector)
        for declaration in declarations:
            assert declaration in block


def test_responsive_layer_is_the_final_layout_owner() -> None:
    entrypoint = REACT_STYLESHEET.read_text(encoding="utf-8")
    responsive = RESPONSIVE_STYLESHEET.read_text(encoding="utf-8")

    assert entrypoint.index('@import "./studio.css"') < entrypoint.index(
        '@import "./responsive.css"'
    )
    assert "@media (max-width: 1023px)" in responsive
    assert "@media (min-width: 1024px) and (max-width: 1439px)" in responsive
    assert "--studio-page-max: 1760px" in responsive

    for selector, declarations in {
        ".app-shell .data-scroll-region {": ["overflow-x: auto", "max-width: 100%"],
        '.create-shell[data-scroll-mode="workbench"] {': [
            "height: calc(100dvh - 64px)",
            "overflow: hidden",
        ],
        '.app-shell[data-view="conversations"] {\n  height: 100dvh;': ["overflow: hidden"],
        '.app-shell[data-view="conversations"] #mainContent,': ["height: 100%"],
        ".overlay .drawer {": [
            "calc(100vw - 32px)",
            "calc(100dvh - 32px)",
        ],
    }.items():
        block = _block(responsive, selector)
        for declaration in declarations:
            assert declaration in block


def test_theme_layer_supports_persisted_light_dark_and_system_modes() -> None:
    entrypoint = REACT_STYLESHEET.read_text(encoding="utf-8")
    theme = THEME_STYLESHEET.read_text(encoding="utf-8")
    app = APP_SOURCE.read_text(encoding="utf-8")

    assert (
        entrypoint.index('@import "./studio.css"')
        < entrypoint.index('@import "./theme.css"')
        < entrypoint.index('@import "./responsive.css"')
    )
    assert ":root.dark {" in theme
    assert "color-scheme: dark" in theme
    assert ".appearance-options {" in theme
    assert "useStudioTheme" in app


def test_react_chat_composer_has_one_focus_boundary() -> None:
    stylesheet = REACT_STYLESHEET.read_text(encoding="utf-8")
    textarea_block = _block(stylesheet, ".chat-composer textarea {")
    assert "border: 0" in textarea_block
    assert "background: transparent" in textarea_block
    assert "box-shadow: none" in textarea_block
    assert ".chat-composer:focus-within {" in stylesheet


def test_react_chat_uses_shared_protocol_and_asymmetric_messages() -> None:
    stylesheet = REACT_STYLESHEET.read_text(encoding="utf-8")
    source = CHAT_SOURCE.read_text(encoding="utf-8")
    package = PACKAGE.read_text(encoding="utf-8")
    vite_config = (REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "vite.config.ts").read_text(
        encoding="utf-8"
    )

    user_bubble = _block(stylesheet, ".chat-message-list .message.user .message-content {")
    assistant = _block(stylesheet, ".chat-message-list .message.assistant .message-content {")
    assert "margin-left: auto" in user_bubble
    assert "width: fit-content" in user_bubble
    assert "padding: 0" in assistant
    assert "ChatWorkspace" in source
    assert 'apiFetch("/v1/responses"' in source
    assert '"@kingsoftcloud/ksadk-web": "0.3.3"' in package
    assert "@kingsoftcloud/ksadk-web" not in vite_config
    # 没有本地 Agent 时仍可从账号目录选择云端 Agent，不再把会话入口
    # 强制重定向到创建页。
    assert "先创建 Agent 才能开始会话" not in APP_SOURCE.read_text(encoding="utf-8")


def test_react_chat_keeps_compact_sessions_and_streaming_controls() -> None:
    stylesheet = REACT_STYLESHEET.read_text(encoding="utf-8")
    source = CHAT_SOURCE.read_text(encoding="utf-8")

    session = _block(stylesheet, ".chat-session-item {")
    assert "min-height: 34px" in session
    assert ".sr-only {" in stylesheet
    assert "@keyframes chat-message-enter" in stylesheet
    assert "@keyframes chat-stream-caret" in stylesheet
    assert "@media (prefers-reduced-motion: reduce)" in stylesheet
    assert "<time>" not in source
    assert "chat-composer-hint" not in source
    assert 'aria-label="暂停生成"' in source
    assert "<Pause" in source


def test_react_chat_composer_owns_three_turn_scoped_approval_levels() -> None:
    stylesheet = REACT_STYLESHEET.read_text(encoding="utf-8")
    source = CHAT_SOURCE.read_text(encoding="utf-8")
    composer_source = CHAT_COMPOSER_SOURCE.read_text(encoding="utf-8")
    approval_source = (
        REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "approvalModes.ts"
    ).read_text(encoding="utf-8")

    assert 'value: "ask"' in approval_source
    assert 'value: "risk"' in approval_source
    assert 'value: "full"' in approval_source
    assert "请求批准" in approval_source
    assert "帮我批准" in approval_source
    assert "完全访问权限" in approval_source
    assert "approval_mode: approvalModeForTurn" in source
    assert "下一轮生效" in composer_source
    assert "<ChatComposer" in source
    assert ".chat-approval-trigger" in stylesheet
    assert ".chat-approval-menu" in stylesheet


def test_observability_keeps_trends_visible_with_an_active_trace() -> None:
    responsive = RESPONSIVE_STYLESHEET.read_text(encoding="utf-8")
    source = (
        REPOSITORY_ROOT
        / "ksadk"
        / "studio"
        / "react-ui"
        / "src"
        / "pages"
        / "ObservabilityPage.tsx"
    ).read_text(encoding="utf-8")

    workbench_overview = _block(
        responsive,
        '.app-shell .observability-page[data-scroll-mode="workbench"] .observability-overview {',
    )
    workbench_body = _block(
        responsive,
        '.app-shell .observability-page[data-scroll-mode="workbench"] .observability-body {',
    )
    assert "display: grid" in workbench_overview
    assert "display: none" not in workbench_overview
    assert "overflow-y: auto" in workbench_body
    assert "运行趋势" in source
    assert "overview-metric-card" in source
    assert "trace-workbench" in source


def test_orchestration_pipeline_uses_a_compact_adaptive_graph_canvas() -> None:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    responsive = RESPONSIVE_STYLESHEET.read_text(encoding="utf-8")
    source = ORCHESTRATION_SOURCE.read_text(encoding="utf-8")
    graph = _block(stylesheet, ".orchestration-graph {")
    edge_label = _block(stylesheet, ".orchestration-graph .react-flow__edge-text {")

    assert "height: clamp(380px, 48dvh, 500px)" in graph
    assert "min-height: 540px" not in graph
    assert "overflow: hidden" in graph
    assert "stroke: none" in edge_label
    assert 'from "@xyflow/react"' in source
    assert 'data-layout="adaptive-serpentine"' in source
    assert 'data-background="plain"' in source
    assert "BackgroundVariant" not in source
    assert "<Background" not in source
    assert "layoutPipeline" in source
    assert "fitView" in source
    assert "适应画布" in source
    assert "Local Runtime" not in source
    assert "route-mode-badge" not in source
    assert "pipeline-location" not in source
    assert ".orchestration-aside-section" in stylesheet
    assert "@media (min-width: 768px) and (max-width: 1439px)" in responsive


def test_compact_selects_keep_their_width_inside_trace_toolbar() -> None:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    toolbar = _block(stylesheet, ".trace-toolbar {")
    search = _block(stylesheet, ".trace-search-field {")
    compact = _block(stylesheet, ".studio-select-trigger.compact-select {")

    assert "flex-wrap: wrap" in toolbar
    assert "min-width: 220px" in search
    assert "max-width: 380px" in search
    assert "flex: 0 0 156px" in compact


def test_global_header_only_repeats_context_for_nested_pages() -> None:
    app = APP_SOURCE.read_text(encoding="utf-8")

    assert 'className={`global-header${breadcrumbParent ? " nested" : ""}`}' in app
    assert "{breadcrumbParent && (" in app
    assert "breadcrumbTitle" in app


def test_page_headers_do_not_float_runtime_status_badges() -> None:
    runtime_resources = RUNTIME_RESOURCES_SOURCE.read_text(encoding="utf-8")
    observability = OBSERVABILITY_SOURCE.read_text(encoding="utf-8")
    agents = (
        REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "pages" / "AgentsPage.tsx"
    ).read_text(encoding="utf-8")

    assert "Local Ready" not in runtime_resources
    assert "Local Ready" not in agents
    assert 'className="trace-target"' not in observability


def test_trace_detail_can_collapse_and_raw_otlp_uses_a_light_json_tree() -> None:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    responsive = RESPONSIVE_STYLESHEET.read_text(encoding="utf-8")
    source = OBSERVABILITY_SOURCE.read_text(encoding="utf-8")
    package = PACKAGE.read_text(encoding="utf-8")

    assert 'from "react-json-view-lite"' in source
    assert "<JsonView" in source
    assert "allExpanded" in source
    assert "collapseAllNested" in source
    assert "全部展开" in source
    assert "全部收起" in source
    assert "detailCollapsed" in source
    assert "收起详情" in source
    assert "展开详情" in source
    assert '<pre className="trace-raw"' not in source
    assert '"react-json-view-lite"' in package
    assert ".otlp-json {" in stylesheet
    assert "background: var(--surface)" in _block(stylesheet, ".otlp-json {")
    assert ".trace-detail-expand" not in responsive or "display: none" not in _block(
        responsive, ".trace-detail-expand"
    )
    expanded = _block(
        responsive,
        ".app-shell .trace-workbench.detail-expanded .trace-detail-panel {",
    )
    assert "grid-column: 1 / -1" in expanded

    kv_row = _block(stylesheet, ".trace-kv-row {")
    kv_key = _block(stylesheet, ".trace-kv-key {")
    assert "minmax(130px" in kv_row
    assert "overflow-wrap: anywhere" in kv_key
    collapsed = _block(stylesheet, ".trace-detail-panel.is-collapsed {")
    assert "display: none" in collapsed
    assert "trace-detail-reopen" in source
    assert "detail-collapsed" in source


def test_observability_opens_as_a_paginated_trace_list() -> None:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    source = OBSERVABILITY_SOURCE.read_text(encoding="utf-8")

    assert "limit: String(TRACE_PAGE_SIZE)" in source
    assert 'sort: "startedAt:desc"' in source
    assert "TRACE_PAGE_SIZE = 50" in source
    assert 'className="trace-list-page"' in source
    assert "<StudioDataTable" in source
    assert "cursorStack" in source
    assert "nextCursor" in source
    assert "返回 Trace 列表" in source
    assert "trace-list-table" not in source
    assert ".trace-list-table" not in stylesheet
    assert "position: sticky" in _block(stylesheet, ".studio-data-table th {")


def test_skill_details_have_a_read_only_file_tree_and_preview() -> None:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    source = (
        REPOSITORY_ROOT / "ksadk" / "studio" / "react-ui" / "src" / "pages" / "ResourcesPage.tsx"
    ).read_text(encoding="utf-8")

    browser = (
        REPOSITORY_ROOT
        / "ksadk"
        / "studio"
        / "react-ui"
        / "src"
        / "components"
        / "SkillFileBrowser.tsx"
    ).read_text(encoding="utf-8")
    markdown = (
        REPOSITORY_ROOT
        / "ksadk"
        / "studio"
        / "react-ui"
        / "src"
        / "components"
        / "ui"
        / "MarkdownPreview.tsx"
    ).read_text(encoding="utf-8")

    assert "SkillFileBrowser" in source
    assert "SkillFileTree" in browser
    assert "CodeViewer" in browser
    assert "MarkdownPreview" in browser
    assert "ReactMarkdown" in markdown
    assert "只读预览，不会执行脚本" in browser
    assert ".skill-preview-layout {" in stylesheet
    assert ".markdown-preview {" in stylesheet
    assert ".code-viewer-line {" in stylesheet
    assert ".skill-markdown-preview {" not in stylesheet
    assert ".skill-code-line {" not in stylesheet


def test_compact_navigation_has_a_persisted_explicit_toggle() -> None:
    app = APP_SOURCE.read_text(encoding="utf-8")
    navigation = (
        REPOSITORY_ROOT
        / "ksadk"
        / "studio"
        / "react-ui"
        / "src"
        / "components"
        / "NavigationRail.tsx"
    ).read_text(encoding="utf-8")
    responsive = RESPONSIVE_STYLESHEET.read_text(encoding="utf-8")

    assert 'data-rail={railExpanded ? "expanded" : "compact"}' in app
    assert '"展开导航"' in app
    assert '"收起导航"' in app
    assert "NAVIGATION_RAIL_PREFERENCE_KEY" in navigation
    assert "readNavigationRailPreference" in app
    assert "writeNavigationRailPreference" in app
    assert '.app-shell[data-rail="compact"] {' in responsive
    assert '.app-shell[data-rail="expanded"] {' in responsive
