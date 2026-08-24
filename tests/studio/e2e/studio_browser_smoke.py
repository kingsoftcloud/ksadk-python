"""Headless acceptance for the production React Studio shell and Skill imports."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright
from studio_e2e_support import studio_server, write_skill


def _candidate(page: Page, name: str):
    return page.locator(".skill-candidate").filter(has_text=name)


def _open_skill_discovery(page: Page) -> None:
    page.get_by_role("button", name="Skill", exact=True).click()
    # The current workspace groups resources under tabs; Skill is a selected
    # resource-type tab rather than a duplicate page heading.
    expect(page.get_by_role("tab").filter(has_text="Skill")).to_have_attribute(
        "aria-selected", "true"
    )
    page.get_by_role("button", name="发现 Skill", exact=True).click()
    expect(page.get_by_role("dialog", name="发现本地 Skill")).to_be_visible()
    page.get_by_label("扫描目录（逗号分隔；留空扫描安全默认目录）").fill("skills")


def _assert_multi_import_and_partial_failure(page: Page, workspace: Path) -> None:
    _open_skill_discovery(page)
    page.get_by_role("button", name="扫描候选", exact=True).click()
    expect(page.get_by_role("checkbox")).to_have_count(2)
    _candidate(page, "alpha").get_by_role("button", name="查看详情", exact=True).click()
    preview = page.get_by_role("dialog", name="alpha · 文件")
    expect(preview).to_be_visible()
    expect(preview.locator(".markdown-preview")).to_contain_text("Follow the instructions")
    preview.get_by_role("treeitem", name="check.py", exact=True).click()
    expect(preview.get_by_role("region", name="scripts/check.py 源码")).to_contain_text(
        "preview-safe"
    )
    preview.get_by_role("button", name="关闭", exact=True).click()
    page.get_by_role("button", name="全选可导入", exact=True).click()
    page.get_by_role("button", name="导入所选 2 个", exact=True).click()
    expect(page.get_by_text("已导入 2 个 Skill", exact=True)).to_be_visible()

    assert (workspace / "capabilities/skills/alpha/SKILL.md").is_file()
    assert (workspace / "capabilities/skills/beta/SKILL.md").is_file()

    write_skill(workspace / "skills", "gamma")
    page.get_by_role("button", name="扫描候选", exact=True).click()
    expect(page.get_by_role("checkbox")).to_have_count(3)
    expect(_candidate(page, "alpha")).to_contain_text("已安装")
    expect(_candidate(page, "gamma")).to_contain_text("可导入")
    _candidate(page, "alpha").get_by_role("checkbox").check()
    _candidate(page, "gamma").get_by_role("checkbox").check()

    # The scanned alpha candidate is now stale. Its failure must not stop gamma.
    write_skill(workspace / "skills", "alpha", "Changed after inspection.")
    import_button = page.get_by_role("button", name="导入所选 2 个", exact=True)
    import_button.click()
    conflict = page.get_by_role("alertdialog", name="所选 Skill 中有 1 个已安装")
    expect(conflict).to_be_visible()
    assert conflict.evaluate("dialog => dialog.contains(document.activeElement)")
    page.keyboard.press("Escape")
    expect(conflict).to_be_hidden()
    expect(import_button).to_be_focused()
    import_button.click()
    expect(conflict).to_be_visible()
    conflict.get_by_role("button", name="覆盖并继续", exact=True).click()

    expect(page.get_by_text("已导入 1 个，1 个失败", exact=True)).to_be_visible()
    expect(_candidate(page, "alpha")).to_contain_text("候选在确认前发生变化")
    expect(_candidate(page, "gamma")).to_contain_text("已导入")
    assert (workspace / "capabilities/skills/gamma/SKILL.md").is_file()
    page.get_by_role("button", name="关闭", exact=True).click()


def _assert_core_navigation(page: Page) -> None:
    # The global header is the stable page identity.  Body headings vary based
    # on whether the workspace contains an Agent or deployment yet.
    for label in (
        "Agent",
        "构建",
        "部署",
        "可观测",
        "运行资源",
        "任务编排",
    ):
        page.get_by_role("button", name=label, exact=True).click()
        expect(page.get_by_role("banner", name="当前页面").get_by_text(label, exact=True)).to_be_visible()
        page.wait_for_load_state("networkidle")


def main() -> None:
    with TemporaryDirectory(prefix="ksadk-react-studio-") as temp_dir:
        workspace = Path(temp_dir)
        alpha = write_skill(workspace / "skills", "alpha")
        (alpha / "scripts").mkdir()
        (alpha / "scripts" / "check.py").write_text('print("preview-safe")\n', encoding="utf-8")
        write_skill(workspace / "skills", "beta")

        with studio_server(workspace) as base_url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 960})
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))

                def proxy_studio_api(route) -> None:
                    requested = urlsplit(route.request.url)
                    target = f"{base_url}{requested.path}"
                    if requested.query:
                        target = f"{target}?{requested.query}"
                    route.fulfill(response=route.fetch(url=target))

                frontend_url = os.environ.get("STUDIO_FRONTEND_URL", base_url)
                if frontend_url != base_url:
                    page.route("**/api/v1/**", proxy_studio_api)
                    page.route("**/agentengine/api/v1/**", proxy_studio_api)
                page.goto(frontend_url, wait_until="networkidle")
                _assert_multi_import_and_partial_failure(page, workspace)
                _assert_core_navigation(page)
                assert page_errors == [], f"Uncaught React page errors: {page_errors}"
                page.unroute_all(behavior="wait")
            finally:
                browser.close()


if __name__ == "__main__":
    main()
