"""Real-browser acceptance for the lazy Studio Channel workspace contribution.

Run with ``KSADK_CHANNEL_UI_E2E=1``.  The test creates a temporary workspace
and DSH home and never reads the user's configured Studio workspace.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright
from studio_e2e_support import studio_server

from ksadk.plugins.dsh_toolchain import DshToolchainManager
from ksadk.studio.dsh_provider_registration import (
    StudioDshProviderRegistrationManager,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("KSADK_CHANNEL_UI_E2E") != "1",
    reason="set KSADK_CHANNEL_UI_E2E=1 for isolated real-browser Channel acceptance",
)


def _prepare_profile(workspace: Path) -> None:
    toolchain = DshToolchainManager(base_dir=workspace / ".toolchains")
    toolchain.install()
    os.environ["AGENTENGINE_PLUGIN_TOOLCHAIN_HOME"] = str(workspace / ".toolchains")
    registration = StudioDshProviderRegistrationManager.discover_or_create_workspace_default(
        workspace
    )
    assert registration is not None
    asyncio.run(registration.bootstrap_official_codex_provider())
    asyncio.run(registration.aclose())


def _assert_channel_page(page: Page) -> None:
    expect(page.get_by_role("heading", name="消息渠道")).to_be_visible(timeout=45_000)
    expect(page.get_by_label("服务地址")).to_be_visible(timeout=45_000)
    expect(page.get_by_text("尚未配置")).to_be_visible()
    expect(page.get_by_role("button", name="连接 Agent")).to_be_disabled()
    expect(page.get_by_text("飞书示例")).to_have_count(0)


def test_lazy_shell_discovers_and_mounts_channel_page(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KSADK_DSH_HOME", raising=False)
    monkeypatch.delenv("KSADK_DSH_PROFILE", raising=False)
    monkeypatch.setenv("KSADK_STUDIO_LAZY_START", "1")
    monkeypatch.setenv("KSADK_STUDIO_TEAMS_DEFAULT", "1")
    _prepare_profile(tmp_path)
    with studio_server(tmp_path) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            opened = time.monotonic()
            page.goto(base_url + "/", wait_until="domcontentloaded")
            expect(page.get_by_role("navigation", name="产品导航")).to_be_visible(timeout=10_000)
            print(f"STUDIO_SHELL_VISIBLE_SECONDS={time.monotonic() - opened:.2f}")
            expect(page.get_by_role("button", name="消息渠道", exact=True)).to_be_visible(
                timeout=60_000
            )
            print(f"CHANNEL_NAV_VISIBLE_SECONDS={time.monotonic() - opened:.2f}")
            page.get_by_role("button", name="消息渠道", exact=True).click()
            _assert_channel_page(page)
            print("CHANNEL_PAGE_MOUNTED_WITHOUT_RETRY")
            browser.close()
