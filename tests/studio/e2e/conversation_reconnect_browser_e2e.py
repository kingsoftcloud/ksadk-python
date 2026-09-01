"""Browser acceptance for typed Conversation replay without duplicate turns."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from playwright.sync_api import Page, Route, expect, sync_playwright

from ksadk.studio.contracts import (
    AgentSpec,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.service import StudioService
from tests.studio.e2e.studio_e2e_support import studio_server
from tests.studio.runtime_adapter_fixtures import RuntimeFixture, standard_codex_events

AGENT_ID = "conversation-reconnect-agent"
AGENT_NAME = "Conversation Reconnect Agent"
FINAL_ANSWER = "发现除零风险。请先检查空列表。"


def _runtime_inspector(_runtime: object) -> tuple[str, str, str]:
    return "0.8.2", "0.147.0", "codex-cli 0.147.0"


def _seed_agent(service: StudioService) -> None:
    draft = service.create_studio_agent(
        agent_id=AGENT_ID,
        name=AGENT_NAME,
        spec=AgentSpec(
            runtime=RuntimeRef(type="codex", version="0.147.0"),
            model=ModelSpec(
                model="model-example",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            instructions=Instructions(system="Keep every turn identity distinct."),
            security=SecuritySpec(
                network=NetworkPolicy(allowed_hosts=["model.example.com"])
            ),
        ),
    )
    service.builder.build(draft)


def _json(base_url: str, path: str) -> dict:
    with urlopen(f"{base_url}{path}") as response:
        return json.loads(response.read())


def _first_canonical_item_prefix(body: str) -> str:
    blocks = [block for block in body.replace("\r\n", "\n").split("\n\n") if block]
    for index, block in enumerate(blocks):
        if '"conversationItem"' in block:
            return "\n\n".join(blocks[: index + 1]) + "\n\n"
    raise AssertionError("initial stream did not contain a canonical ConversationItem")


def _exercise_reconnect(page: Page, base_url: str) -> None:
    initial_posts = 0
    replay_requests: list[str] = []

    def cut_first_stream(route: Route) -> None:
        nonlocal initial_posts
        initial_posts += 1
        if initial_posts > 1:
            route.continue_()
            return
        response = route.fetch()
        partial = _first_canonical_item_prefix(response.body().decode("utf-8"))
        route.fulfill(response=response, body=partial)

    page.route("**/api/v1/builds/*/conversation:stream", cut_first_stream)
    page.on(
        "request",
        lambda request: replay_requests.append(request.url)
        if "/api/v1/runs/" in request.url and "/events?after=" in request.url
        else None,
    )

    # Studio can keep background plugin/session requests open; readiness is the
    # rendered conversation surface, not a process-wide network-idle window.
    page.goto(f"{base_url}/#/conversations", wait_until="domcontentloaded")
    expect(page.get_by_role("combobox", name="切换会话目标")).to_contain_text(
        AGENT_NAME
    )
    composer = page.get_by_role("textbox", name="消息")
    expect(composer).to_be_enabled()

    composer.fill("检查一次")
    page.get_by_role("button", name="发送消息").click()
    answer = page.locator('article[data-role="assistant"] .chat-markdown').filter(
        has_text=FINAL_ANSWER
    )
    expect(answer).to_have_count(1, timeout=15_000)
    expect(composer).to_have_value("")
    assert initial_posts == 1
    assert replay_requests, "typed stream EOF must resume from the durable event cursor"
    assert any("after=" in url and not url.endswith("after=0") for url in replay_requests)

    # A second turn uses the same Session but creates one new Run. Identical
    # answer text must remain visible twice because item identity, not text,
    # controls reduction.
    composer.fill("再检查一次")
    page.get_by_role("button", name="发送消息").click()
    expect(answer).to_have_count(2, timeout=15_000)
    assert initial_posts == 2

    runs = _json(base_url, "/api/v1/runs")["items"]
    assert len(runs) == 2
    assert len({run["id"] for run in runs}) == 2
    assert len({run["sessionId"] for run in runs}) == 1


def main() -> None:
    with TemporaryDirectory(prefix="ksadk-conversation-reconnect-") as temp_dir:
        workspace = Path(temp_dir)
        service = StudioService(
            workspace,
            codex_runtime_inspector=_runtime_inspector,
            runtime_executor=RuntimeFixture(standard_codex_events).executor,
        )
        _seed_agent(service)
        with studio_server(workspace, service=service) as base_url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 960})
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                _exercise_reconnect(page, base_url)
                assert page_errors == [], f"Uncaught React page errors: {page_errors}"
                page.unroute_all(behavior="wait")
            finally:
                browser.close()


if __name__ == "__main__":
    main()
