"""Browser acceptance for the reviewed Soul and Memory policy editor."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen

from playwright.sync_api import Locator, Page, expect, sync_playwright
from studio_e2e_support import studio_server

from ksadk.studio.service import StudioService


def _create_agent(base_url: str) -> None:
    payload = {
        "id": "policy-agent",
        "name": "Policy Agent",
        "description": "Soul and Memory browser fixture",
        "template": "blank",
        "spec": {
            "description": "Soul and Memory browser fixture",
            "runtime": {"type": "codex"},
            "instructions": {
                "system": "Answer with evidence.",
                "task": "Complete the request.",
            },
            "soul": {
                "schemaVersion": "agentkit.soul/v1",
                "identity": "A careful release reviewer.",
                "principles": ["Prefer evidence", "State uncertainty"],
                "boundaries": ["Never expose credentials"],
                "tone": "Concise and direct.",
            },
            "model": {
                "provider": "openai-compatible",
                "model": "fixture-model",
                "endpointUrl": "https://model.example.com/v1/chat/completions",
                "credentialRef": "env://MODEL_API_KEY",
            },
            "context": {
                "maxInputTokens": 4096,
                "reserveOutputTokens": 512,
                "rollout": {"contextEngine": "shadow", "memoryWrite": "enabled"},
            },
            "memory": {
                "enabled": True,
                "providerRef": "memory-browser-fixture",
                "recall": {
                    "enabled": True,
                    "maxTokens": 2048,
                    "topK": 6,
                    "minScore": 0.55,
                },
                "write": {"mode": "explicit_only", "flushBeforeCompaction": True},
                "scopes": ["workspace", "agent", "user"],
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


def _box(locator: Locator) -> dict[str, float]:
    value = locator.bounding_box()
    assert value is not None
    return value


def _assert_aligned(left: Locator, right: Locator) -> None:
    left_box = _box(left)
    right_box = _box(right)
    assert abs(left_box["y"] - right_box["y"]) <= 2, (left_box, right_box)
    assert abs(left_box["width"] - right_box["width"]) <= 2, (left_box, right_box)


def _assert_no_page_overflow(page: Page) -> None:
    dimensions = page.evaluate(
        """() => ({
          viewport: document.documentElement.clientWidth,
          content: document.documentElement.scrollWidth,
        })"""
    )
    assert dimensions["content"] <= dimensions["viewport"] + 1, dimensions


def main() -> None:
    with TemporaryDirectory(prefix="ksadk-agent-policy-editor-") as temp_dir:
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
            _create_agent(base_url)
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(
                    f"{base_url}/#/agents/policy-agent/edit",
                    wait_until="domcontentloaded",
                )

                soul = page.get_by_role("group", name="Soul · 稳定人格")
                expect(soul).to_be_visible()
                expect(soul.get_by_role("textbox", name="身份定义")).to_have_value(
                    "A careful release reviewer."
                )
                expect(soul.get_by_role("textbox", name="原则")).to_have_value(
                    "Prefer evidence\nState uncertainty"
                )
                expect(soul.get_by_role("textbox", name="边界")).to_have_value(
                    "Never expose credentials"
                )
                expect(soul.get_by_role("textbox", name="表达语气")).to_have_value(
                    "Concise and direct."
                )
                provenance = soul.get_by_role("status", name="Soul 编译来源")
                expect(provenance).to_contain_text("AgentSpec.soul")
                expect(provenance).to_contain_text("managed-runtime.base_instructions")
                digest = provenance.locator("code").nth(1).text_content() or ""
                algorithm, _, value = digest.partition(":")
                assert algorithm == "sha256", digest
                assert len(value) == 64 and all(
                    char in "0123456789abcdef" for char in value
                ), digest
                soul_fields = soul.locator(".soul-list-grid > .studio-form-field")
                _assert_aligned(soul_fields.nth(0), soul_fields.nth(1))
                manifest = page.get_by_role("region", name="agentkit.yaml 源码")
                expect(manifest).to_contain_text("soul_source: AgentSpec.soul")
                expect(manifest).to_contain_text(f"soul_digest: {digest}")
                _assert_no_page_overflow(page)

                page.get_by_role("button", name="运行策略", exact=True).click()
                memory = page.get_by_role("group", name="Memory · 跨会话策略")
                expect(memory).to_be_visible()
                expect(memory.get_by_role("textbox", name="Memory Provider")).to_have_value(
                    "memory-browser-fixture"
                )
                expect(memory.get_by_role("spinbutton", name="召回 Token 上限")).to_have_value(
                    "2048"
                )
                expect(memory.get_by_role("spinbutton", name="召回条数")).to_have_value("6")
                expect(memory.get_by_role("spinbutton", name="最小相关度")).to_have_value(
                    "0.55"
                )
                expect(memory.get_by_role("status", name="Memory 策略来源")).to_contain_text(
                    "AgentSpec.memory"
                )
                memory_fields = memory.locator(".memory-policy-grid > .studio-form-field")
                _assert_aligned(memory_fields.nth(0), memory_fields.nth(1))
                _assert_no_page_overflow(page)

                page.set_viewport_size({"width": 768, "height": 900})
                _assert_no_page_overflow(page)
                source_items = memory.locator(".source-provenance > span")
                first_box = _box(source_items.nth(0))
                second_box = _box(source_items.nth(1))
                assert second_box["y"] > first_box["y"], (first_box, second_box)
            finally:
                browser.close()


if __name__ == "__main__":
    main()
