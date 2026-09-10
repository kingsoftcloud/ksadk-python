"""Exercise grouped navigation and portaled history in a disposable Studio."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from conversation_items_browser_e2e import (
    CanonicalConversationEvents,
    RuntimeFixture,
    _runtime_inspector,
    _seed_agent,
)
from playwright.sync_api import expect, sync_playwright
from studio_e2e_support import studio_server

from ksadk.studio.service import StudioService


def run(output: Path) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    records = []
    with TemporaryDirectory(prefix="studio-navigation-") as directory:
        workspace = Path(directory)
        fixture = RuntimeFixture(CanonicalConversationEvents())
        service = StudioService(
            workspace, codex_runtime_inspector=_runtime_inspector, runtime_executor=fixture.executor
        )
        _seed_agent(service)
        with studio_server(workspace, service=service) as base, sync_playwright() as pw:
            browser = pw.chromium.launch()
            for width, theme in [(1440, "light"), (1024, "dark"), (768, "light"), (390, "dark")]:
                context = browser.new_context(viewport={"width": width, "height": 900})
                context.add_init_script(
                    f"localStorage.setItem('agentkit-studio-theme', '{theme}');"
                )
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/#/agents")
                expect(page.locator(".app-shell")).to_be_visible()

                def capture(name):
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    assert page.locator(".app-main").evaluate(
                        "e => e.scrollWidth <= e.clientWidth + 1"
                    )
                    expect(page.locator("html")).to_have_attribute("data-theme", theme)
                    page.screenshot(path=str(output / f"{width}-{theme}-{name}.png"))
                    records.append(
                        {"width": width, "theme": theme, "scenario": name, "status": "passed"}
                    )

                def open_navigation():
                    if (
                        width < 1024
                        and not page.get_by_role("navigation", name="产品导航").is_visible()
                    ):
                        page.get_by_role("button", name="展开导航", exact=True).click()
                    return page.get_by_role("navigation", name="产品导航")

                nav = open_navigation()
                expect(nav.get_by_role("button")).to_have_count(4)
                capture("four-primary-destinations")
                for group, destinations in [
                    (
                        "资源库",
                        [
                            ("模型与工具", "resources"),
                            ("运行资源", "runtime-resources"),
                            ("插件", "plugins"),
                        ],
                    ),
                    (
                        "运行中心",
                        [
                            ("构建", "builds"),
                            ("部署", "deployments"),
                            ("自动化", "automations"),
                            ("编排", "orchestration"),
                            ("可观测", "observability"),
                            ("评测", "evaluations"),
                        ],
                    ),
                ]:
                    for label, route in destinations:
                        nav = open_navigation()
                        group_button = nav.get_by_role("button", name=group, exact=True)
                        if group_button.get_attribute("aria-expanded") != "true":
                            group_button.click()
                        nav.get_by_role("button", name=label, exact=True).click()
                        expect(page.locator(".app-shell")).to_have_attribute("data-view", route)
                        if width < 1024:
                            expect(
                                page.get_by_role("dialog", name="工作区导航")
                            ).not_to_be_visible()
                        capture("destination-" + route)
                open_navigation().get_by_role("button", name="新对话", exact=True).click()
                expect(page.get_by_role("heading", name="有什么可以帮你？")).to_be_visible()
                expect(page.get_by_role("heading", name="新对话", exact=True)).to_have_count(1)
                expect(
                    page.locator('.studio-chat-shell[data-integrated-history="true"]')
                ).to_be_visible()
                expect(page.locator(".studio-chat-shell .chat-session-sidebar")).to_have_count(0)
                textarea = page.locator(".studio-composer-area textarea")
                textarea.fill("导航切换后保留的未发送草稿")
                open_navigation().get_by_role("button", name="Agent", exact=True).click()
                expect(page.locator(".app-shell")).to_have_attribute("data-view", "agents")
                # Browser back restores the mounted conversation, not a new chat.
                page.go_back()
                expect(textarea).to_have_value("导航切换后保留的未发送草稿")
                capture("chat-draft-survives-navigation")
                open_navigation()
                expect(page.get_by_role("complementary", name="会话历史")).to_be_visible()
                if width < 1024:
                    page.keyboard.press("Escape")
                    expect(page.get_by_role("button", name="展开导航", exact=True)).to_be_focused()
                    expect(page.get_by_role("complementary", name="会话历史")).not_to_be_visible()
                    page.get_by_role("button", name="对话操作", exact=True).click()
                    expect(page.get_by_role("menuitem", name="刷新", exact=True)).to_be_visible()
                    page.get_by_role("menuitem", name="运行详情", exact=True).click()
                    expect(page.locator(".chat-run-panel")).to_be_visible()
                    capture("mobile-chat-run-details")
                assert not errors, errors
                context.close()
            browser.close()
    (output / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(f"Workspace navigation: {len(run(args.output))} states passed")
