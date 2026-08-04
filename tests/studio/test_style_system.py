from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STYLESHEET = REPOSITORY_ROOT / "ksadk" / "studio" / "static" / "app.css"
INDEX_HTML = REPOSITORY_ROOT / "ksadk" / "studio" / "static" / "index.html"
APP_SCRIPT = REPOSITORY_ROOT / "ksadk" / "studio" / "static" / "app.js"
SHARED_CHAT_STYLESHEET = (
    REPOSITORY_ROOT / "ksadk" / "studio" / "static" / "shared-chat.css"
)


def _stylesheet_without_root() -> tuple[str, str]:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    match = re.search(r":root\s*\{(?P<tokens>.*?)\n\}", stylesheet, re.DOTALL)
    assert match is not None
    return stylesheet, stylesheet[: match.start()] + stylesheet[match.end() :]


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


def test_component_css_cannot_introduce_private_typography_values() -> None:
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
    assert font_shorthands == ["inherit"], "font shorthand is only allowed for form inheritance"


def test_core_components_use_shared_visual_contracts() -> None:
    stylesheet, _ = _stylesheet_without_root()

    expected_contracts = {
        ".page-header h1": "--font-size-page-title",
        ".button,\n.icon-button": "--button-height",
        ".field label": "--font-size-control",
        ".status-badge": "--status-height",
        "input,\nselect {": "--control-height",
    }

    for selector, token in expected_contracts.items():
        start = stylesheet.find(selector)
        assert start >= 0, f"missing core selector {selector}"
        block_end = stylesheet.find("}", start)
        assert token in stylesheet[start:block_end], f"{selector} must use {token}"


def test_static_markup_has_no_inline_styles() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    assert re.search(r"\sstyle\s*=", markup, re.IGNORECASE) is None


def test_shared_resource_navigation_has_one_semantic_active_item() -> None:
    script = APP_SCRIPT.read_text(encoding="utf-8")

    assert 'node.dataset.resourceKind === state.resourceKind' in script
    assert 'node.setAttribute("aria-current", "page")' in script
    assert 'node.removeAttribute("aria-current")' in script


def test_model_credentials_are_never_written_to_browser_storage() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    script = APP_SCRIPT.read_text(encoding="utf-8")

    assert 'id="modelCredentialValue"' in markup
    assert 'type="password"' in markup
    assert 'autocomplete="new-password"' in markup
    assert "localStorage" not in script
    assert "sessionStorage" not in script


def test_create_agent_distinguishes_workspace_slug_from_cloud_agent_id() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert "本地标识" in markup
    assert "不是云端 AgentId" in markup
    assert "云端 AgentId 由部署服务分配" in markup


def test_invocation_drawer_uses_openai_responses_instead_of_control_plane_runs() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    script = APP_SCRIPT.read_text(encoding="utf-8")

    assert "POST /v1/responses" in markup
    assert 'const endpoint = "/v1/responses"' in script
    assert '"input_text"' in script
    assert "X-AgentKit-Session" not in script[script.index("function renderInvocation"):]
    assert "/api/v1/builds/${buildId}/runs" not in script


def test_chat_composer_starts_at_one_line_and_grows_until_its_scroll_limit() -> None:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    script = APP_SCRIPT.read_text(encoding="utf-8")

    start = stylesheet.index(".composer textarea {")
    block = stylesheet[start:stylesheet.index("}", start)]
    assert "min-height: 42px" in block
    assert "max-height: 160px" in block
    assert "overflow-y: auto" in block
    assert 'input.style.height = "42px"' in script
    assert "Math.min(Math.max(input.scrollHeight, 42), 160)" in script


def test_long_chat_output_scrolls_inside_the_message_pane() -> None:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")

    for selector, declarations in {
        ".chat-shell {": ["min-height: 0", "overflow: hidden"],
        ".conversation {": ["min-height: 0", "overflow: hidden"],
        ".message-list {": ["min-height: 0", "overflow-y: auto", "overflow-x: hidden"],
        ".message-content {": ["max-width: 100%", "overflow-wrap: anywhere"],
    }.items():
        start = stylesheet.index(selector)
        block = stylesheet[start:stylesheet.index("}", start)]
        for declaration in declarations:
            assert declaration in block


def test_shared_chat_composer_has_one_focus_boundary() -> None:
    stylesheet = SHARED_CHAT_STYLESHEET.read_text(encoding="utf-8")

    textarea_selector = (
        'html[data-agentkit-studio-chat="workbench"] '
        "main > div:last-child textarea {"
    )
    start = stylesheet.find(textarea_selector)
    assert start >= 0
    block_end = stylesheet.find("}", start)
    textarea_block = stylesheet[start:block_end]
    assert "border: 0 !important" in textarea_block
    assert "outline: 0 !important" in textarea_block
    assert "box-shadow: none !important" in textarea_block

    assert (
        'html[data-agentkit-studio-chat="workbench"] main > div:last-child '
        "textarea:focus-visible {"
    ) in stylesheet


def test_shared_chat_uses_asymmetric_message_alignment() -> None:
    stylesheet = SHARED_CHAT_STYLESHEET.read_text(encoding="utf-8")

    user_row_selector = (
        'html[data-agentkit-studio-chat="workbench"]\n'
        "  main\n"
        "  > div:nth-child(2)\n"
        '  [class~="justify-end"] {'
    )
    user_row_start = stylesheet.find(user_row_selector)
    assert user_row_start >= 0
    user_row_end = stylesheet.find("}", user_row_start)
    user_row_block = stylesheet[user_row_start:user_row_end]
    assert "justify-content: flex-end !important" in user_row_block

    user_bubble_selector = (
        '  [class~="justify-end"]\n'
        '  > [class*="max-w-[80%]"] {'
    )
    user_bubble_start = stylesheet.find(user_bubble_selector)
    assert user_bubble_start >= 0
    user_bubble_end = stylesheet.find("}", user_bubble_start)
    user_bubble_block = stylesheet[user_bubble_start:user_bubble_end]
    assert "width: fit-content !important" in user_bubble_block
    assert "background: #edf4fc !important" in user_bubble_block

    assistant_selector = (
        '  [class~="group"][class*="max-w-3xl"] {'
    )
    assistant_start = stylesheet.find(assistant_selector)
    assert assistant_start >= 0
    assistant_end = stylesheet.find("}", assistant_start)
    assistant_block = stylesheet[assistant_start:assistant_end]
    assert "padding: 0 !important" in assistant_block
    assert "border-left: 0 !important" in assistant_block

    assert 'button[aria-controls$="-thinking-detail"] {' in stylesheet
