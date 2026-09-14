"""Real browser vertical for Scheduler Lite and the built-in KsADK Harness.

The browser creates and runs both continuity modes through production Studio
HTTP routes.  AgentControl, the Kernel worker, HarnessRuntimeAdapter and the
canonical SessionEvent store and production model client are real.  Only the
external model endpoint is replaced by a deterministic local HTTP service, so
an ``accepted`` receipt can never manufacture the terminal occurrence asserted
below.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import Request, urlopen

from playwright.sync_api import Page, expect, sync_playwright
from studio_e2e_support import studio_server

from ksadk.plugins.dsh_home import prepare_studio_dsh_home
from ksadk.plugins.providers.harness_dsh import shipped_harness_dsh_bundle
from ksadk.studio.contracts import AgentSpec
from ksadk.studio.service import StudioService
from tests.e2e.chat_completions_stub import DeterministicChatCompletionsStub

AGENT_ID = "scheduler-harness-agent"
AGENT_NAME = "Scheduler Harness Agent"
CONTINUE_SESSION_ID = "scheduler-harness-continuation"


class SchedulingChatStub(DeterministicChatCompletionsStub):
    """Replace only external model inference; all tool effects are production."""

    def _respond(self, path, authorization, payload):
        response = super()._respond(path, authorization, payload)
        names = [item.get("function", {}).get("name") for item in payload.get("tools", [])]
        if "studio_schedule" not in names:
            return response
        text = payload["messages"][-1]["content"]
        interval = "每7分钟" in text
        intent = {
            "action": "create",
            "display_name": "服务巡检" if interval else "每日简报",
            "prompt": "检查服务状态" if interval else "生成昨日工作日报",
            "schedule": {"kind": "interval", "everySeconds": 420, "timezone": "Asia/Shanghai"}
            if interval
            else {"kind": "cron", "expression": "0 10 * * *", "timezone": "Asia/Shanghai"},
        }
        response["choices"][0].update(
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "schedule-intent",
                            "type": "function",
                            "function": {
                                "name": "studio_schedule",
                                "arguments": json.dumps(intent, ensure_ascii=False),
                            },
                        }
                    ],
                },
            }
        )
        return response


def _managed_harness_profile(workspace: Path) -> dict[str, str]:
    """Install the wheel-owned Bundle behind a deterministic DSH CLI seam."""

    home = workspace / ".agentkit" / "dsh-home"
    prepare_studio_dsh_home(home)
    profile = home / "profiles" / "studio"
    installed = profile / "node_modules" / "@kingsoftcloud" / "ksadk-harness-provider"
    installed.parent.mkdir(parents=True)
    shutil.copytree(shipped_harness_dsh_bundle().root, installed)
    (profile / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"@kingsoftcloud/ksadk-harness-provider": "1.0.0"},
                "dsh": {"profile": {"bundles": ["@kingsoftcloud/ksadk-harness-provider"]}},
            }
        ),
        encoding="utf-8",
    )
    executable = workspace / ".agentkit" / "dsh-fixture"
    executable.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *--version*) echo 0.1.1-rc.2;;\n"
        "  *--dump-config*) echo 'profile: studio; harness: 1.0.0';;\n"
        "  *) exit 2;;\n"
        "esac\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return {
        "KSADK_DSH_HOME": str(home),
        "KSADK_DSH_PROFILE": "studio",
        "KSADK_DSH_BIN": str(executable),
    }


def _json(base_url: str, path: str) -> dict:
    request = Request(f"{base_url}{path}", headers={"Accept": "application/json"})
    with urlopen(request) as response:
        raw = response.read()
        assert response.status < 300, raw.decode("utf-8")
    return json.loads(raw) if raw else {}


def _agent_spec(*, endpoint_url: str) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "description": "Scheduler Harness browser fixture",
            "runtime": {"type": "harness"},
            "instructions": {
                "system": "You are the real built-in Harness scheduler fixture.",
                "task": "Retain prior scheduled turns in one continued session.",
            },
            "model": {
                "provider": "openai-compatible",
                "model": "fixture-model",
                "endpointUrl": endpoint_url,
                "credentialRef": "env://MODEL_API_KEY",
                "parameters": {"temperature": 0.2, "maxTokens": 128},
            },
            "capabilities": {"skills": [], "mcpServers": [], "tools": []},
            "execution": {
                "strategy": "direct",
                "maxSteps": 4,
                "timeoutSeconds": 30,
                "retry": {"maxAttempts": 1, "backoffSeconds": 0},
            },
            "context": {
                "maxInputTokens": 4096,
                "reserveOutputTokens": 512,
                "compaction": {"enabled": True, "thresholdRatio": 0.8},
            },
            "security": {
                "toolPolicy": "deny-by-default",
                "allowedPermissions": ["process:host-user"],
                "network": {
                    "mode": "restricted",
                    "allowedHosts": ["127.0.0.1"],
                    "allowPrivateNetwork": True,
                },
            },
        }
    )


def _create_task(
    page: Page,
    base_url: str,
    *,
    name: str,
    continuity: str,
    session_id: str = "",
) -> dict:
    global_create = page.get_by_role("button", name="新建定时任务", exact=True)
    if global_create.is_visible():
        global_create.click()
    else:
        page.get_by_role("button", name="新建定时任务", exact=True).click()
    form = page.get_by_role("dialog")
    expect(form).to_be_visible()
    if form.get_by_label("交给谁").is_enabled():
        form.get_by_label("交给谁").select_option(AGENT_ID)
    form.get_by_label("任务名称", exact=True).fill(name)
    form.get_by_label("任务说明", exact=True).fill(f"执行 {name}")
    form.get_by_role("button", name="固定间隔", exact=True).click()
    form.get_by_role("spinbutton", name="每隔").fill("60")
    form.get_by_role("combobox", name="对话方式").select_option(continuity)
    if continuity == "continue_session":
        form.get_by_role("combobox", name="选择会话").select_option(session_id)
    form.get_by_role("button", name="创建任务", exact=True).click()
    page.locator(".automation-detail").wait_for()
    page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
    row = page.get_by_role("button", name=f"查看定时任务 {name} 的详情")
    expect(row).to_be_visible()
    task = next(
        item
        for item in _json(base_url, "/api/v1/schedules")["items"]
        if item["displayName"] == name
    )
    row.click()
    return task


def _wait_terminal(base_url: str, task_id: str, *, expected_count: int) -> list[dict]:
    terminal = {"succeeded", "failed", "cancelled", "skipped"}
    items: list[dict] = []
    for _ in range(150):
        items = _json(base_url, f"/api/v1/schedules/{task_id}/occurrences")["items"]
        if len(items) >= expected_count and all(item["state"] in terminal for item in items):
            return items
        time.sleep(0.05)
    events = (
        _json(base_url, f"/api/v1/sessions/{items[0]['sessionId']}/events?limit=500")
        if items
        else {}
    )
    raise AssertionError(
        f"task {task_id} did not produce {expected_count} terminal occurrences: "
        f"{items!r}; events={events!r}"
    )


def _start_browser_sse(page: Page, session_id: str, after_seq: int) -> None:
    page.evaluate(
        """
        ({sessionId, afterSeq}) => {
          window.__schedulerSseResult = (async () => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 8000);
            try {
              const response = await fetch(
                `/api/v1/sessions/${encodeURIComponent(sessionId)}/events/stream?afterSeqId=${afterSeq}`,
                { signal: controller.signal, headers: { Accept: "text/event-stream" } },
              );
              if (!response.ok || !response.body) throw new Error(`SSE ${response.status}`);
              const reader = response.body.getReader();
              const decoder = new TextDecoder();
              let text = "";
              while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                text += decoder.decode(value, { stream: true });
                if (text.includes('"type":"run.completed"')) {
                  controller.abort();
                  return { contentType: response.headers.get("content-type") || "", text };
                }
              }
              throw new Error("SSE ended without run.completed");
            } finally {
              clearTimeout(timer);
            }
          })();
        }
        """,
        {"sessionId": session_id, "afterSeq": after_seq},
    )


def _assert_harness_vertical(
    page: Page,
    base_url: str,
    model: DeterministicChatCompletionsStub,
) -> None:
    page.goto(f"{base_url}/#/automations", wait_until="networkidle")
    expect(page.get_by_role("heading", name="自动化", exact=True, level=1)).to_be_visible()
    expect(page.get_by_text("本地调度运行中", exact=True)).to_be_visible()

    new_task = _create_task(
        page,
        base_url,
        name="Harness 新会话",
        continuity="new_session",
    )
    page.locator(".automation-detail").get_by_role("button", name="立即运行", exact=True).click()
    new_occurrences = _wait_terminal(base_url, new_task["taskId"], expected_count=1)
    assert new_occurrences[0]["state"] == "succeeded", new_occurrences[0]
    assert new_occurrences[0]["sessionId"].startswith("sched-occ_")
    assert new_occurrences[0]["runId"]
    page.reload(wait_until="networkidle")
    page.get_by_role("button", name="查看定时任务 Harness 新会话 的详情").click()
    page.get_by_role("button", name="删除任务", exact=True).click()
    page.get_by_role("alertdialog", name="删除定时任务「Harness 新会话」？").get_by_role(
        "button", name="删除任务", exact=True
    ).click()
    expect(page.get_by_text("新建任务，安排下一次执行", exact=True)).to_be_visible()

    continue_task = _create_task(
        page,
        base_url,
        name="Harness 继续会话",
        continuity="continue_session",
        session_id=CONTINUE_SESSION_ID,
    )
    page.locator(".automation-detail").get_by_role("button", name="立即运行", exact=True).click()
    first_pair = _wait_terminal(base_url, continue_task["taskId"], expected_count=1)
    assert first_pair[0]["state"] == "succeeded", first_pair[0]
    first_run_id = first_pair[0]["runId"]

    replay = _json(base_url, f"/api/v1/sessions/{CONTINUE_SESSION_ID}/events?limit=500")
    after_seq = replay["page"]["latestSeqId"]
    assert after_seq > 0
    page.reload(wait_until="networkidle")
    page.get_by_role("button", name="查看定时任务 Harness 继续会话 的详情").click()
    _start_browser_sse(page, CONTINUE_SESSION_ID, after_seq)
    page.locator(".automation-detail").get_by_role("button", name="立即运行", exact=True).click()
    continued = _wait_terminal(base_url, continue_task["taskId"], expected_count=2)
    assert [item["state"] for item in continued] == ["succeeded", "succeeded"]
    assert {item["sessionId"] for item in continued} == {CONTINUE_SESSION_ID}
    assert first_run_id != continued[0]["runId"]

    sse = page.evaluate("() => window.__schedulerSseResult")
    assert sse["contentType"].startswith("text/event-stream"), sse
    assert '"type":"run.completed"' in sse["text"], sse

    # New-session plus two turns in one continued Session reached the real
    # production model client. The second continued turn receives durable history.
    requests = model.requests()
    assert len(requests) == 3
    assert all(item.path == "/v1/chat/completions" for item in requests)
    assert all(item.authorization == "Bearer harness-fixture-key" for item in requests)
    assert len(requests[0].payload["messages"]) == 2
    # LiteLLM may serialize an unset optional name as null. Preserve exact
    # message count, order, roles, content and every other wire field.
    messages = [
        {key: value for key, value in message.items() if key != "name" or value is not None}
        for message in requests[2].payload["messages"]
    ]
    assert messages == [
        {
            "role": "system",
            "content": (
                "You are the real built-in Harness scheduler fixture.\n\n"
                "Retain prior scheduled turns in one continued session."
            ),
        },
        {"role": "user", "content": "执行 Harness 继续会话"},
        {"role": "assistant", "content": "scheduled harness result 2"},
        {"role": "user", "content": "执行 Harness 继续会话"},
    ], requests


def _assert_conversation_scheduling(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/#/conversations?agentId={AGENT_ID}", wait_until="domcontentloaded")
    composer = page.get_by_role("textbox", name="发送消息…", exact=True)
    expect(composer).to_be_enabled()
    for text, name in (
        ("每天10点帮我生成昨日工作日报", "每日简报"),
        ("每7分钟帮我检查服务状态", "服务巡检"),
    ):
        composer.fill(text)
        page.get_by_role("button", name="发送消息").click()
        expect(page.get_by_text(f"已创建定时任务「{name}」。", exact=False)).to_be_visible(
            timeout=20000
        )
        expect(page.get_by_role("button", name="停止生成")).to_have_count(0)
    tasks = _json(base_url, "/api/v1/schedules")["items"]
    daily = next(task for task in tasks if task["displayName"] == "每日简报")
    interval = next(task for task in tasks if task["displayName"] == "服务巡检")
    assert daily["schedule"]["expression"] == "0 10 * * *"
    assert interval["schedule"]["everySeconds"] == 420
    page.goto(f"{base_url}/#/automations", wait_until="domcontentloaded")
    card = page.get_by_role("button", name="查看定时任务 每日简报 的详情")
    expect(card).to_be_visible()
    artifacts = os.getenv("KSADK_SCHEDULER_E2E_ARTIFACT_DIR")
    if artifacts:
        Path(artifacts).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(artifacts) / "automation-board.png"), full_page=True)
    card.click()
    page.get_by_role("button", name="编辑", exact=True).click()
    expect(page.get_by_label("执行时间", exact=True)).to_have_value("10:00")
    if artifacts:
        page.screenshot(path=str(Path(artifacts) / "automation-editor.png"), full_page=True)
    page.get_by_role("button", name="取消", exact=True).click()
    page.get_by_role("button", name="立即运行", exact=True).click()
    terminal = _wait_terminal(base_url, daily["taskId"], expected_count=1)[0]
    assert terminal["state"] == "succeeded", terminal
    page.get_by_role("link", name="查看会话与结果").click()
    expect(page.get_by_text("scheduled harness result 6", exact=True)).to_be_visible(timeout=15000)
    assert f"sessionId={terminal['sessionId']}" in page.url
    expect(page.locator(".chat-session-main[aria-current=true]")).to_contain_text(
        "定时任务 · 生成昨日工作日报"
    )
    page.reload(wait_until="networkidle")
    expect(page.get_by_text("scheduled harness result 6", exact=True)).to_be_visible(timeout=15000)
    expect(page.locator(".chat-session-main[aria-current=true]")).to_contain_text(
        "定时任务 · 生成昨日工作日报"
    )
    if artifacts:
        page.screenshot(path=str(Path(artifacts) / "automation-result.png"), full_page=True)


def main() -> None:
    with TemporaryDirectory(prefix="ksadk-scheduler-harness-browser-") as temp_dir:
        workspace = Path(temp_dir)
        environment = _managed_harness_profile(workspace)
        with SchedulingChatStub() as model:
            runtime_environment = {
                **environment,
                "KSADK_AGENT_KERNEL": "1",
                "MODEL_API_KEY": "harness-fixture-key",
            }
            with patch.dict(os.environ, runtime_environment):
                service = StudioService(workspace)
                spec = _agent_spec(endpoint_url=model.endpoint_url)
                service.create_studio_agent(
                    agent_id=AGENT_ID,
                    name=AGENT_NAME,
                    description="Scheduler Harness browser fixture",
                    spec=spec,
                    runtime=spec.runtime,
                )

            asyncio.run(
                service.session_service.create_session(AGENT_ID, "local-user", CONTINUE_SESSION_ID)
            )

            with (
                patch.dict(os.environ, runtime_environment),
                studio_server(workspace, service=service) as base_url,
                sync_playwright() as playwright,
            ):
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 960})
                    page_errors: list[str] = []
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    _assert_harness_vertical(page, base_url, model)
                    _assert_conversation_scheduling(page, base_url)
                    assert page_errors == [], f"Uncaught React page errors: {page_errors}"
                finally:
                    browser.close()


if __name__ == "__main__":
    main()
