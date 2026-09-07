"""Visual acceptance of the public Studio bundle (no editable UI source needed).

Run with uv run python tests/studio/e2e/studio_visual_review.py --output /tmp/studio-visual
Optionally pass --channel chromium to use Playwright's full Chromium build.
Screenshots are review artifacts, not pixel snapshots tied to an OS font stack.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import Page, expect, sync_playwright
from studio_e2e_support import studio_server
from studio_responsive_smoke import assert_no_root_overflow, create_test_agent

PAGES = (
    ("Agent", "agents"),
    ("会话", "conversations"),
    ("工程资源", "resources"),
    ("运行资源", "runtime"),
    ("插件", "plugins"),
    ("构建", "builds"),
    ("部署", "deployments"),
    ("自动化", "automations"),
    ("编排", "orchestration"),
    ("可观测", "traces"),
    ("评测", "evaluations"),
)
VIEWPORTS = ((390, 844), (768, 768), (1024, 768), (1440, 960), (1920, 1080))


def assert_readable(page: Page, selector: str) -> None:
    """Measure actual rendered text/background pairs, including inherited fill."""
    pairs = page.locator(selector).evaluate_all(
        r"""elements => elements.filter(e => e.getClientRects().length).map(e => {
          const rgb = value => {
            const match = value.match(/^rgba?\(([^)]+)\)$/);
            if (!match) throw new Error(`Unsupported color: ${value}`);
            return match[1].split(',').map(Number);
          };
          let background = [255, 255, 255];
          for (let parent = e; parent; parent = parent.parentElement) {
            const color = rgb(getComputedStyle(parent).backgroundColor);
            if (color.length === 3 || color[3] === 1) {
              background = color.slice(0, 3); break;
            }
            if (color[3] !== 0) throw new Error('Translucent text surface needs compositing');
          }
          return {text: e.textContent.trim().slice(0, 50),
            foreground: rgb(getComputedStyle(e).color).slice(0, 3), background};
        })"""
    )
    assert pairs, f"No visible text matched {selector}"

    def luminance(rgb: list[float]) -> float:
        values = [value / 255 for value in rgb]
        values = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
        return sum(v * weight for v, weight in zip(values, (0.2126, 0.7152, 0.0722)))

    for pair in pairs:
        a, b = luminance(pair["foreground"]), luminance(pair["background"])
        ratio = (max(a, b) + 0.05) / (min(a, b) + 0.05)
        assert ratio >= 4.5, {"contrast": ratio, **pair}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checks: list[dict] = []
    with TemporaryDirectory(prefix="studio-visual-") as tmp, studio_server(Path(tmp)) as url:
        create_test_agent(url)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=args.channel)
            try:
                for theme in ("light", "dark"):
                    for width, height in VIEWPORTS:
                        context = browser.new_context(
                            viewport={"width": width, "height": height},
                            color_scheme=theme,
                            reduced_motion="reduce",
                            device_scale_factor=1,
                        )
                        context.add_init_script(
                            f"localStorage.setItem('agentkit-studio-theme', {json.dumps(theme)})"
                        )
                        page = context.new_page()
                        errors: list[str] = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(url, wait_until="domcontentloaded")
                        expect(page.locator(".app-shell")).to_be_visible()
                        expect(page.locator("html")).to_have_attribute("data-theme", theme)
                        expect(
                            page.get_by_role("button", name="创建 Agent", exact=True).first
                        ).to_be_enabled()
                        assert_readable(page, ".button.accent:not(:disabled)")
                        assert_readable(page, ".navigation-rail .nav-item")
                        if width >= 1024:
                            assert_readable(page, ".navigation-rail .nav-label")

                        def capture(name: str) -> None:
                            assert_no_root_overflow(page)
                            main = page.locator(".app-main").bounding_box()
                            assert (
                                main and main["x"] >= 0 and main["x"] + main["width"] <= width + 1
                            )
                            page.screenshot(
                                path=str(args.output / f"{theme}-{width}-{name}.png"),
                                animations="disabled",
                            )
                            checks.append({"theme": theme, "width": width, "page": name})

                        for label, name in PAGES:
                            page.locator(".primary-nav").get_by_role(
                                "button", name=label, exact=True
                            ).click()
                            if name == "conversations":
                                expect(page.locator(".chat-composer")).to_be_visible()
                                composer = page.locator(".chat-composer").bounding_box()
                                assert composer and composer["y"] + composer["height"] <= height
                                page.get_by_role("textbox", name="消息").fill("检查窄屏输入与焦点")
                                expect(page.get_by_role("button", name="发送消息")).to_be_enabled()
                            capture(name)
                            if name == "agents":
                                assert_readable(
                                    page, ".agent-cell-copy strong, .agent-cell-copy > span"
                                )
                                trigger = page.get_by_role("combobox", name="筛选 Agent 状态")
                                trigger.click()
                                expect(page.locator(".studio-select-content")).to_be_in_viewport(
                                    ratio=1
                                )
                                expect(page.locator(".studio-select-item").first).to_be_focused()
                                page.keyboard.press("ArrowDown")
                                expect(page.locator(".studio-select-item").nth(1)).to_be_focused()
                                page.locator(".studio-select-item").nth(1).hover()
                                capture("agents-filter")
                                rows = page.locator(".studio-select-item").all()
                                assert len(rows) >= 2
                                boxes = [row.bounding_box() for row in rows]
                                for index, box in enumerate(boxes):
                                    assert box and box["height"] >= 40
                                    assert box["x"] >= 0 and box["y"] >= 0, box
                                    assert box["x"] + box["width"] <= width, box
                                    assert box["y"] + box["height"] <= height, box
                                    if index:
                                        previous = boxes[index - 1]
                                        assert previous
                                        assert box["y"] - previous["y"] - previous["height"] >= 6
                                assert_readable(page, ".studio-select-item")
                                page.keyboard.press("Escape")
                                expect(trigger).to_be_focused()
                        page.locator(".primary-nav").get_by_role(
                            "button", name="工程资源", exact=True
                        ).click()
                        for resource in ("模型", "Tool", "MCP", "Skill"):
                            page.get_by_role("tab").filter(has_text=resource).click()
                            capture(f"resources-{resource}")
                        page.locator(".primary-nav").get_by_role(
                            "button", name="Agent", exact=True
                        ).click()
                        page.get_by_role("button", name="创建 Agent", exact=True).first.click()
                        capture("create")
                        page.get_by_role("button", name="设置", exact=True).click()
                        dialog = page.get_by_role("dialog", name="设置")
                        expect(dialog).to_be_visible()
                        capture("settings")
                        page.keyboard.press("Escape")
                        expect(dialog).to_be_hidden()
                        expect(page.get_by_role("button", name="设置", exact=True)).to_be_focused()
                        assert not errors, errors
                        context.close()
                        print(f"PASS {theme} {width}×{height}", flush=True)
            finally:
                browser.close()
    (args.output / "results.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2))
    print(
        f"PASS {len(checks)} page/theme/viewport captures; "
        "contrast, composer, focus, no page errors"
    )


if __name__ == "__main__":
    main()
