"""Headless browser acceptance for the embedded Studio authoring/chat flow."""

from __future__ import annotations

import json
import time
import urllib.request

from playwright.sync_api import expect, sync_playwright

BASE_URL = "http://127.0.0.1:18968"


def _json(path: str, *, method: str = "GET", body: dict | None = None) -> dict:
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=payload,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": f"browser-{time.time_ns()}",
        },
    )
    with urllib.request.urlopen(request) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def _wait_operation(operation_id: str) -> dict:
    for _ in range(300):
        operation = _json(f"/api/v1/operations/{operation_id}")
        if operation["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            return operation
        time.sleep(0.02)
    raise AssertionError(f"operation {operation_id} did not finish")


def _seed_session() -> None:
    created = _json(
        "/api/v1/agents",
        method="POST",
        body={
            "id": "review-helper",
            "name": "Review Helper",
            "description": "只读代码审查",
            "template": "blank",
            "spec": {
                "description": "只读代码审查",
                "instructions": {
                    "system": "检查代码，只报告确定的问题。",
                    "task": "先读取文件，再给出最小修复建议。",
                },
                "bindings": {},
            },
        },
    )
    build = _json(
        f"/api/v1/agents/{created['metadata']['id']}/builds",
        method="POST",
        body={"revision": 1, "runEvaluation": False},
    )
    build_id = _wait_operation(build["id"])["resourceId"]
    run = _json(
        f"/api/v1/builds/{build_id}/runs",
        method="POST",
        body={
            "sessionId": "ses-browser-smoke",
            "input": {"content": "请介绍你的职责、能力和工作边界。"},
            "environment": "local",
            "stream": True,
        },
    )
    assert _wait_operation(run["id"])["status"] == "SUCCEEDED"


def main() -> None:
    _seed_session()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.goto(BASE_URL, wait_until="networkidle")

        page.locator('.nav-item[data-view="chat"]').click()
        page.locator(".session-item").wait_for()
        assert page.locator(".session-item").first.bounding_box()["height"] <= 44

        markdown = page.locator("ksadk-message").last
        expect(markdown.locator("h2")).to_contain_text("审查结果")
        expect(markdown.locator("strong")).to_contain_text("确定问题")

        page.locator("#chatInput").fill("请再检查一次，并保留 Markdown 结构。")
        page.locator("#sendMessage").click()
        expect(page.locator("#inspectorStatus")).to_have_text("COMPLETED")
        expect(page.locator("#eventTimeline")).to_contain_text("execute_tool codex.command")
        expect(page.locator("#usageInput")).to_have_text("128")
        expect(page.locator("#usageOutput")).to_have_text("32")
        expect(page.locator("#usageDuration")).to_have_text("1.34 s")
        expect(page.locator("ksadk-message").last.locator("strong")).to_contain_text("确定问题")
        assert len(_json("/api/v1/runs?sessionId=ses-browser-smoke")["items"]) == 2

        page.locator('.nav-item[data-view="observability"]').click()
        expect(page.locator("#view-observability")).to_have_class("view active")
        expect(page.locator("#traceList .trace-list-item")).to_have_count(2)
        page.locator("#traceList .trace-list-item").first.click()
        expect(page.locator("#traceMetricDuration")).to_have_text("1.34 s")
        expect(page.locator("#traceMetricTokens")).to_have_text("160")
        expect(page.locator("#traceSpanTree")).to_contain_text("chat glm-5.2")
        expect(page.locator("#traceSpanTree")).to_contain_text("execute_tool codex.command")
        expect(page.locator("#traceSpanTree .trace-span-row")).to_have_count(3)
        page.get_by_role("button", name="chat glm-5.2", exact=False).click()
        page.locator('[data-trace-tab="attributes"]').click()
        expect(page.locator("#traceDetailContent")).to_contain_text("gen_ai.request.model")
        page.locator('[data-trace-tab="raw"]').click()
        expect(page.locator("#traceRawOtlp")).to_contain_text("resourceSpans")
        trace_id = page.locator("#traceIdLabel").inner_text()
        assert f"traceId={trace_id}" in page.url
        page.reload(wait_until="networkidle")
        expect(page.locator("#view-observability")).to_have_class("view active")
        expect(page.locator("#traceIdLabel")).to_have_text(trace_id)

        page.locator('.nav-item[data-view="chat"]').click()
        page.locator("[data-session-menu]").first.click()
        page.get_by_role("button", name="删除会话", exact=True).click()
        page.get_by_role("button", name="确认删除", exact=True).click()
        expect(page.locator(".session-empty")).to_contain_text("还没有会话")
        assert _json("/api/v1/runs?sessionId=ses-browser-smoke")["items"] == []

        page.locator('.nav-item[data-view="agents"]').click()
        page.get_by_role("button", name="创建 Agent", exact=True).click()
        expect(page.locator("#codexQuickCreate")).to_be_visible()
        expect(page.locator(".wizard-steps")).to_be_hidden()
        page.locator("#quickAgentPrompt").fill("你是更新后的代码审查助手，只报告确定问题。")
        page.locator("#quickCreateSubmit").click()
        expect(page.locator("#view-chat")).to_have_class("view chat-view active")
        manifest = _json("/api/v1/codex/manifest")
        assert "更新后的代码审查助手" in manifest["manifest"]["prompt"]
        browser.close()


if __name__ == "__main__":
    main()
