"""Responsive acceptance for the production React Studio shell."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen

from playwright.sync_api import Page, expect, sync_playwright
from studio_e2e_support import studio_server

from ksadk.studio.service import StudioService

VIEWPORTS = (
    (768, 768),
    (1024, 768),
    (1280, 800),
    (1440, 900),
    (1458, 861),
    (1512, 982),
    (1920, 1080),
    (2560, 1440),
    (3840, 2160),
)


def assert_no_root_overflow(page: Page) -> None:
    metrics = page.evaluate(
        """() => ({
          viewport: window.innerWidth,
          scrollWidth: document.documentElement.scrollWidth,
          overflowing: [...document.querySelectorAll('*')]
            .map(element => {
              const rect = element.getBoundingClientRect();
              return {
                tag: element.tagName,
                className: typeof element.className === 'string' ? element.className : '',
                left: Math.round(rect.left),
                right: Math.round(rect.right),
                width: Math.round(rect.width),
                scrollWidth: element.scrollWidth,
              };
            })
            .filter(item => item.right > innerWidth + 1 || item.scrollWidth > item.width + 1)
            .slice(0, 12),
        })"""
    )
    assert metrics["scrollWidth"] <= metrics["viewport"] + 1, metrics


def rect(page: Page, selector: str) -> dict[str, float]:
    return page.locator(selector).evaluate(
        """element => {
          const value = element.getBoundingClientRect();
          return {
            left: value.left,
            right: value.right,
            top: value.top,
            bottom: value.bottom,
            width: value.width,
            height: value.height,
          };
        }"""
    )


def create_test_agent(base_url: str) -> None:
    payload = {
        "id": "responsive-agent",
        "name": "Responsive Agent",
        "description": "Responsive browser fixture",
        "template": "blank",
        "spec": {
            "description": "Responsive browser fixture",
            "runtime": {"type": "codex"},
            "instructions": {
                "system": "You are a responsive browser fixture.",
                "task": "Answer the request.",
            },
            "model": {
                "provider": "openai-compatible",
                "model": "fixture-model",
                "endpointUrl": "https://model.example.com/v1/chat/completions",
                "credentialRef": "env://MODEL_API_KEY",
                "parameters": {"temperature": 0.2, "maxTokens": 128},
            },
            "capabilities": {"skills": [], "mcpServers": [], "tools": []},
            "execution": {
                "strategy": "direct",
                "maxSteps": 4,
                "timeoutSeconds": 30,
                "retry": {"maxAttempts": 1, "backoffSeconds": 0},
            },
            "context": {
                "maxInputTokens": 4096,
                "reserveOutputTokens": 512,
                "compaction": {"enabled": True, "thresholdRatio": 0.8},
            },
            "security": {
                "toolPolicy": "deny-by-default",
                "allowedPermissions": [],
                "network": {
                    "mode": "restricted",
                    "allowedHosts": ["model.example.com"],
                    "allowPrivateNetwork": False,
                },
            },
        },
    }
    request = Request(
        f"{base_url}/api/v1/agents",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request) as response:
        assert response.status == 201, response.read().decode("utf-8")


def route_trace_fixture(route) -> None:
    trace_id = "0123456789abcdef0123456789abcdef"
    summary = {
        "traceId": trace_id,
        "runId": "run-responsive",
        "agentId": "responsive-agent",
        "sessionId": "session-responsive",
        "runtimeType": "codex",
        "model": "fixture-model",
        "status": "COMPLETED",
        "startedAt": "2026-08-10T08:00:00Z",
        "durationMs": 240,
        "inputTokens": 8,
        "outputTokens": 4,
        "totalTokens": 12,
        "usageReported": True,
        "spanCount": 1,
    }
    if route.request.url.split("?", 1)[0].endswith("/traces/overview"):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "range": "24h",
                    "total": 1,
                    "completed": 1,
                    "successRate": 1,
                    "averageDurationMs": 240,
                    "inputTokens": 8,
                    "outputTokens": 4,
                    "totalTokens": 12,
                    "buckets": [
                        {
                            "startedAt": "2026-08-10T08:00:00Z",
                            "runs": 1,
                            "completed": 1,
                        }
                    ],
                }
            ),
        )
        return
    if route.request.url.split("?", 1)[0].endswith(f"/traces/{trace_id}/otlp"):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "resourceSpans": [
                        {
                            "resource": {
                                "attributes": [
                                    {
                                        "key": "service.name",
                                        "value": {"stringValue": "responsive-agent"},
                                    }
                                ]
                            },
                            "scopeSpans": [],
                        }
                    ]
                }
            ),
        )
        return
    if route.request.url.split("?", 1)[0].endswith(f"/traces/{trace_id}"):
        detail = {
            **summary,
            "rootSpanId": "span-root",
            "metrics": {
                "durationMs": 240,
                "durationSource": "runtime",
                "inputTokens": 8,
                "outputTokens": 4,
                "totalTokens": 12,
                "usageReported": True,
                "usageSource": "fixture",
            },
            "target": {"name": "本地工作区"},
            "resource": {},
            "scope": {"name": "responsive-test", "version": "1"},
            "spans": [
                {
                    "spanId": "span-root",
                    "parentSpanId": "",
                    "name": "responsive run",
                    "kind": "INTERNAL",
                    "status": "OK",
                    "startTimeUnixNano": "1000000000",
                    "endTimeUnixNano": "1240000000",
                    "durationMs": 240,
                    "attributes": {},
                    "events": [
                        {
                            "name": "run.created",
                            "timeUnixNano": "1000000000",
                            "attributes": {
                                "agentkit.event.manifestId": "manifest-responsive-0123456789abcdef"
                            },
                        }
                    ],
                }
            ],
        }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(detail),
        )
        return
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({"items": [summary], "nextCursor": None, "total": 1}),
    )


def route_recoverable_chat_fixture(route) -> None:
    url = route.request.url.split("?", 1)[0]
    if url.endswith("/api/v1/runs/run-live/events"):
        route.fulfill(
            status=200,
            content_type="text/event-stream",
            body=(
                'id: 1\nevent: thinking.delta\ndata: {"text":"读取当前上下文"}\n\n'
                'id: 2\nevent: command.started\ndata: {"callId":"cmd-live","command":"rg TODO"}\n\n'
                'id: 3\nevent: message.delta\ndata: {"text":"正在继续生成可恢复的回答"}\n\n'
            ),
        )
        return
    if url.endswith("/api/v1/runs/run-history/events"):
        route.fulfill(status=200, content_type="text/event-stream", body="")
        return
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(
            {
                "items": [
                    {
                        "id": "run-live",
                        "agentId": "responsive-agent",
                        "sessionId": "session-live",
                        "status": "RUNNING",
                        "input": "继续处理这个长任务",
                        "output": "",
                        "model": "fixture-model",
                        "startedAt": "2026-08-10T09:00:00Z",
                    },
                    {
                        "id": "run-history",
                        "agentId": "responsive-agent",
                        "sessionId": "session-history",
                        "status": "COMPLETED",
                        "input": "展示历史答案",
                        "output": "这是已经完成的历史答案。",
                        "model": "fixture-model",
                        "startedAt": "2026-08-10T08:00:00Z",
                        "completedAt": "2026-08-10T08:00:01Z",
                    },
                ]
            }
        ),
    )


def assert_page_matrix(page: Page, width: int) -> None:
    navigation = page.locator(".primary-nav")
    pages = (
        ("Agent", "Agent", "data", None),
        ("构建", "构建", "document", None),
        ("部署", "部署", "document", None),
        ("工程资源", "工程资源", "data", "模型"),
        ("工程资源", "工程资源", "data", "Tool"),
        ("工程资源", "工程资源", "data", "MCP"),
        ("工程资源", "工程资源", "data", "Skill"),
        ("可观测", "可观测", "workbench", None),
        ("运行资源", "运行资源", "document", None),
        ("自动化", "自动化", "document", None),
    )
    for nav_label, page_title, layout, tab_label in pages:
        navigation.get_by_role("button", name=nav_label, exact=True).click()
        if tab_label is not None:
            page.get_by_role("tab").filter(has_text=tab_label).click()
        expect(
            page.get_by_role("banner", name="当前页面").get_by_text(
                page_title, exact=True
            )
        ).to_be_visible()
        page_root = page.locator("#mainContent > div:not(.chat-wrap) > [data-layout]").first
        expect(page_root).to_have_attribute("data-layout", layout)
        try:
            assert_no_root_overflow(page)
        except AssertionError as error:
            overflowing = page.evaluate(
                """() => [...document.querySelectorAll('*')]
                  .map(element => ({
                    tag: element.tagName,
                    className: element.className,
                    right: Math.round(element.getBoundingClientRect().right),
                  }))
                  .filter(item => item.right > innerWidth + 1)
                  .slice(0, 8)"""
            )
            raise AssertionError((nav_label, str(error), overflowing)) from error
        page_rect = page_root.evaluate(
            """element => {
              const value = element.getBoundingClientRect();
              return { left: value.left, right: value.right, width: value.width };
            }"""
        )
        assert page_rect["left"] >= 0, (nav_label, page_rect)
        assert page_rect["right"] <= width + 1, (nav_label, page_rect)
        if width == 3840:
            expected_max = 1760
            assert page_rect["width"] <= expected_max + 1, (nav_label, page_rect)

def main() -> None:
    with TemporaryDirectory(prefix="ksadk-responsive-studio-") as temp_dir:
        workspace = Path(temp_dir)
        service = StudioService(
            workspace,
            codex_runtime_inspector=lambda _runtime: (
                "0.8.2",
                "0.144.4",
                "codex-cli 0.144.4",
            ),
        )
        with (
            studio_server(workspace, service=service) as base_url,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": 768, "height": 768},
                    device_scale_factor=2,
                    reduced_motion="reduce",
                )
                page = context.new_page()
                page.goto(base_url, wait_until="networkidle")

                assert_no_root_overflow(page)
                expect(page.locator("html")).to_have_attribute("data-theme", "light")
                page.get_by_role("button", name="设置", exact=True).click()
                settings_dialog = page.get_by_role("dialog", name="设置")
                expect(settings_dialog).to_be_visible()
                settings_dialog.locator('input[name="studio-theme"][value="dark"]').check()
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                assert page.locator("html").evaluate(
                    "element => element.classList.contains('dark')"
                )
                assert (
                    page.evaluate("getComputedStyle(document.documentElement).colorScheme")
                    == "dark"
                )
                page.keyboard.press("Escape")
                page.reload(wait_until="networkidle")
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")

                page.get_by_role("button", name="设置", exact=True).click()
                settings_dialog = page.get_by_role("dialog", name="设置")
                settings_dialog.locator('input[name="studio-theme"][value="system"]').check()
                page.emulate_media(color_scheme="dark")
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                page.emulate_media(color_scheme="light")
                expect(page.locator("html")).to_have_attribute("data-theme", "light")
                settings_dialog.locator('input[name="studio-theme"][value="light"]').check()
                page.emulate_media(color_scheme="dark")
                expect(page.locator("html")).to_have_attribute("data-theme", "light")
                settings_dialog.locator('input[name="studio-theme"][value="system"]').check()
                page.emulate_media(color_scheme="light")
                page.keyboard.press("Escape")

                main_rect = rect(page, ".app-main")
                assert main_rect["left"] >= 0, main_rect
                assert main_rect["right"] <= 769, main_rect

                page.get_by_role("button", name="创建 Agent", exact=True).first.click()
                compact_trigger = page.get_by_role(
                    "button", name="查看创建入口与配置步骤", exact=True
                )
                expect(compact_trigger).to_be_visible()
                expect(page.locator(".app-shell")).to_have_attribute("data-viewport", "compact")
                compact_create_drawer = page.get_by_role("dialog", name="创建方式", exact=True)
                expect(compact_create_drawer).to_be_hidden()
                expect(page.locator(".create-rail")).to_have_count(0)

                compact_trigger.click()
                expect(compact_create_drawer).to_be_visible()
                expect(compact_create_drawer.locator(".create-rail-panel")).to_be_visible()
                expect(page.locator("#mainContent")).to_have_attribute("inert", "")

                compact_create_drawer.locator(".authoring-mode-tabs button").filter(
                    has_text="对话构建"
                ).click()
                expect(compact_create_drawer).to_be_hidden()
                expect(compact_trigger).to_be_focused()
                expect(page.get_by_role("heading", name="对话创建 Agent")).to_be_visible()
                conversation_input = page.get_by_placeholder("描述你想创建或调整的 Agent…")
                conversation_input.fill("保留这段构建说明")

                page.set_viewport_size({"width": 1024, "height": 768})
                expect(page.locator(".app-shell")).to_have_attribute("data-viewport", "laptop")
                expect(compact_trigger).to_be_hidden()
                expect(page.locator(".create-rail")).to_be_visible()
                assert not page.locator(".create-rail").evaluate(
                    "element => element.hasAttribute('inert')"
                )
                expect(page.locator(".create-rail")).to_have_css("width", "212px")
                laptop_rail = rect(page, ".create-rail")
                assert abs(laptop_rail["width"] - 212) <= 1, laptop_rail
                expect(conversation_input).to_have_value("保留这段构建说明")
                assert_no_root_overflow(page)

                page.set_viewport_size({"width": 768, "height": 768})
                expect(page.locator(".app-shell")).to_have_attribute("data-viewport", "compact")
                expect(page.locator(".create-rail")).to_have_count(0)
                expect(compact_create_drawer).to_be_hidden()
                expect(conversation_input).to_have_value("保留这段构建说明")

                compact_trigger.click()
                page.keyboard.press("Escape")
                expect(compact_create_drawer).to_be_hidden()
                expect(compact_trigger).to_be_focused()
                compact_trigger.click()
                compact_create_drawer.get_by_role("button", name="关闭", exact=True).click()
                expect(compact_create_drawer).to_be_hidden()
                expect(compact_trigger).to_be_focused()

                for viewport in (
                    {"width": 1458, "height": 861},
                    {"width": 1512, "height": 982},
                ):
                    page.set_viewport_size(viewport)
                    expect(page.locator(".create-shell")).to_have_attribute(
                        "data-layout", "workbench"
                    )
                    height_metrics = page.evaluate(
                        """() => ({
                          viewportHeight: innerHeight,
                          rootScrollHeight: document.documentElement.scrollHeight,
                          chatOverflow: getComputedStyle(
                            document.querySelector('.conversation-chat')
                          ).overflowY,
                          transcriptOverflow: getComputedStyle(
                            document.querySelector('.conversation-transcript')
                          ).overflowY,
                          inspectOverflow: getComputedStyle(
                            document.querySelector('.conversation-draft-rail')
                          ).overflowY,
                        })"""
                    )
                    assert (
                        height_metrics["rootScrollHeight"] <= height_metrics["viewportHeight"] + 1
                    ), height_metrics
                    assert height_metrics["chatOverflow"] == "visible", height_metrics
                    assert height_metrics["transcriptOverflow"] == "auto", height_metrics
                    assert height_metrics["inspectOverflow"] == "hidden", height_metrics

                page.set_viewport_size({"width": 1024, "height": 682})
                page.locator(".authoring-mode-tabs button").filter(has_text="快速创建").click()
                expect(page.locator(".create-shell")).to_have_attribute(
                    "data-layout", "document"
                )
                page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                continue_button = page.get_by_role("button", name="继续", exact=True)
                expect(continue_button).to_be_visible()
                continue_rect = continue_button.evaluate(
                    "element => element.getBoundingClientRect().toJSON()"
                )
                # The document flow keeps the step action visible after
                # scrolling to the end of the current quick-create step.
                assert continue_rect["top"] >= 0, continue_rect
                assert continue_rect["bottom"] <= page.viewport_size["height"], continue_rect
                assert_no_root_overflow(page)

                page.set_viewport_size({"width": 768, "height": 768})
                resource_trigger = page.locator(".primary-nav").get_by_role(
                    "button", name="工程资源", exact=True
                )
                resource_trigger.click()
                page.get_by_role("tab").filter(has_text="Skill").click()
                expect(page.get_by_role("tab").filter(has_text="Skill")).to_have_attribute(
                    "aria-selected", "true"
                )
                discovery_trigger = page.get_by_role("button", name="发现 Skill", exact=True)
                discovery_trigger.click()
                discovery_dialog = page.get_by_role("dialog", name="发现本地 Skill")
                expect(discovery_dialog).to_be_visible()
                expect(page.locator(".global-header")).to_have_attribute("inert", "")
                expect(page.locator(".sidebar")).to_have_attribute("inert", "")
                expect(page.locator(".skip-link")).to_have_attribute("inert", "")
                for _ in range(20):
                    page.keyboard.press("Tab")
                    assert discovery_dialog.evaluate(
                        "dialog => dialog.contains(document.activeElement)"
                    )
                drawer_rect = discovery_dialog.evaluate(
                    """element => {
                      const value = element.getBoundingClientRect();
                      return { left: value.left, right: value.right, width: value.width };
                    }"""
                )
                assert drawer_rect["left"] >= 16, drawer_rect
                assert drawer_rect["right"] <= 752, drawer_rect
                page.keyboard.press("Escape")
                expect(discovery_dialog).to_be_hidden()
                expect(discovery_trigger).to_be_focused()
                assert not page.locator(".global-header").evaluate(
                    "element => element.hasAttribute('inert')"
                )
                context.close()

                for width, height in VIEWPORTS:
                    matrix_context = browser.new_context(
                        viewport={"width": width, "height": height},
                        reduced_motion="reduce",
                    )
                    matrix_page = matrix_context.new_page()
                    matrix_page.goto(base_url, wait_until="networkidle")
                    expected_rail = 80 if width <= 1023 else 216
                    sidebar_rect = rect(matrix_page, ".sidebar")
                    assert abs(sidebar_rect["width"] - expected_rail) <= 1, (
                        width,
                        sidebar_rect,
                    )
                    assert_page_matrix(matrix_page, width)

                    if width == 3840:
                        matrix_page.locator(".primary-nav").get_by_role(
                            "button", name="Agent", exact=True
                        ).click()
                        matrix_page.get_by_role(
                            "button", name="创建 Agent", exact=True
                        ).first.click()
                        document_rect = rect(matrix_page, ".wizard-content")
                        assert document_rect["width"] <= 1201, document_rect
                        matrix_page.locator(".authoring-mode-tabs button").filter(
                            has_text="对话构建"
                        ).click()
                        workbench_rect = rect(matrix_page, ".authoring-mode-panel")
                        assert workbench_rect["width"] <= 1361, workbench_rect
                    matrix_context.close()

                create_test_agent(base_url)
                workbench_context = browser.new_context(
                    viewport={"width": 1024, "height": 768},
                    reduced_motion="reduce",
                    color_scheme="dark",
                )
                workbench_context.add_init_script(
                    "localStorage.setItem('agentkit-studio-theme', 'system')"
                )
                workbench_page = workbench_context.new_page()
                workbench_page.route("**/api/v1/runs**", route_recoverable_chat_fixture)
                workbench_page.goto(base_url, wait_until="networkidle")
                expect(workbench_page.locator("html")).to_have_attribute("data-theme", "dark")
                workbench_page.locator(".primary-nav").get_by_role(
                    "button", name="会话", exact=True
                ).click()
                expect(workbench_page.locator(".app-shell")).to_have_attribute(
                    "data-view", "conversations"
                )
                expect(workbench_page.locator(".studio-chat-shell")).to_be_visible()
                expect(workbench_page.locator(".chat-conversation")).to_be_visible()
                expect(workbench_page.locator(".chat-composer")).to_be_visible()
                expect(
                    workbench_page.get_by_text("正在继续生成可恢复的回答", exact=True)
                ).to_be_visible()
                first_session_row = workbench_page.locator(".chat-session-item").first.evaluate(
                    "element => element.getBoundingClientRect().toJSON()"
                )
                assert first_session_row["height"] <= 41, first_session_row
                assert workbench_page.locator(".chat-session-item time").count() == 0
                model_trigger = workbench_page.locator(".chat-model-trigger")
                expect(model_trigger).to_be_visible()
                model_trigger_text = model_trigger.inner_text().strip()
                assert model_trigger_text and model_trigger_text != "模型"
                expect(
                    workbench_page.get_by_role("button", name="批准模式：帮我批准")
                ).to_be_visible()
                workbench_page.get_by_role("button", name="批准模式：帮我批准").click()
                approval_menu = workbench_page.locator(".chat-approval-menu")
                expect(approval_menu).to_be_visible()
                assert approval_menu.locator(".chat-approval-option").count() == 3
                approval_menu.get_by_text("请求批准", exact=True).click()
                expect(
                    workbench_page.get_by_role("button", name="批准模式：请求批准")
                ).to_be_visible()
                workbench_page.locator(".chat-message-list").evaluate(
                    """element => {
                      const spacer = document.createElement('div');
                      spacer.dataset.testLongConversation = 'true';
                      spacer.style.height = '1800px';
                      element.append(spacer);
                    }"""
                )
                dark_chat_colors = workbench_page.evaluate(
                    """() => {
                      const header = document.querySelector('.chat-conversation-header');
                      const composer = document.querySelector('.chat-composer');
                      const sidebar = document.querySelector('.chat-session-sidebar');
                      const text = header.querySelector('h1');

                      const context = document.createElement('canvas').getContext('2d');
                      const rgb = value => {
                        context.clearRect(0, 0, 1, 1);
                        context.fillStyle = value;
                        context.fillRect(0, 0, 1, 1);
                        return Array.from(context.getImageData(0, 0, 1, 1).data.slice(0, 3));
                      };
                      const result = {
                        header: rgb(getComputedStyle(header).backgroundColor),
                        form: rgb(getComputedStyle(composer).backgroundColor),
                        messageBackground: rgb(getComputedStyle(sidebar).backgroundColor),
                        messageText: rgb(getComputedStyle(text).color),
                      };
                      return result;
                    }"""
                )
                for surface in ("header", "form", "messageBackground"):
                    assert max(dark_chat_colors[surface]) < 100, dark_chat_colors
                assert min(dark_chat_colors["messageText"]) > 175, dark_chat_colors
                chat_rect = rect(workbench_page, ".chat-wrap")
                assert chat_rect["top"] >= 64, chat_rect
                assert chat_rect["bottom"] <= 769, chat_rect
                composer_rect = rect(workbench_page, ".chat-composer-wrap")
                assert composer_rect["top"] >= 64, composer_rect
                assert composer_rect["bottom"] <= chat_rect["bottom"] + 1, (
                    composer_rect,
                    chat_rect,
                )
                message_scroll = workbench_page.locator(".chat-message-list").evaluate(
                    """element => ({
                      overflowY: getComputedStyle(element).overflowY,
                      clientHeight: element.clientHeight,
                      scrollHeight: element.scrollHeight,
                    })"""
                )
                assert message_scroll["overflowY"] == "auto", message_scroll
                assert message_scroll["scrollHeight"] > message_scroll["clientHeight"], (
                    message_scroll
                )
                assert_no_root_overflow(workbench_page)

                workbench_page.locator(".chat-session-main").filter(has_text="展示历史答案").click()
                expect(
                    workbench_page.get_by_text("这是已经完成的历史答案。", exact=True)
                ).to_be_visible()
                workbench_page.locator(".chat-session-main").filter(
                    has_text="继续处理这个长任务"
                ).click()
                expect(
                    workbench_page.get_by_text("正在继续生成可恢复的回答", exact=True)
                ).to_be_visible()
                workbench_page.reload(wait_until="domcontentloaded")
                expect(workbench_page.locator(".app-shell")).to_have_attribute(
                    "data-view", "conversations"
                )
                expect(
                    workbench_page.get_by_text("正在继续生成可恢复的回答", exact=True)
                ).to_be_visible()
                expect(workbench_page.locator(".chat-composer")).to_be_visible()
                context_ring = workbench_page.locator(".chat-context-ring")
                context_tooltip = workbench_page.locator(".chat-context-tooltip")
                expect(context_ring).to_be_visible()
                expect(context_tooltip).to_be_hidden()
                context_ring.hover()
                expect(context_tooltip).to_be_visible()
                expect(context_tooltip).to_contain_text("上下文窗口")
                workbench_page.mouse.move(0, 0)
                context_ring.focus()
                expect(context_tooltip).to_be_visible()

                workbench_page.get_by_role("button", name="运行详情", exact=True).click()
                expect(workbench_page.locator(".chat-run-panel")).to_be_visible()
                run_panel_rect = rect(workbench_page, ".chat-run-panel")
                assert run_panel_rect["top"] >= 64, run_panel_rect
                assert run_panel_rect["right"] <= 1025, run_panel_rect
                assert run_panel_rect["bottom"] <= 769, run_panel_rect
                assert run_panel_rect["width"] <= 421, run_panel_rect

                workbench_page.set_viewport_size({"width": 1512, "height": 982})
                expect(workbench_page.locator(".app-shell")).to_have_attribute(
                    "data-viewport", "desktop"
                )
                chat_sidebar = rect(workbench_page, ".sidebar")
                assert abs(chat_sidebar["width"] - 216) <= 1, chat_sidebar
                assert_no_root_overflow(workbench_page)
                workbench_context.close()

                trace_context = browser.new_context(
                    viewport={"width": 1458, "height": 861},
                    reduced_motion="reduce",
                )
                trace_page = trace_context.new_page()
                trace_page.route("**/api/v1/traces**", route_trace_fixture)
                trace_page.goto(base_url, wait_until="networkidle")
                trace_page.locator(".primary-nav").get_by_role(
                    "button", name="可观测", exact=True
                ).click()
                trace_root = trace_page.locator(".observability-page")
                expect(trace_root).to_have_attribute("data-layout", "workbench")
                assert trace_page.url.endswith("#/observability"), trace_page.url
                expect(trace_page.locator(".observability-overview")).to_be_visible()
                expect(trace_page.locator(".overview-chart")).to_be_visible()
                expect(trace_page.locator(".overview-metric-card")).to_have_count(4)
                expect(trace_page.locator(".trace-list-page")).to_be_visible()
                expect(
                    trace_page.locator(".trace-list-page .studio-data-table tbody tr")
                ).to_have_count(1)
                trace_page.get_by_role("button", name="查看详情", exact=True).click()
                expect(trace_root).to_have_attribute("data-layout", "workbench")
                expect(trace_page.locator(".trace-span-row")).to_have_count(1)
                trace_sidebar = rect(trace_page, ".sidebar")
                assert abs(trace_sidebar["width"] - 216) <= 1, trace_sidebar
                observability_body = trace_page.locator(".observability-body")
                body_scroll = observability_body.evaluate(
                    """element => ({
                      overflowY: getComputedStyle(element).overflowY,
                      clientHeight: element.clientHeight,
                      scrollHeight: element.scrollHeight,
                    })"""
                )
                # Desktop trace workbench keeps scrolling inside its panes so
                # the overview and panel headers remain stable.
                assert body_scroll["overflowY"] == "hidden", body_scroll
                trace_page.locator(".trace-workbench").scroll_into_view_if_needed()
                trace_rect = rect(trace_page, ".trace-workbench")
                assert trace_rect["top"] >= 64, trace_rect
                assert trace_rect["top"] < 862, trace_rect
                assert_no_root_overflow(trace_page)
                trace_overflows = trace_page.evaluate(
                    """() => ({
                      spans: getComputedStyle(document.querySelector('.trace-span-tree')).overflowY,
                      detail: getComputedStyle(
                        document.querySelector('.trace-detail-body')
                      ).overflowY,
                    })"""
                )
                assert trace_overflows == {
                    "spans": "auto",
                    "detail": "auto",
                }, trace_overflows
                trace_page.get_by_role("tab", name="Events", exact=True).click()
                event_row = trace_page.locator(".trace-event-card .trace-kv-row").first
                expect(event_row).to_be_visible()
                event_columns = event_row.evaluate(
                    """element => {
                      const key = element.querySelector('.trace-kv-key').getBoundingClientRect();
                      const value = element.querySelector(
                        '.trace-kv-value'
                      ).getBoundingClientRect();
                      return { keyRight: key.right, valueLeft: value.left };
                    }"""
                )
                assert event_columns["keyRight"] <= event_columns["valueLeft"], event_columns

                trace_page.get_by_role("tab", name="Raw OTLP", exact=True).click()
                json_tree = trace_page.locator(".otlp-json")
                expect(json_tree).to_be_visible()
                expect(json_tree).to_contain_text("resourceSpans")
                expect(json_tree).to_contain_text("service.name")
                assert json_tree.locator(".otlp-json-collapse").count() >= 3
                json_colors = json_tree.evaluate(
                    """element => ({
                      background: getComputedStyle(element).backgroundColor,
                      key: getComputedStyle(element.querySelector('.otlp-json-key')).color,
                      text: getComputedStyle(element).color,
                    })"""
                )
                assert json_colors["background"] != "rgb(32, 38, 49)", json_colors
                assert json_colors["key"] != json_colors["text"], json_colors
                trace_page.get_by_role("button", name="全部收起", exact=True).click()
                expect(json_tree.locator(".otlp-json-expand").first).to_be_visible()
                trace_page.get_by_role("button", name="全部展开", exact=True).click()
                expect(json_tree.locator(".otlp-json-collapse").first).to_be_visible()
                expect(json_tree).to_contain_text("service.name")

                trace_page.set_viewport_size({"width": 1194, "height": 820})
                collapse_detail = trace_page.get_by_role("button", name="收起详情", exact=True)
                expect(collapse_detail).to_be_visible()
                collapse_detail.click()
                expect(trace_page.locator(".trace-detail-panel")).to_be_hidden()
                reopen_detail = trace_page.locator(".trace-span-header").get_by_role(
                    "button", name="展开右侧详情", exact=True
                )
                expect(reopen_detail).to_be_visible()
                expect(trace_page.get_by_role("tab", name="Raw OTLP", exact=True)).to_be_hidden()
                reopen_detail.click()

                expand_button = trace_page.get_by_role("button", name="放大详情", exact=True)
                expect(expand_button).to_be_visible()
                expand_button.click()
                collapse_button = trace_page.get_by_role("button", name="退出放大", exact=True)
                expect(collapse_button).to_be_visible()
                expect(trace_page.locator(".trace-detail-panel")).to_be_visible()
                expect(trace_page.locator(".trace-span-panel")).to_be_hidden()
                collapse_button.click()
                expect(
                    trace_page.get_by_role("button", name="放大详情", exact=True)
                ).to_be_visible()
                trace_page.get_by_role("button", name="返回 Trace 列表", exact=True).click()
                expect(trace_root).to_have_attribute("data-layout", "workbench")
                expect(trace_page.locator(".trace-list-page")).to_be_visible()
                expect(trace_page.locator(".app-shell")).to_have_attribute("data-rail", "expanded")
                expect(trace_page.locator(".sidebar")).to_have_css("width", "216px")
                expanded_sidebar = rect(trace_page, ".sidebar")
                assert abs(expanded_sidebar["width"] - 216) <= 1, expanded_sidebar
                trace_page.reload(wait_until="networkidle")
                expect(trace_page.locator(".app-shell")).to_have_attribute("data-rail", "expanded")
                trace_page.get_by_role("button", name="收起导航", exact=True).click()
                expect(trace_page.locator(".app-shell")).to_have_attribute("data-rail", "compact")
                trace_context.close()
            finally:
                browser.close()


if __name__ == "__main__":
    main()
