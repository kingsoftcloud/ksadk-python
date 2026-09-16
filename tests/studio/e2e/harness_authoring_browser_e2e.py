"""Browser + real Studio/DSH acceptance with a deterministic model.

Run from the repository root with PYTHONPATH=. and the project Python.
"""

import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from playwright.sync_api import expect, sync_playwright
from studio_e2e_support import studio_server

from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.studio.contracts import ModelSpec
from ksadk.studio.service import StudioService
from tests.studio.test_dsh_provider_registration import _managed_profile


class Reasoner:
    async def complete(self, *, model, prompt, messages, tools):  # noqa: ANN001
        prior = [m.get("content") for m in messages if m.get("role") == "assistant"]
        return HarnessReasoningTurn(
            final_text="second:remembered" if "first:stored" in prior else "first:stored"
        )


def wait_operation(page, url: str, operation: dict) -> str:  # noqa: ANN001
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = page.request.get(f"{url}/api/v1/operations/{operation['id']}").json()
        if result["status"] == "SUCCEEDED":
            return result["resourceId"]
        assert result["status"] not in {"FAILED", "CANCELLED"}, result
        time.sleep(0.05)
    raise AssertionError(f"Operation timed out: {operation['id']}")


def main() -> None:
    with (TemporaryDirectory(prefix="ksadk-harness-authoring-") as directory,
          pytest.MonkeyPatch.context() as monkeypatch):
        workspace = Path(directory)
        monkeypatch.setenv("HARNESS_BROWSER_FIXTURE_KEY", "fixture-not-a-secret")
        _managed_profile(workspace, monkeypatch)
        service = StudioService(workspace, harness_reasoner=Reasoner())
        service.catalog.create_model_profile(
            name="fixture-model", display_name="Browser Fixture Model",
            version="1.0.0", description="Offline authoring fixture",
            spec=ModelSpec(model="fixture-model",
                           endpoint_url="https://model.example.test/v1/chat/completions",
                           credential_ref="env://HARNESS_BROWSER_FIXTURE_KEY"),
        )
        with studio_server(workspace, service=service) as url, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                creates = []
                page.on("request", lambda r: creates.append(r)
                        if r.url.endswith("/api/v1/authoring/quick") else None)
                page.goto(f"{url}/#/create")
                page.get_by_role("combobox", name="Runtime", exact=True).click()
                page.get_by_role("option", name="通用智能体 · KsADK Harness", exact=True).click()
                page.get_by_text("本地运行：已授权 · 高级权限", exact=True).click()
                page.get_by_role("checkbox", name="允许通用智能体", exact=False).check()
                page.locator("#quickAgentName").fill("Harness Browser Acceptance")
                page.locator("#quickPrompt").fill("Answer concisely using verified evidence.")
                page.get_by_role("button", name="继续", exact=True).click()
                page.get_by_role("button", name="选择模型", exact=True).click()
                page.get_by_role("option", name="Browser Fixture Model", exact=False).click()
                page.keyboard.press("Escape")
                page.get_by_role("button", name="继续", exact=True).click()
                expect(page.get_by_role("button", name="一键优化 Prompt")).to_be_visible()
                page.get_by_role("button", name="继续", exact=True).click()
                # Creating a draft does not require an external Provider or model call.
                assert not creates, "Continue must never submit the Agent form"
                page.locator('input[name="buildAfterCreate"]').uncheck()
                with page.expect_response(
                    lambda r: r.url.endswith("/api/v1/authoring/quick")
                ) as saved:
                    page.get_by_role("button", name="创建 Agent", exact=True).click()
                response = saved.value
                assert response.ok, response.text()
                assert len(creates) == 1, "Create must send exactly one request"
                agent_id = response.json()["metadata"]["id"]
                page.goto(f"{url}/#/agents/{agent_id}/edit")
                expect(page.get_by_role("button", name="保存修改")).to_be_visible()
                expect(
                    page.get_by_text("通用智能体 · KsADK Harness", exact=True).first
                ).to_be_visible()
                page.get_by_text("本地运行：已授权 · 高级权限", exact=True).click()
                expect(page.get_by_role("checkbox", name="允许通用智能体",
                                        exact=False)).to_be_checked()
                page.get_by_role("button", name="能力绑定", exact=True).click()
                expect(page.get_by_role("button", name="选择绑定 MCP")).to_be_visible()
                assert "当前 Runtime 不支持新增 MCP" not in page.locator("body").inner_text()
                with page.expect_response(
                    lambda r: r.request.method == "PUT" and f"/agents/{agent_id}" in r.url
                ) as updated:
                    page.get_by_role("button", name="保存修改").click()
                assert updated.value.ok, updated.value.text()
                spec = updated.value.request.post_data_json
                assert spec["runtime"]["type"] == "harness"
                assert "process:host-user" in spec["security"]["allowedPermissions"]
                assert spec["runtime"].get("entryPoint") is None
                page.reload()
                expect(page.get_by_role("button", name="保存修改")).to_be_visible()
                built = page.request.post(f"{url}/api/v1/agents/{agent_id}/builds",
                    data={"revision": 2}, headers={"Idempotency-Key": "browser-build"})
                assert built.ok, built.text()
                build_id = wait_operation(page, url, built.json())
                for turn, expected in enumerate(["first:stored", "second:remembered"]):
                    started = page.request.post(f"{url}/api/v1/builds/{build_id}/runs",
                        data={"input": {"role": "user", "content": f"Turn {turn}"},
                              "sessionId": "browser-session"},
                        headers={"Idempotency-Key": f"browser-turn-{turn}"})
                    assert started.ok, started.text()
                    run_id = wait_operation(page, url, started.json())
                    run = page.request.get(f"{url}/api/v1/runs/{run_id}").json()
                    assert run["output"] == expected, run
                print("PASS: browser create -> save -> edit -> reload -> DSH Build -> two turns")
            finally:
                browser.close()


if __name__ == "__main__":
    main()
