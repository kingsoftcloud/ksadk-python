"""Review the built Studio against a disposable local server (no model requests).

The optional baseline directory contains a separately built pre-change UI.
Screenshots and measured layout results are written together for review.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def run(base: str, output: Path, before_static: Path | None = None, *, height: int = 900) -> None:
    output.mkdir(parents=True, exist_ok=True)
    results = []
    routes = [
        "conversations",
        "agents",
        "create",
        "resources/model",
        "builds",
        "deployments",
        "runtime-resources",
        "automations",
        "observability",
        "evaluations",
        "plugins",
    ]
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for phase in ["before", "after"] if before_static else ["after"]:
            sizes = [1440] if phase == "before" else [1440, 1024, 768, 390]
            for width in sizes:
                for theme in ["light", "dark"]:
                    context = browser.new_context(
                        viewport={"width": width, "height": height}, color_scheme=theme
                    )
                    context.add_init_script(
                        f"localStorage.setItem('agentkit-studio-theme', '{theme}')"
                    )
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    if phase == "before":

                        def baseline(route):
                            path = urlsplit(route.request.url).path
                            if route.request.resource_type == "document":
                                route.fulfill(path=str(before_static / "index.html"))
                            elif path.startswith("/static/"):
                                route.fulfill(
                                    path=str(before_static / path.removeprefix("/static/"))
                                )
                            else:
                                route.continue_()

                        page.route("**/*", baseline)
                    for route in routes[:2] if phase == "before" else routes:
                        page.goto(base + "/studio-shell/#/" + route)
                        expect(page.locator(".app-shell")).to_be_visible()
                        expect(page.locator("html")).to_have_attribute("data-theme", theme)
                        if route == "conversations":
                            expect(
                                page.get_by_role("heading", name="有什么可以帮你？")
                            ).to_be_visible()
                            expect(page.locator(".studio-composer-area textarea")).to_be_visible()
                        elif route == "create":
                            expect(page.get_by_label("Agent 名称", exact=False)).to_be_visible()
                        else:
                            expect(page.locator(".app-shell")).to_have_attribute(
                                "data-view", route.split("/")[0]
                            )
                            expect(page.locator("#mainContent > :visible").first).to_be_visible()
                            busy = page.locator("[aria-busy='true']")
                            if busy.count() == 1:
                                busy.wait_for(state="hidden")
                        page.evaluate("""() => Promise.all(document.getAnimations()
                            .filter(a => Number(a.effect?.getComputedTiming().endTime) <= 600)
                            .map(a => a.finished.catch(() => {})))""")
                        metrics = page.evaluate("""() => {
                          const rect = selector => document.querySelector(selector)
                            ?.getBoundingClientRect();
                          const main = document.querySelector('.app-main');
                          return {
                            overflow: document.documentElement.scrollWidth > innerWidth + 1,
                            header: rect('.global-header').height,
                            rail: rect('.studio-navigation')?.width,
                            canvas: getComputedStyle(main).backgroundColor
                          };
                        }""")
                        assert not metrics["overflow"], (width, theme, route, metrics)
                        if phase == "after":
                            assert (
                                metrics["header"] == 56
                                if width >= 768 or route == "conversations"
                                else 56 <= metrics["header"] <= 225
                            ), (width, theme, route, metrics)
                            if width >= 1024:
                                assert metrics["rail"] == 280, metrics
                        name = f"{phase}-{width}-{theme}-{route.replace('/', '-')}.png"
                        page.screenshot(path=str(output / name))
                        results.append(
                            {
                                "phase": phase,
                                "width": width,
                                "theme": theme,
                                "route": route,
                                **metrics,
                            }
                        )
                    assert not errors, errors
                    context.close()
        # Both entry documents consume the same build. System mode tracks OS
        # changes and an explicit theme persists across a full reload.
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        context.add_init_script("localStorage.setItem('agentkit-studio-theme', 'system')")
        page = context.new_page()
        for entry in ["/", "/studio-core/"]:
            page.goto(base + entry + "#/conversations")
            expect(page.locator(".app-shell")).to_be_visible()
            expect(page.locator("html")).to_have_attribute("data-theme", "dark")
            page.emulate_media(color_scheme="light")
            expect(page.locator("html")).to_have_attribute("data-theme", "light")
            page.reload()
            expect(page.locator("html")).to_have_attribute("data-theme", "light")
            page.emulate_media(color_scheme="dark")
        context.close()
        browser.close()
    (output / "measurements.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"Studio visual review: {len(results)} page states and both entry documents passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--before-static", type=Path)
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args()
    run(args.base_url, args.output, args.before_static, height=args.height)
