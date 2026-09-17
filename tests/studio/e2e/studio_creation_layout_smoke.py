"""Create wizard layout and persistence checks in a disposable workspace.

Build React first, then run with PYTHONPATH=. uv run --no-sync python
 tests/studio/e2e/studio_creation_layout_smoke.py --output /tmp/studio-creation
Only local composition/build APIs are used; no model requests are sent.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from conversation_items_browser_e2e import (
    CanonicalConversationEvents, RuntimeFixture, _seed_agent,
)
from playwright.sync_api import expect, sync_playwright
from studio_e2e_support import studio_server
from studio_layout_smoke import inside_viewport

from ksadk.studio.contracts import ModelSpec
from ksadk.studio.service import StudioService
from ksadk.studio.workspace_registry import WorkspaceRegistry


def creation_runtime_inspector(runtime: object) -> tuple[str, str, str]:
    # Match each declaration's pinned version; no installed CLI is executed.
    version = str(getattr(runtime, "version", None) or "0.147.0")
    return "0.8.2", version, f"codex-cli {version}"


def run(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with TemporaryDirectory(prefix="studio-creation-layout-") as temporary:
        fixture = RuntimeFixture(CanonicalConversationEvents())
        with patch("ksadk.studio.service.WorkspaceRegistry", return_value=WorkspaceRegistry(Path(temporary) / "registry")):
            service = StudioService(Path(temporary), codex_runtime_inspector=creation_runtime_inspector,
                                    runtime_executor=fixture.executor)
        _seed_agent(service)
        service.catalog.create_model_profile(
            name="creation-layout-fixture", display_name="布局测试模型", version="1.0.0",
            description="隔离浏览器验证", spec=ModelSpec(model="model-example",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY"),
        )
        with studio_server(Path(temporary), service=service) as base, sync_playwright() as pw:
            browser = pw.chromium.launch()
            for width, theme in [(1440, "light"), (1024, "light"), (768, "light"),
                                 (390, "light"), (320, "dark"), (1440, "dark")]:
                context = browser.new_context(viewport={"width": width, "height": 960}, color_scheme=theme)
                context.add_init_script(f"localStorage.setItem('agentkit-studio-theme', '{theme}')")
                page = context.new_page()
                page.set_default_timeout(15000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def capture(scenario):
                    page.evaluate("""() => Promise.all(document.getAnimations()
                      .filter(a => Number(a.effect?.getComputedTiming().endTime) <= 600)
                      .map(a => a.finished.catch(() => {})))""")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), scenario
                    expect(page.locator("html")).to_have_attribute("data-theme", theme)
                    if page.locator(".wizard-actions").is_visible() and not page.get_by_role("dialog").count():
                        inside_viewport(page, page.get_by_role("button", name="保存草稿", exact=True))
                        inside_viewport(page, page.get_by_role("button", name="继续", exact=True)
                                        if page.get_by_role("button", name="继续", exact=True).is_visible()
                                        else page.get_by_role("button", name="创建 Agent", exact=True))
                    filename = f"{width}-{theme}-{scenario}.png"
                    page.screenshot(path=str(output / filename))
                    results.append({"width": width, "theme": theme, "scenario": scenario, "status": "passed"})
                    (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
                    print(f"PASS {width} {theme} {scenario}", flush=True)

                def select_mode(label):
                    if width < 1024:
                        page.get_by_role("button", name="查看创建入口与配置步骤").click()
                    page.get_by_role("tab", name=label, exact=True).click()
                    expect(page.get_by_role("tabpanel", name=label, exact=True)).to_be_visible()

                try:
                    page.goto(base + "/studio-shell/#/create")
                    expect(page.locator("#quickAgentName")).to_be_visible()
                    expect(page.locator('.global-header .badge')).to_have_attribute("data-state", "ready", timeout=30000)
                    capture("step1-empty")
                    page.get_by_role("button", name="继续", exact=True).click()
                    expect(page.locator("#quickPrompt")).to_have_attribute("aria-invalid", "true")
                    page.locator("#quickAgentName").fill(f"工作计划助手 {width}")
                    page.locator("#quickPrompt").fill("帮助我整理工作计划，先确认目标，再提供简洁可执行的步骤。")
                    if width >= 1280:
                        preview = page.get_by_role("complementary", name="配置预览")
                        expect(preview).to_contain_text(f"工作计划助手 {width}")
                        expect(preview).to_contain_text("帮助我整理工作计划")
                    capture("step1-defined")
                    page.get_by_role("button", name="保存草稿", exact=True).click()
                    expect(page.get_by_text("草稿已保存", exact=True)).to_be_visible()
                    page.reload()
                    expect(page.locator("#quickAgentName")).to_have_value(f"工作计划助手 {width}", timeout=30000)
                    page.get_by_role("button", name="继续", exact=True).click()
                    expect(page.get_by_role("heading", name="绑定能力", exact=True)).to_be_focused()
                    page.get_by_role("button", name="选择模型", exact=True).click()
                    option = page.get_by_role("option", name=re.compile("布局测试模型"))
                    expect(option).to_be_visible()
                    if option.get_by_role("checkbox").get_attribute("aria-checked") != "true":
                        option.click()
                    page.keyboard.press("Escape")
                    page.get_by_role("heading", name="绑定能力", exact=True).scroll_into_view_if_needed()
                    expect(page.locator(".wizard-optional-capabilities")).not_to_have_attribute("open", "")
                    capture("step2-capabilities")
                    page.locator(".wizard-optional-capabilities > summary").click()
                    expect(page.get_by_role("button", name="连接金山云", exact=True)).to_be_visible()
                    page.locator(".wizard-optional-capabilities > summary").click()
                    page.get_by_role("button", name="继续", exact=True).click()
                    expect(page.locator("#composedSystemPrompt")).not_to_have_value("")
                    page.locator("#composedSystemPrompt").fill("你是工作计划助手。使用中文，以简洁、有序的步骤回答。")
                    page.locator("#composedTaskPrompt").fill("确认目标并列出三项行动。")
                    page.get_by_role("heading", name="提示词与策略").scroll_into_view_if_needed()
                    capture("step3-prompts")
                    page.get_by_role("button", name="上一步", exact=True).click()
                    expect(page.get_by_role("button", name="移除 布局测试模型")).to_be_visible()
                    page.get_by_role("button", name="继续", exact=True).click()
                    expect(page.locator("#composedTaskPrompt")).to_have_value("确认目标并列出三项行动。")
                    page.get_by_role("button", name="继续", exact=True).click()
                    expect(page.get_by_role("heading", name="检查并创建", exact=True)).to_be_visible()
                    capture("step4-review")
                    page.get_by_role("button", name="完整摘要", exact=True).click()
                    expect(page.get_by_role("dialog", name="配置摘要")).to_contain_text("布局测试模型")
                    capture("summary-drawer")
                    page.keyboard.press("Escape")
                    if width < 1024:
                        page.get_by_role("button", name="查看创建入口与配置步骤").click()
                        page.get_by_role("button", name=re.compile("定义 Agent 名称")).click()
                        expect(page.get_by_role("dialog", name="创建方式", exact=True)).not_to_be_visible()
                        expect(page.get_by_role("heading", name="定义 Agent", exact=True)).to_be_focused()
                        page.get_by_role("button", name="查看创建入口与配置步骤").click()
                        page.get_by_role("button", name=re.compile("检查并创建 确认配置")).click()
                    if width in (1440, 390) and theme == "light":
                        with page.expect_response(lambda response: response.request.method == "POST"
                                and response.url.endswith("/api/v1/authoring/quick")) as pending:
                            page.get_by_role("button", name="创建 Agent", exact=True).click()
                        assert pending.value.status == 201, pending.value.text()
                        created = pending.value.json()
                        assert created["spec"]["instructions"]["task"] == "确认目标并列出三项行动。"
                        expect(page.locator(".app-shell")).to_have_attribute("data-view", "conversations", timeout=20000)
                        capture("created-and-opened-chat")
                        page.goto(base + "/studio-shell/#/create")
                    for label, scenario in [("对话构建", "mode-conversation"), ("导入", "mode-import"), ("项目识别", "mode-project")]:
                        select_mode(label)
                        capture(scenario)
                    select_mode("快速创建")
                    assert not errors, errors
                except Exception:
                    page.screenshot(path=str(output / f"{width}-{theme}-failure.png"), full_page=True)
                    raise
                finally:
                    context.close()
            browser.close()
    print(f"Creation layout: {len(results)} states passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/tmp/studio-creation-layout"))
    run(parser.parse_args().output)
