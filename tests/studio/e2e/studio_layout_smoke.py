"""Layout regression checks for category navigation and progressive disclosure.

Build the React Studio first, then run with the project's uv environment.
Screenshots are optional and contain only data from a temporary workspace.
"""

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
from playwright.sync_api import Page, expect, sync_playwright
from studio_e2e_support import studio_server

from ksadk.studio.service import StudioService


def inside_viewport(page: Page, locator) -> dict:
    box = locator.bounding_box()
    assert box, "Control is not visible"
    viewport = page.viewport_size
    assert viewport and box["x"] >= -1 and box["y"] >= -1, box
    assert box["x"] + box["width"] <= viewport["width"] + 1, box
    assert box["y"] + box["height"] <= viewport["height"] + 1, box
    return box


def run(output: Path | None = None) -> list[dict]:
    results: list[dict] = []
    if output:
        output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="studio-layout-") as temporary:
        workspace = Path(temporary)
        fixture = RuntimeFixture(CanonicalConversationEvents())
        service = StudioService(
            workspace, codex_runtime_inspector=_runtime_inspector, runtime_executor=fixture.executor
        )
        _seed_agent(service)
        with studio_server(workspace, service=service) as base, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width, theme in [
                (1440, "light"),
                (1024, "light"),
                (768, "light"),
                (390, "light"),
                (1440, "dark"),
                (390, "dark"),
            ]:
                context = browser.new_context(
                    viewport={"width": width, "height": 900}, color_scheme=theme
                )
                context.add_init_script(
                    f"localStorage.setItem('agentkit-studio-theme', '{theme}');"
                )
                page = context.new_page()
                page_errors = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))

                def capture(name: str) -> None:
                    expect(page.locator("html")).to_have_attribute("data-theme", theme)
                    page.evaluate("""() => Promise.all(document.getAnimations()
                        .filter(animation =>
                          Number(animation.effect?.getComputedTiming().endTime) <= 600)
                        .map(animation => animation.finished.catch(() => {})))""")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    if output:
                        page.screenshot(path=str(output / f"{width}-{theme}-{name}.png"))
                    results.append(
                        {"width": width, "theme": theme, "scenario": name, "status": "passed"}
                    )

                def navigate(route: str) -> None:
                    page.goto(base + "/#/" + route)
                    expect(page.locator(".app-shell")).to_be_visible()

                navigate("agents")
                expect(
                    page.get_by_role("button", name="Conversation Items Agent 的更多操作")
                ).to_be_visible()
                trigger = page.get_by_role("combobox", name="筛选 Agent 状态")
                trigger.click()
                expect(page.get_by_role("option", name="草稿", exact=True)).to_be_visible()
                assert (
                    page.locator('.studio-select-trigger[aria-label="筛选 Agent 状态"]').evaluate(
                        "e => getComputedStyle(e).outlineStyle"
                    )
                    == "none"
                )
                capture("agent-select")
                page.keyboard.press("Escape")
                page.get_by_role("button", name="Conversation Items Agent 的更多操作").click()
                page.get_by_role("menuitem", name="删除", exact=True).click()
                expect(page.get_by_role("alertdialog")).to_be_visible()
                capture("agent-delete-confirm")
                page.keyboard.press("Escape")
                expect(
                    page.get_by_role("button", name="Conversation Items Agent 的更多操作")
                ).to_be_focused()

                if width < 1024:
                    page.get_by_role("button", name="展开导航", exact=True).click()
                page.get_by_role("button", name="设置", exact=True).click()
                expect(page.get_by_role("heading", name="设置", exact=True)).to_be_visible()
                for category in ["通用", "云端连接", "运行与沙箱", "模型与凭证", "关于"]:
                    page.get_by_role("button", name=category, exact=True).click()
                    page.wait_for_timeout(150)
                    inside_viewport(page, page.get_by_role("heading", name="设置", exact=True))
                    inside_viewport(page, page.get_by_role("button", name="关闭", exact=True))
                    inside_viewport(page, page.get_by_role("button", name="保存", exact=True))
                    if category == "云端连接":
                        page.get_by_label("Region", exact=True).fill("layout-test-region")
                    capture("settings-" + category)
                page.get_by_role("button", name="云端连接", exact=True).click()
                expect(page.get_by_label("Region", exact=True)).to_have_value("layout-test-region")
                page.get_by_role("button", name="取消", exact=True).click()

                navigate("resources/model")
                expect(page.locator(".studio-data-table")).not_to_have_attribute(
                    "data-state", "loading"
                )
                expect(page.get_by_role("combobox", name="筛选资源来源")).not_to_be_visible()
                page.locator(".resource-filter-details > summary").click()
                page.get_by_role("combobox", name="筛选资源来源").click()
                page.get_by_role("option", name="工作区自定义", exact=True).click()
                expect(page.locator(".resource-filter-details > summary")).to_contain_text("已设置")
                expect(page.locator(".studio-data-table")).not_to_have_attribute(
                    "data-state", "loading"
                )
                capture("resource-filters")
                page.get_by_role("button", name="清除筛选", exact=True).first.click()
                expect(page.get_by_role("combobox", name="筛选资源来源")).to_contain_text(
                    "全部来源"
                )

                navigate("create")
                expect(page.locator(".app-shell")).to_have_attribute(
                    "data-rail", "expanded" if width >= 1024 else "compact"
                )
                expect(page.get_by_label("Agent 名称", exact=False)).to_be_visible()
                expect(page.locator("#quickDescription")).not_to_be_visible()
                if width >= 1024:
                    steps = page.locator(".wizard-steps .wizard-step").all()
                    tops = [step.bounding_box()["y"] for step in steps]
                    assert max(tops) - min(tops) <= 1, tops
                    assert page.locator(".create-rail").bounding_box()["height"] < 150
                else:
                    page.get_by_role("button", name="查看创建入口与配置步骤").click()
                    expect(page.get_by_role("dialog", name="创建方式", exact=True)).to_be_visible()
                    page.keyboard.press("Escape")
                page.get_by_role("button", name="继续", exact=True).click()
                expect(page.locator("#quickPrompt")).to_have_attribute("aria-invalid", "true")
                capture("create-validation")
                page.locator(".secondary-settings > summary").filter(has_text="标识与描述").click()
                expect(page.locator("#quickDescription")).to_be_visible()
                page.get_by_role("button", name="完整摘要", exact=True).click()
                expect(page.get_by_role("dialog", name="配置摘要", exact=True)).to_be_visible()
                inside_viewport(page, page.get_by_role("button", name="关闭", exact=True))
                capture("create-summary")
                page.keyboard.press("Escape")

                navigate("conversations")
                expect(
                    page.get_by_role("heading", name="有什么可以帮你？", exact=True)
                ).to_be_visible()
                expect(page.locator(".app-shell")).to_have_attribute(
                    "data-rail", "expanded" if width >= 1024 else "compact"
                )
                composer = page.locator(".studio-composer-area")
                assert composer.bounding_box()["width"] <= 833
                capture("chat-empty")
                inside_viewport(page, page.locator(".studio-composer-area textarea"))
                inside_viewport(
                    page, page.locator('.studio-composer-area button[title="发送消息"]')
                )
                if width < 1024:
                    expect(page.get_by_role("complementary", name="会话历史")).not_to_be_visible()
                    page.get_by_role("button", name="展开导航").click()
                    expect(page.get_by_role("complementary", name="会话历史")).to_be_visible()
                    capture("chat-history")
                    page.keyboard.press("Escape")
                    expect(page.get_by_role("button", name="展开导航")).to_be_focused()
                assert not page_errors, page_errors
                context.close()
            browser.close()
    if output:
        (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.output)
    print(f"Studio layout smoke: {len(result)} states passed")
