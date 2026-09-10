"""Exercise Studio creation and scheduling through the rendered product UI.

Run after building React: PYTHONPATH=. uv run python
tests/studio/e2e/studio_workflow_layout_smoke.py --output /tmp/studio-workflows

Every run uses a disposable workspace and loopback server. Scheduler execution
crosses the real Kernel/Codex adapter; only the external model is replaced by
the deterministic local Responses fixture. No user credentials are consumed.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from openai_codex import CodexConfig
from playwright.sync_api import Page, expect, sync_playwright
from scheduler_browser_e2e import (
    AGENT_ID,
    AGENT_NAME,
    _agent_spec,
    _json,
    _prepare_agent,
    _scheduled_instance_id,
)
from studio_e2e_support import studio_server

from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.runtime import RuntimeExecutor, RuntimeRegistry
from ksadk.studio.service import StudioService
from tests.e2e.codex_app_server_fixture import RealCodexFactory
from tests.e2e.codex_responses_stub import DeterministicResponsesStub


def fixture_runtime_inspector(runtime: object) -> tuple[str, str, str]:
    # Isolate product workflows from a locally installed CLI's version. The
    # scheduler uses RealCodexFactory separately for execution, below.
    version = str(getattr(runtime, "version", None) or "0.147.0")
    return "0.8.2", version, f"codex-cli {version}"


def quick_creation(page: Page, base: str, width: int, record) -> None:
    create_requests: list[str] = []
    page.on(
        "request",
        lambda request: (
            create_requests.append(request.url)
            if request.method == "POST" and request.url.endswith("/api/v1/authoring/quick")
            else None
        ),
    )
    name = f"布局验证助手 {width}"
    slug = f"layout-created-{width}"
    page.goto(base + "/#/create", wait_until="domcontentloaded")
    expect(page.locator("#quickAgentName")).to_be_visible()
    page.get_by_role("button", name="继续", exact=True).click()
    expect(page.locator("#quickPrompt")).to_have_attribute("aria-invalid", "true")
    record("create-required-validation")
    page.locator("#quickAgentName").fill(name)
    page.locator("#quickPrompt").fill("帮助用户整理工作计划，先确认目标，再提供简洁可执行的步骤。")
    page.locator(".secondary-settings > summary").filter(has_text="标识与描述").click()
    page.locator("#agent-slug").fill(slug)
    page.locator("#quickDescription").fill("仅用于隔离浏览器验证的示例助手。")
    page.get_by_role("button", name="保存草稿", exact=True).click()
    saved = page.evaluate(
        "JSON.parse(localStorage.getItem(Object.keys(localStorage).find(key => "
        "key.startsWith('agentkit.studio.agentDraft.v2:'))) || 'null')"
    )
    assert saved["fields"]["name"] == name
    assert saved["fields"]["slug"] == slug
    expect(page.get_by_text("草稿已保存", exact=True)).to_be_visible()
    record("create-save-draft")
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#quickAgentName")).to_have_value(name)
    expect(page.locator("#quickPrompt")).to_have_value(
        "帮助用户整理工作计划，先确认目标，再提供简洁可执行的步骤。"
    )
    expect(page.locator("#quickDescription")).to_have_value("仅用于隔离浏览器验证的示例助手。")
    record("create-restore-saved-draft")
    page.get_by_role("button", name="继续", exact=True).click()
    expect(page.get_by_role("button", name="选择模型", exact=True)).to_be_visible()
    page.get_by_role("button", name="选择模型", exact=True).click()
    option = page.get_by_role("option", name=re.compile("布局测试模型"))
    expect(option).to_be_visible()
    if option.get_by_role("checkbox").get_attribute("aria-checked") != "true":
        option.click()
    page.get_by_role("button", name="仅看已选", exact=True).click()
    expect(option).to_be_visible()
    record("create-select-model")
    page.keyboard.press("Escape")
    page.get_by_role("button", name="继续", exact=True).click()
    expect(page.locator("#composedSystemPrompt")).not_to_have_value("", timeout=10000)
    page.locator("#composedSystemPrompt").fill(
        "你是工作计划助手。请使用中文，以简洁、有序的步骤回答。"
    )
    page.locator("#composedTaskPrompt").fill("确认目标并列出三项行动。")
    record("create-edit-composed-prompt")
    page.get_by_role("button", name="上一步", exact=True).click()
    expect(page.get_by_role("button", name="移除 布局测试模型")).to_be_visible()
    page.get_by_role("button", name="继续", exact=True).click()
    expect(page.locator("#composedTaskPrompt")).to_have_value("确认目标并列出三项行动。")
    page.get_by_role("button", name="继续", exact=True).click()
    expect(page.get_by_role("heading", name="检查并创建", exact=True)).to_be_visible()
    page.wait_for_timeout(300)
    assert not create_requests, (
        "Entering review must not create an Agent before explicit confirmation"
    )
    page.get_by_role("button", name="完整摘要", exact=True).click()
    expect(page.get_by_role("dialog", name="配置摘要")).to_contain_text("布局测试模型")
    record("create-review-summary")
    page.keyboard.press("Escape")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/api/v1/authoring/quick")
        )
    ) as created_response:
        page.get_by_role("button", name="创建 Agent", exact=True).click()
    response = created_response.value
    assert response.status == 201, response.text()
    created = response.json()
    assert len(create_requests) == 1
    created_id = created["metadata"]["id"]
    assert created["metadata"]["labels"]["agentkit.ksyun.com/slug"] == slug
    assert created["spec"]["instructions"]["task"] == "确认目标并列出三项行动。"
    expect(page.locator(".app-shell")).to_have_attribute(
        "data-view", "conversations", timeout=20000
    )
    detail = _json(base, f"/api/v1/agents/{created_id}")
    assert detail["builds"], detail
    assert detail["builds"][0]["status"] == "SUCCEEDED", detail["builds"]
    expect(page.get_by_role("heading", name="有什么可以帮你？", exact=True)).to_be_visible()
    record("create-persist-build-open-chat")
    page.reload(wait_until="domcontentloaded")
    persisted = _json(base, f"/api/v1/agents/{created_id}")["draft"]
    assert persisted["metadata"]["name"] == name
    assert (
        persisted["spec"]["instructions"]["system"]
        == "你是工作计划助手。请使用中文，以简洁、有序的步骤回答。"
    )
    expect(page.get_by_role("heading", name="有什么可以帮你？", exact=True)).to_be_visible()
    record("create-reload-persistence")


def scheduling(page: Page, base: str, width: int, build_id: str, record) -> None:
    name = f"工作计划复盘 {width}"
    edited_name = name + " 已调整"
    page.goto(base + "/#/automations", wait_until="domcontentloaded")
    expect(page.get_by_text("本地调度运行中", exact=True)).to_be_visible()
    expect(page.get_by_text("保持 Studio 运行，任务才会自动执行。", exact=True)).to_be_visible()
    record("scheduler-runtime-status")
    page.get_by_role("button", name="新建定时任务", exact=True).click()
    form = page.get_by_role("dialog")
    expect(form).to_be_visible()
    form.get_by_role("combobox", name="交给谁", exact=True).select_option(AGENT_ID)
    form.get_by_role("textbox", name="任务名称", exact=True).fill(name)
    form.get_by_role("textbox", name="任务说明", exact=True).fill(
        "生成工作计划并列出待跟进事项。"
    )
    form.get_by_role("button", name="仅一次", exact=True).click()
    expect(form.locator('input[type="datetime-local"]')).to_be_visible()
    form.get_by_role("button", name="固定时间", exact=True).click()
    form.get_by_role("combobox", name="重复", exact=True).select_option("custom")
    expect(form.get_by_role("textbox", name=re.compile("Cron 表达式"))).to_be_visible()
    form.get_by_role("button", name="固定间隔", exact=True).click()
    form.get_by_role("spinbutton", name="每隔", exact=True).fill("1")
    form.get_by_role("combobox", name="时间单位", exact=True).select_option("3600")
    form.get_by_role("combobox", name=re.compile("^对话方式")).select_option("continue_session")
    expect(form.get_by_role("combobox", name="选择会话", exact=True)).to_be_visible()
    form.get_by_role("combobox", name=re.compile("^对话方式")).select_option("new_session")
    form.locator("summary").filter(has_text="更多设置").click()
    form.get_by_role("checkbox", name="启用此任务", exact=True).uncheck()
    record("scheduler-create-form-variants")
    form.get_by_role("button", name="创建任务", exact=True).click()
    row = page.get_by_role("button", name=f"查看定时任务 {name} 的详情")
    expect(row).to_be_visible()
    task = next(
        item for item in _json(base, "/api/v1/schedules")["items"] if item["displayName"] == name
    )
    task_id = task["taskId"]
    assert task["enabled"] is False
    assert task["target"]["agentInstanceId"] == _scheduled_instance_id(build_id)
    row.click()
    detail = page.locator(".automation-detail")
    expect(page.get_by_role("dialog").get_by_role("heading", name=name, exact=True)).to_be_visible()
    record("scheduler-created-disabled")
    detail.get_by_role("button", name="编辑", exact=True).click()
    form.get_by_role("textbox", name="任务名称", exact=True).fill(edited_name)
    form.get_by_role("textbox", name="任务说明", exact=True).fill(
        "生成调整后的工作计划，并明确负责人。"
    )
    form.get_by_role("button", name="保存变更", exact=True).click()
    expect(page.get_by_role("button", name=f"查看定时任务 {edited_name} 的详情")).to_be_visible()
    task = next(
        item for item in _json(base, "/api/v1/schedules")["items"] if item["taskId"] == task_id
    )
    assert task["command"]["payload"]["content"] == "生成调整后的工作计划，并明确负责人。"
    page.reload(wait_until="domcontentloaded")
    page.get_by_role("button", name=f"查看定时任务 {edited_name} 的详情").click()
    record("scheduler-edit-reload-persistence")
    detail.get_by_role("button", name="启用", exact=True).click()
    expect(detail.get_by_role("button", name="暂停", exact=True)).to_be_visible()
    detail.get_by_role("button", name="暂停", exact=True).click()
    expect(detail.get_by_role("button", name="启用", exact=True)).to_be_visible()
    expect(detail.get_by_role("button", name="立即运行", exact=True)).to_be_disabled()
    record("scheduler-enable-disable")
    detail.get_by_role("button", name="启用", exact=True).click()
    expect(detail.get_by_role("button", name="暂停", exact=True)).to_be_visible()
    detail.get_by_role("button", name="立即运行", exact=True).click()
    terminal = None
    for _ in range(200):
        values = _json(base, f"/api/v1/schedules/{task_id}/occurrences")["items"]
        if values and values[0]["state"] in {"succeeded", "failed", "cancelled", "skipped"}:
            terminal = values[0]
            break
        time.sleep(0.1)
    assert terminal and terminal["state"] == "succeeded", terminal
    expect(detail.locator(".automation-occurrences")).to_contain_text("成功", timeout=10000)
    record("scheduler-run-real-kernel-terminal")
    detail.locator("summary").filter(has_text="执行详情").click()
    expect(detail.locator(".automation-occurrence-facts")).to_contain_text(terminal["runId"])
    expect(detail.locator(".automation-timeline")).to_contain_text("成功")
    record("scheduler-expand-execution-evidence")
    detail.locator("summary").filter(has_text="执行详情").click()
    page.keyboard.press("Escape")
    page.get_by_role("tab", name="执行记录", exact=True).click()
    expect(page.locator(".automation-history")).to_contain_text("成功")
    record("scheduler-global-history")
    page.goto(base + f"/#/agents/{AGENT_ID}", wait_until="domcontentloaded")
    expect(page.get_by_role("banner", name="当前页面")).to_contain_text(AGENT_NAME)
    page.get_by_role("tab", name="自动化", exact=True).click()
    page.get_by_role("button", name=f"查看定时任务 {edited_name} 的详情").click()
    expect(detail.locator(".automation-occurrences")).to_contain_text("成功")
    record("scheduler-embedded-history")
    detail.get_by_role("button", name="删除任务", exact=True).click()
    dialog = page.get_by_role("alertdialog")
    expect(dialog).to_be_visible()
    record("scheduler-delete-confirm")
    dialog.get_by_role("button", name="取消", exact=True).click()
    expect(detail).to_be_visible()
    detail.get_by_role("button", name="删除任务", exact=True).click()
    page.get_by_role("alertdialog").get_by_role("button", name="删除任务", exact=True).click()
    page.get_by_role("button", name="列表视图", exact=True).click()
    expect(page.get_by_text("还没有定时任务", exact=True)).to_be_visible()
    assert all(item["taskId"] != task_id for item in _json(base, "/api/v1/schedules")["items"])
    retained = _json(base, f"/api/v1/schedules/{task_id}/occurrences")["items"]
    assert retained[0]["state"] == "succeeded"
    record("scheduler-delete-retains-history")
    # The embedded empty state must retain a creation entry point.
    page.get_by_role("tab", name="执行记录", exact=True).click()
    page.get_by_role("button", name="新建定时任务", exact=True).click()
    expect(form).to_be_visible()
    form.get_by_role("button", name="取消", exact=True).click()
    expect(page.get_by_role("tab", name="执行记录", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    record("scheduler-embedded-create-cancel")


def run(output: Path, workflow: str = "all", only_width: int | None = None) -> None:
    output.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    with (
        TemporaryDirectory(prefix="studio-workflow-layout-") as temporary,
        DeterministicResponsesStub() as responses,
    ):
        workspace = Path(temporary)
        client_factory = RealCodexFactory(responses_url=responses.base_url)
        config = CodexConfig(env={"CODEX_HOME": str(workspace / "codex-home")})
        registry = RuntimeRegistry()
        registry.register("codex", lambda _context: CodexRuntimeAdapter(client_factory(config)))
        service = StudioService(
            workspace,
            codex_runtime_inspector=fixture_runtime_inspector,
            runtime_executor=RuntimeExecutor(registry),
        )
        build_id = _prepare_agent(service)
        service.catalog.create_model_profile(
            name="layout-fixture",
            display_name="布局测试模型",
            version="1.0.0",
            description="隔离浏览器验证",
            spec=_agent_spec().model,
        )
        with studio_server(workspace, service=service) as base, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                variants = [
                    (width, theme)
                    for width, theme in [(1440, "light"), (390, "dark")]
                    if only_width is None or width == only_width
                ]
                for width, theme in variants:
                    context = browser.new_context(
                        viewport={"width": width, "height": 960}, color_scheme=theme
                    )
                    context.add_init_script(
                        f"localStorage.setItem('agentkit-studio-theme', '{theme}');"
                    )
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def record(scenario: str) -> None:
                        expect(page.locator("html")).to_have_attribute("data-theme", theme)
                        page.evaluate("""() => Promise.all(document.getAnimations()
                            .filter(animation =>
                              Number(animation.effect?.getComputedTiming().endTime) <= 600)
                            .map(animation => animation.finished.catch(() => {})))""")
                        dimensions = page.evaluate(
                            "({width: innerWidth, "
                            "contentWidth: document.documentElement.scrollWidth})"
                        )
                        assert dimensions["contentWidth"] <= dimensions["width"] + 1, (
                            scenario,
                            dimensions,
                        )
                        filename = f"{width}-{theme}-{scenario}.png"
                        page.screenshot(path=str(output / filename), full_page=True)
                        results.append(
                            {
                                "width": width,
                                "theme": theme,
                                "scenario": scenario,
                                "status": "passed",
                                "screenshot": filename,
                            }
                        )
                        (output / "results.json").write_text(
                            json.dumps(results, ensure_ascii=False, indent=2)
                        )
                        print(f"PASS {width} {theme} {scenario}", flush=True)

                    try:
                        if workflow in {"all", "create"}:
                            quick_creation(page, base, width, record)
                        if workflow in {"all", "scheduler"}:
                            scheduling(page, base, width, build_id, record)
                        assert not errors, errors
                    except Exception:
                        page.screenshot(
                            path=str(output / f"{width}-{theme}-failure.png"), full_page=True
                        )
                        (output / f"{width}-{theme}-failure.txt").write_text(
                            page.locator("body").aria_snapshot()
                        )
                        raise
                    finally:
                        context.close()
            finally:
                browser.close()
        assert len(responses.requests()) == (0 if workflow == "create" else len(variants))
        assert all(process.poll() is not None for process in client_factory.processes)
    print(f"Studio workflow layout smoke: {len(results)} states passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/tmp/studio-workflow-layout"))
    parser.add_argument("--workflow", choices=["all", "create", "scheduler"], default="all")
    parser.add_argument("--width", type=int, choices=[1440, 390])
    args = parser.parse_args()
    run(args.output, args.workflow, args.width)
