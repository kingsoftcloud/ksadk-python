"""PCM 浏览器 E2E（方案 §12.4）。

需先启动 Studio + 安装 playwright：
  pip install playwright && playwright install chromium
  agentengine studio <workspace> --port 18968
  python tests/studio/e2e/pcm_browser_smoke.py

验证：PCM 表单可编辑、capability 限制、编译预览、Context 预览、Inspector 页签联动。
"""

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
        headers={"Content-Type": "application/json", "Idempotency-Key": f"pcm-{time.time_ns()}"},
    )
    with urllib.request.urlopen(request) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def _seed_agent() -> str:
    created = _json(
        "/api/v1/agents",
        method="POST",
        body={
            "id": "pcm-browser-agent",
            "name": "PCM Browser Agent",
            "description": "PCM E2E",
            "template": "blank",
            "spec": {
                "runtime": {"type": "codex", "version": "0.144.4"},
                "description": "PCM E2E",
                "instructions": {"system": "你是助手", "task": "用 uv run"},
                "bindings": {},
                "context": {"ownership": "auto", "rollout": {"contextEngine": "shadow"}},
                "memory": {"enabled": False},
            },
        },
    )
    return created["metadata"]["id"]


def main() -> None:
    agent_id = _seed_agent()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.goto(f"{BASE_URL}/#agent={agent_id}", wait_until="networkidle")

        # 1. Agent Detail 含 PCM panel
        page.locator("#pcmPanel").wait_for()
        expect(page.locator("#pcmPanel")).to_contain_text("Context Policy")

        # 2. 编译预览
        page.locator("#pcmPromptPreview").click()
        page.locator("#pcmResult").wait_for()
        expect(page.locator("#pcmResult")).to_contain_text("Prompt 编译结果")
        expect(page.locator("#pcmResult")).to_contain_text("contentHash")

        # 3. Context 预览
        page.locator("#pcmContextPreview").click()
        expect(page.locator("#pcmResult")).to_contain_text("Context 预览结果")

        # 4. 编辑 PCM policy（进入编辑）
        page.locator("#detailEdit").click()
        page.locator("#pcmOwnership").wait_for()
        # capability 限制：codex 只能 native
        expect(page.locator("#pcmOwnership option")).to_have_count(2)  # auto + native
        page.locator("#pcmOwnership").select_option("native")
        page.locator("#pcmRollout").select_option("enabled")
        page.locator("#quickCreateSubmit").click()
        # 保存成功后回到 detail
        page.locator("#pcmPanel").wait_for()

        # 5. 验证保存的 policy
        detail = _json(f"/api/v1/agents/{agent_id}")
        assert detail["draft"]["spec"]["context"]["ownership"] == "native"
        assert detail["draft"]["spec"]["context"]["rollout"]["contextEngine"] == "enabled"

        browser.close()
        print("PCM browser E2E passed")


if __name__ == "__main__":
    main()
