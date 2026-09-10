"""Responsive acceptance for the production React Studio shell.

Run after building Studio: PYTHONPATH=. uv run python tests/studio/e2e/studio_responsive_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from urllib.request import Request, urlopen

from playwright.sync_api import Page, expect, sync_playwright
from studio_e2e_support import studio_server

from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCompleted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
    UsageReported,
)
from ksadk.events.content import TextContent
from ksadk.runtime import RunHandle, StartRequest
from ksadk.studio.service import StudioService
from tests.studio.runtime_adapter_fixtures import RuntimeFixture

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


def open_studio(page: Page, base_url: str) -> None:
    """Wait for the rendered Studio shell, not for long-lived API traffic.

    Studio intentionally starts session/catalog/trace requests while it mounts.
    ``networkidle`` turns that valid background work into a flaky browser gate;
    the visible application shell is the actual readiness condition here.
    """
    page.goto(base_url, wait_until="domcontentloaded")
    expect(page.locator(".app-shell")).to_be_visible()


def reload_studio(page: Page) -> None:
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(".app-shell")).to_be_visible()


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


class RecoverableConversationEvents:
    """Real canonical sessions with one completed and one deliberately live turn.

    Only the runtime event source is deterministic. Session creation, the RunAgent
    stream, persistence, active-run discovery and replay use production routes.
    """

    def __init__(self) -> None:
        self.release_live = Event()

    async def __call__(
        self, request: StartRequest, handle: RunHandle
    ) -> AsyncIterator[RuntimeEvent]:
        live = "长任务" in str(request.input)
        body = "正在继续生成可恢复的回答" if live else "这是已经完成的历史答案。"
        if live:
            body += "\n\n" + "\n\n".join(
                f"第 {index} 项：保留完整对话内容，并让输入区保持在可见位置。"
                for index in range(1, 45)
            )
        source = SourceRef(framework="codex")
        common = {
            "schema_version": 2,
            "timestamp": 1.0,
            "run_id": handle.run_id,
            "scope_id": f"scope-{handle.run_id}",
            "source": source,
        }
        yield RunStarted(event_id=f"{handle.run_id}:1", seq=1, status="running", **common)
        yield ItemStarted(
            event_id=f"{handle.run_id}:2",
            seq=2,
            item_id="answer",
            item_kind="message",
            phase="final_answer",
            initial=ContentSnapshot(parts=()),
            **common,
        )
        yield ItemUpdated(
            event_id=f"{handle.run_id}:3",
            seq=3,
            item_id="answer",
            item_kind="message",
            op="append",
            update=TextContent(part_id="text", text=body),
            **common,
        )
        yield UsageReported(
            event_id=f"{handle.run_id}:4",
            seq=4,
            input_tokens=80,
            output_tokens=120,
            total_tokens=200,
            **common,
        )
        while live and not self.release_live.is_set():
            await asyncio.sleep(0.05)
        yield ItemCompleted(
            event_id=f"{handle.run_id}:5",
            seq=5,
            item_id="answer",
            item_kind="message",
            snapshot=ContentSnapshot(parts=(TextContent(part_id="text", text=body),)),
            **common,
        )
        yield RunCompleted(
            event_id=f"{handle.run_id}:6",
            seq=6,
            status="completed",
            output_refs=(OutputRef(scope_id=common["scope_id"], item_id="answer", part_id="text"),),
            **common,
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
            page.get_by_role("tab", name=tab_label, exact=True).click()
        expect(
            page.get_by_role("banner", name="当前页面").get_by_text(page_title, exact=True)
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
    verification_failures: list[str] = []
    with TemporaryDirectory(prefix="ksadk-responsive-studio-") as temp_dir:
        workspace = Path(temp_dir)
        conversation_events = RecoverableConversationEvents()
        conversation_runtime = RuntimeFixture(conversation_events)
        service = StudioService(
            workspace,
            runtime_executor=conversation_runtime.executor,
            codex_runtime_inspector=lambda _runtime: (
                "0.8.2",
                # This browser fixture creates a current Codex agent.  Keep
                # the simulated local runtime aligned with that agent's
                # pinned version so this test exercises the Studio UI rather
                # than deliberately tripping the runtime-version guard.
                "0.147.0",
                "codex-cli 0.147.0",
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
                open_studio(page, base_url)

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
                reload_studio(page)
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
                laptop_rail = rect(page, ".create-rail")
                laptop_stage = rect(page, ".create-stage")
                assert laptop_rail["height"] < 150, laptop_rail
                assert laptop_stage["top"] >= laptop_rail["bottom"] - 1, (laptop_rail, laptop_stage)
                assert laptop_stage["width"] <= laptop_rail["width"] + 1, (
                    laptop_rail,
                    laptop_stage,
                )
                assert (
                    abs(
                        (laptop_stage["left"] + laptop_stage["right"])
                        - (laptop_rail["left"] + laptop_rail["right"])
                    )
                    <= 2
                ), (laptop_rail, laptop_stage)
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
                expect(page.locator(".create-shell")).to_have_attribute("data-layout", "document")
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
                skill_tab = page.get_by_role(
                    "tab", name=re.compile(r"^Skill(?:\s+\d+)?$")
                )
                skill_tab.click()
                expect(skill_tab).to_have_attribute(
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
                    open_studio(matrix_page, base_url)
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

                print("Responsive shell and viewport matrix passed", flush=True)
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
                open_studio(workbench_page, base_url)
                expect(workbench_page.locator("html")).to_have_attribute("data-theme", "dark")
                workbench_page.locator(".primary-nav").get_by_role(
                    "button", name="会话", exact=True
                ).click()
                expect(workbench_page.locator(".app-shell")).to_have_attribute(
                    "data-view", "conversations"
                )
                expect(workbench_page.locator(".studio-chat-shell")).to_be_visible()
                expect(workbench_page.locator(".chat-conversation")).to_be_visible()
                composer = workbench_page.locator('[data-slot="composer"]')
                message_list = workbench_page.locator('[data-slot="message-list"]')
                message_input = composer.locator("textarea")
                expect(composer).to_be_visible()
                expect(message_input).to_be_enabled()
                message_input.fill("展示历史答案")
                workbench_page.get_by_role("button", name="发送消息", exact=True).click()
                expect(
                    workbench_page.get_by_text("这是已经完成的历史答案。", exact=True)
                ).to_be_visible(timeout=15000)
                expect(
                    workbench_page.get_by_role("button", name="停止生成", exact=True)
                ).to_have_count(0)
                workbench_page.get_by_role("button", name="新对话", exact=True).click()
                message_input.fill("继续处理这个长任务")
                workbench_page.get_by_role("button", name="发送消息", exact=True).click()
                live_answer = workbench_page.get_by_text("正在继续生成可恢复的回答", exact=True)
                expect(live_answer).to_be_visible(timeout=15000)
                stop_button = workbench_page.get_by_role("button", name="停止生成", exact=True)
                expect(stop_button).to_be_visible()
                expect(workbench_page.locator(".chat-session-item")).to_have_count(2)
                assert len(conversation_runtime.start_requests) == 2
                # Active sessions may retain the server's initial title until
                # their first turn completes. Identify the row we actually
                # started rather than inventing a title from the input text.
                live_session_title = (
                    workbench_page.locator('.chat-session-main[aria-current="true"]')
                    .inner_text()
                    .strip()
                )
                assert live_session_title
                first_session_row = workbench_page.locator(".chat-session-item").first.evaluate(
                    "element => element.getBoundingClientRect().toJSON()"
                )
                assert first_session_row["height"] <= 41, first_session_row
                assert workbench_page.locator(".chat-session-item time").count() == 0
                # The shared composer supports drafting/queueing during a live
                # run. Its submit control must remain Stop, and navigation or
                # replay must never submit another RunAgent request.
                expect(message_input).to_be_enabled()
                dark_chat_colors = workbench_page.evaluate(
                    """() => {
                      const header = document.querySelector('.chat-conversation-header');
                      const composer = document.querySelector('[data-slot="composer"] form');
                      const sidebar = document.querySelector('.chat-session-sidebar');
                      const text = header.querySelector('h1');
                      const context = document.createElement('canvas').getContext('2d');
                      const rgb = value => {
                        context.clearRect(0, 0, 1, 1);
                        context.fillStyle = value;
                        context.fillRect(0, 0, 1, 1);
                        return Array.from(context.getImageData(0, 0, 1, 1).data.slice(0, 3));
                      };
                      return {
                        header: rgb(getComputedStyle(header).backgroundColor),
                        form: rgb(getComputedStyle(composer).backgroundColor),
                        messageBackground: rgb(getComputedStyle(sidebar).backgroundColor),
                        messageText: rgb(getComputedStyle(text).color),
                      };
                    }"""
                )
                for surface in ("header", "form", "messageBackground"):
                    assert max(dark_chat_colors[surface]) < 100, dark_chat_colors
                assert min(dark_chat_colors["messageText"]) > 175, dark_chat_colors
                chat_rect = rect(workbench_page, ".chat-wrap")
                assert chat_rect["top"] >= 64, chat_rect
                assert chat_rect["bottom"] <= 769, chat_rect
                composer_rect = rect(workbench_page, '[data-slot="composer"]')
                assert composer_rect["top"] >= 64, composer_rect
                assert composer_rect["bottom"] <= chat_rect["bottom"] + 1, (
                    composer_rect,
                    chat_rect,
                )
                assert composer_rect["right"] <= 1025, composer_rect
                message_scroll = message_list.evaluate(
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
                message_list.evaluate("element => { element.scrollTop = 0; }")
                scrolled_composer = rect(workbench_page, '[data-slot="composer"]')
                assert abs(scrolled_composer["top"] - composer_rect["top"]) <= 1, (
                    composer_rect,
                    scrolled_composer,
                )
                assert_no_root_overflow(workbench_page)

                workbench_page.locator(".chat-session-main").filter(has_text="展示历史答案").click()
                expect(
                    workbench_page.get_by_text("这是已经完成的历史答案。", exact=True)
                ).to_be_visible()
                expect(message_input).to_be_enabled(timeout=20000)
                expect(stop_button).to_have_count(0)
                model_trigger = workbench_page.get_by_role("button", name=re.compile(r"^模型 "))
                expect(model_trigger).to_be_visible()
                assert model_trigger.inner_text().strip() not in ("", "模型")
                workbench_page.get_by_role("button", name="风险确认", exact=True).click()
                approval_menu = workbench_page.get_by_role("menu", name="工具权限")
                expect(approval_menu).to_be_visible()
                expect(approval_menu.get_by_role("menuitemradio")).to_have_count(3)
                approval_menu.get_by_role("menuitemradio", name=re.compile(r"^请求批准")).click()
                expect(
                    workbench_page.get_by_role("button", name="请求批准", exact=True)
                ).to_be_visible()
                workbench_page.locator(".chat-session-main").filter(
                    has_text=live_session_title
                ).click()
                expect(live_answer).to_be_visible()
                expect(stop_button).to_be_visible()
                reload_studio(workbench_page)
                expect(workbench_page.locator(".app-shell")).to_have_attribute(
                    "data-view", "conversations"
                )
                try:
                    expect(live_answer).to_be_visible(timeout=15000)
                except AssertionError:
                    session_id = conversation_runtime.start_requests[-1].session_id
                    with urlopen(
                        f"{base_url}/api/v1/sessions/{session_id}/events?limit=100"
                    ) as response:
                        persisted = json.loads(response.read())
                    has_persisted_body = "正在继续生成可恢复的回答" in json.dumps(
                        persisted, ensure_ascii=False
                    )
                    failure = (
                        "Active-session reload lost streamed output "
                        f"(canonical events contain output: {has_persisted_body})"
                    )
                    verification_failures.append(failure)
                    print(f"VERIFICATION FAILED: {failure}", flush=True)
                expect(composer).to_be_visible()
                expect(stop_button).to_be_visible()
                assert len(conversation_runtime.start_requests) == 2
                context_ring = workbench_page.get_by_role("button", name="上下文用量与压缩")
                context_dialog = workbench_page.get_by_role("dialog", name="上下文", exact=True)
                expect(context_ring).to_be_visible()
                expect(context_dialog).to_be_hidden()
                context_ring.hover()
                expect(context_dialog).to_be_visible()
                expect(
                    context_dialog.get_by_role("button", name="压缩上下文", exact=True)
                ).to_be_disabled()
                workbench_page.mouse.move(0, 0)
                context_ring.focus()
                workbench_page.keyboard.press("Enter")
                expect(context_dialog).to_be_visible()
                workbench_page.keyboard.press("Escape")
                expect(context_dialog).to_be_hidden()
                expect(context_ring).to_be_focused()

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
                assert abs(chat_sidebar["width"] - 80) <= 1, chat_sidebar
                # Conversation starts compact; an explicit expansion persists.
                workbench_page.get_by_role("button", name="展开导航", exact=True).click()
                expect(workbench_page.locator(".app-shell")).to_have_attribute(
                    "data-rail", "expanded"
                )
                expect(workbench_page.locator(".sidebar")).to_have_css("width", "216px")
                assert_no_root_overflow(workbench_page)
                conversation_events.release_live.set()
                expect(
                    workbench_page.get_by_role("button", name="停止生成", exact=True)
                ).to_have_count(0, timeout=15000)
                assert len(conversation_runtime.start_requests) == 2
                reload_studio(workbench_page)
                expect(live_answer).to_be_visible(timeout=15000)
                expect(workbench_page.locator(".app-shell")).to_have_attribute(
                    "data-rail", "expanded"
                )
                expect(workbench_page.locator(".sidebar")).to_have_css("width", "216px")
                assert len(conversation_runtime.start_requests) == 2
                print(
                    "Conversation layout, history switching, permissions and run panel exercised",
                    flush=True,
                )
                workbench_context.close()

                trace_context = browser.new_context(
                    viewport={"width": 1458, "height": 861},
                    reduced_motion="reduce",
                )
                trace_page = trace_context.new_page()
                trace_page.route("**/api/v1/traces**", route_trace_fixture)
                open_studio(trace_page, base_url)
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
                reload_studio(trace_page)
                expect(trace_page.locator(".app-shell")).to_have_attribute("data-rail", "expanded")
                trace_page.get_by_role("button", name="收起导航", exact=True).click()
                expect(trace_page.locator(".app-shell")).to_have_attribute("data-rail", "compact")
                print(
                    "Trace detail, pane scrolling, Events and Raw OTLP controls passed", flush=True
                )
                trace_context.close()
                assert not verification_failures, "\n".join(verification_failures)
            finally:
                conversation_events.release_live.set()
                browser.close()


if __name__ == "__main__":
    main()
