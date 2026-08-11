"""ContextContributor 单测（方案 §8.7 / §17.2）。"""

from __future__ import annotations

import asyncio

import pytest

from ksadk.context_engine.contributors import (
    ContextContributionRequest,
    ContextContributor,
    ContributorCapabilities,
    SkillManifestContributor,
    run_contributors,
)
from ksadk.context_engine.hosted_pipeline import default_hosted_contributors
from ksadk.context_engine.models import ContextItem


def _request():
    return ContextContributionRequest(user_input="hello", session_id="s", invocation_id="i")


class _OkContributor(ContextContributor):
    def __init__(self, items):
        self._items = items
        self.capabilities = ContributorCapabilities(
            contributor_id="ok",
            trust_level="untrusted",
            max_tokens=10000,
            timeout_ms=1000,
            cacheability="turn",
            failure_mode="skip",
        )

    async def contribute(self, request):
        return self._items


class _SlowContributor(ContextContributor):
    def __init__(self):
        self.capabilities = ContributorCapabilities(
            contributor_id="slow",
            trust_level="untrusted",
            max_tokens=10000,
            timeout_ms=10,
            cacheability="turn",
            failure_mode="skip",
        )

    async def contribute(self, request):
        await asyncio.sleep(0.5)
        return []


class _FailingContributor(ContextContributor):
    def __init__(self, fail_mode="skip"):
        self.capabilities = ContributorCapabilities(
            contributor_id="fail",
            trust_level="untrusted",
            max_tokens=10000,
            timeout_ms=1000,
            cacheability="turn",
            failure_mode=fail_mode,
        )

    async def contribute(self, request):
        raise RuntimeError("boom")


def _item(iid, tokens):
    return ContextItem(
        item_id=iid,
        kind="recalled_memory",
        content=iid,
        source="t",
        trust_level="untrusted",
        priority=0,
        estimated_tokens=tokens,
    )


def test_contributors_run_concurrent():
    items = [_item("a", 5), _item("b", 7)]
    res = asyncio.run(run_contributors([_OkContributor(items)], _request()))
    assert len(res.items) == 2
    assert res.status["ok"] == "ok"


def test_contributor_timeout_returns_empty():
    res = asyncio.run(run_contributors([_SlowContributor()], _request()))
    assert res.items == []
    assert res.status["slow"] == "timeout"
    assert any("slow" in w for w in res.warnings)


def test_contributor_failure_skip_returns_empty():
    res = asyncio.run(run_contributors([_FailingContributor("skip")], _request()))
    assert res.items == []
    assert res.status["fail"] == "error"


def test_contributor_failure_fail_propagates():
    with pytest.raises(RuntimeError):
        asyncio.run(run_contributors([_FailingContributor("fail")], _request()))


def test_skill_manifest_contributor_emits_resource_manifest():
    c = SkillManifestContributor(
        [{"name": "deploy", "description": "deploy skill", "version": "1.0"}]
    )
    res = asyncio.run(run_contributors([c], _request()))
    assert len(res.items) == 1
    assert res.items[0].kind == "resource_manifest"
    assert res.items[0].trust_level == "resource"


def test_contributors_trust_level_never_platform():
    # 外部 Contributor 注册必须 untrusted/resource，不能产生 platform_safety（方案 §19）
    items = [_item("x", 5)]
    res = asyncio.run(run_contributors([_OkContributor(items)], _request()))
    for it in res.items:
        assert it.trust_level != "platform"
        assert not it.required  # Contributor 不得自行声明 required


def test_default_hosted_contributors_include_memory_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_MEMORY_ENABLED", "true")
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(tmp_path / "memory.db"))

    contributors = default_hosted_contributors(user_id="user", agent_id="agent")

    assert any(item.id() == "memory_recall" for item in contributors)


def test_agent_memory_off_overrides_enabled_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_MEMORY_ENABLED", "true")
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(tmp_path / "memory.db"))

    contributors = default_hosted_contributors(
        user_id="user",
        agent_id="agent",
        memory_recall_enabled=False,
    )

    assert all(item.id() != "memory_recall" for item in contributors)
