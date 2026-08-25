"""Phase 3 Capability Runtime 测试（plan §17 验收项）。"""

from __future__ import annotations

import asyncio

import pytest

from ksadk.harness.capabilities import CapabilityDescriptor, RiskLevel
from ksadk.harness.mcp_runtime import (
    McpCapabilityRuntime,
    McpRuntimeOptions,
    McpServerBinding,
)
from ksadk.harness.skill_runtime import (
    SkillDisclosureError,
    SkillManifest,
    SkillRuntime,
)
from ksadk.harness.tool_policy import ToolCallContext, ToolPolicy
from ksadk.harness.tool_receipts import ToolReceipt, ToolReceiptStore


def _descriptor(cap_id: str = "mcp://budget@1.0.0", **kwargs) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=cap_id,
        kind="mcp",
        name="budget",
        description="预算查询 MCP",
        version="1.0.0",
        **kwargs,
    )


class _FakeTransport:
    def __init__(self, *, fail: bool = False, tools: list | None = None) -> None:
        self.fail = fail
        self.tools = tools if tools is not None else [
            {"name": "query_budget", "inputSchema": {"type": "object"}},
            {"name": "broken_tool"},  # 缺 inputSchema → 隔离
        ]

    async def list_tools(self) -> list[dict]:
        if self.fail:
            raise ConnectionError("server down")
        return self.tools

    async def call_tool(self, name: str, arguments: dict) -> str:
        if self.fail:
            raise ConnectionError("server down")
        return f"result:{name}"


def _runtime(**options) -> McpCapabilityRuntime:
    return McpCapabilityRuntime(
        options=McpRuntimeOptions(
            health_ttl_seconds=30.0, failure_threshold=3, cooldown_seconds=60.0, **options
        )
    )


class TestMcpHealthCache:
    def test_health_cached_within_ttl(self):
        runtime = _runtime()
        transport = _FakeTransport()
        runtime.bind(McpServerBinding(descriptor=_descriptor(), transport=transport))
        first = asyncio.run(runtime.health("mcp://budget@1.0.0", now=100.0))
        second = asyncio.run(runtime.health("mcp://budget@1.0.0", now=110.0))
        assert first.healthy and second.healthy
        assert not first.cached and second.cached

    def test_probe_failure_degrades_optional_in_draft(self):
        runtime = _runtime()
        runtime.bind(McpServerBinding(
            descriptor=_descriptor(), transport=_FakeTransport(fail=True), required=False
        ))
        report = asyncio.run(runtime.health("mcp://budget@1.0.0", now=100.0))
        assert not report.healthy and report.degraded
        assert runtime.degradation_decision(report, environment="draft") == "warn_and_continue"
        assert runtime.degradation_decision(report, environment="revision") == "degrade"

    def test_required_unavailable_blocks_revision(self):
        """必需 MCP 不可用：Revision 环境阻止（§10.3）。"""
        runtime = _runtime()
        runtime.bind(McpServerBinding(
            descriptor=_descriptor(), transport=_FakeTransport(fail=True), required=True
        ))
        report = asyncio.run(runtime.health("mcp://budget@1.0.0", now=100.0))
        assert not report.degraded  # 必需能力不"降级"
        assert runtime.degradation_decision(report, environment="revision") == "block"


class TestMcpCircuitBreaker:
    def test_opens_after_threshold_and_cools_down(self):
        runtime = _runtime()
        runtime.bind(McpServerBinding(
            descriptor=_descriptor(), transport=_FakeTransport(fail=True)
        ))
        # 3 次失败触发熔断。
        for now in (100.0, 101.0, 102.0):
            asyncio.run(runtime.health("mcp://budget@1.0.0", now=now))
        report = asyncio.run(runtime.health("mcp://budget@1.0.0", now=103.0))
        assert report.circuit_open
        # 冷却期内继续 open。
        report = asyncio.run(runtime.health("mcp://budget@1.0.0", now=150.0))
        assert report.circuit_open
        # 冷却期后（>60s）允许试探。

    def test_call_opens_circuit_after_failures(self):
        runtime = _runtime()
        runtime.bind(McpServerBinding(
            descriptor=_descriptor(), transport=_FakeTransport(fail=True)
        ))
        for _ in range(3):
            with pytest.raises(Exception, match="mcp call failed"):
                asyncio.run(runtime.call("mcp://budget@1.0.0", "query_budget", {}))
        with pytest.raises(Exception, match="circuit open"):
            asyncio.run(runtime.call("mcp://budget@1.0.0", "query_budget", {}))


class TestMcpToolSchemaValidation:
    def test_invalid_tool_isolated(self):
        """单个 Tool Schema 无效 → 隔离该 Tool，不拖垮整站（§10.3）。"""
        runtime = _runtime()
        runtime.bind(McpServerBinding(descriptor=_descriptor(), transport=_FakeTransport()))
        tools = asyncio.run(runtime.tools("mcp://budget@1.0.0"))
        assert [t["name"] for t in tools] == ["query_budget"]


class TestToolPolicy:
    def _ctx(self, **kwargs) -> ToolCallContext:
        base = dict(
            tenant_id="t1", user_id="u1", agent_id="a1", tool_name="transfer_money"
        )
        base.update(kwargs)
        return ToolCallContext(**base)

    def test_low_risk_readonly_allowed(self):
        decision = ToolPolicy().decide(self._ctx(risk_level=RiskLevel.LOW))
        assert decision.action == "allow"

    def test_high_risk_requires_approval(self):
        decision = ToolPolicy().decide(self._ctx(risk_level=RiskLevel.HIGH))
        assert decision.action == "require_approval"

    def test_external_side_effects_require_approval(self):
        decision = ToolPolicy().decide(
            self._ctx(risk_level=RiskLevel.LOW, has_external_side_effects=True)
        )
        assert decision.action == "require_approval"

    def test_sensitive_data_requires_approval(self):
        decision = ToolPolicy().decide(
            self._ctx(risk_level=RiskLevel.LOW, data_sensitivity="restricted")
        )
        assert decision.action == "require_approval"

    def test_denied_prefix_denies(self):
        policy = ToolPolicy(denied_prefixes=("drop_",))
        decision = policy.decide(self._ctx(tool_name="drop_database"))
        assert decision.action == "deny"

    def test_network_destination_outside_allowlist_denies(self):
        policy = ToolPolicy(allowed_destinations=("api.internal",))
        decision = policy.decide(
            self._ctx(tool_name="http_get", network_destination="evil.example.com")
        )
        assert decision.action == "deny"

    def test_prior_receipt_skips_reapproval(self):
        """有历史 Receipt 的调用不再重复审批（幂等）。"""
        decision = ToolPolicy().decide(
            self._ctx(risk_level=RiskLevel.HIGH, prior_receipt_id="tr-1")
        )
        assert decision.action == "allow"


class TestToolReceipts:
    def test_execute_once_idempotency(self):
        """审批后仅执行一次：重复 record 返回既有 Receipt。"""
        store = ToolReceiptStore()
        receipt = ToolReceipt(
            invocation_id="r1", call_id="tc-1", tool_name="transfer_money",
            arguments_digest="sha256:abc", decision="approved", status="executed",
            result_digest="sha256:def",
        )
        assert store.record(receipt) is None
        duplicate = ToolReceipt(
            invocation_id="r1", call_id="tc-1", tool_name="transfer_money",
            arguments_digest="sha256:abc", decision="approved", status="skipped",
        )
        existing = store.record(duplicate)
        assert existing is not None and existing.status == "executed"

    def test_get_missing_returns_none(self):
        assert ToolReceiptStore().get("r1", "tc-1") is None


class _SkillSource:
    def manifest(self, skill_id: str) -> SkillManifest:
        return SkillManifest(name="analysis", summary="财务分析技能")

    def full_text(self, skill_id: str) -> str:
        return "# SKILL.md\n完整正文"

    def resource(self, skill_id: str, resource_ref: str) -> bytes:
        return b"resource-bytes"


class TestSkillProgressiveDisclosure:
    def test_levels_disclosed_in_order(self):
        runtime = SkillRuntime(_SkillSource())
        assert runtime.level0("skill://analysis@1") == "财务分析技能"
        manifest = runtime.level1("run-1", "skill://analysis@1")
        assert manifest.name == "analysis"
        assert "SKILL.md" in runtime.level2("run-1", "skill://analysis@1")
        assert runtime.level3("run-1", "skill://analysis@1", "scripts/x.py") == b"resource-bytes"
        assert runtime.level("run-1", "skill://analysis@1") == 2

    def test_level3_without_level2_rejected(self):
        runtime = SkillRuntime(_SkillSource())
        with pytest.raises(SkillDisclosureError, match="Level 2"):
            runtime.level3("run-1", "skill://analysis@1", "scripts/x.py")

    def test_level2_without_level1_rejected(self):
        runtime = SkillRuntime(_SkillSource())
        with pytest.raises(SkillDisclosureError, match="Level 1"):
            runtime.level2("run-1", "skill://analysis@1")

    def test_disclosure_isolated_per_run(self):
        runtime = SkillRuntime(_SkillSource())
        runtime.level1("run-1", "skill://analysis@1")
        with pytest.raises(SkillDisclosureError):
            runtime.level2("run-2", "skill://analysis@1")


class TestSandboxBackend:
    def test_readonly_backend_executes_whitelisted_command(self, tmp_path):
        from ksadk.harness.sandbox_backend import (
            ExecuteRequest,
            LocalReadOnlySandboxBackend,
            SandboxSpec,
        )

        (tmp_path / "data.txt").write_text("hello", encoding="utf-8")
        backend = LocalReadOnlySandboxBackend()
        handle = asyncio.run(backend.create(SandboxSpec(workspace_root=str(tmp_path))))
        result = asyncio.run(
            backend.execute(handle, ExecuteRequest(command=f"cat {tmp_path}/data.txt"))
        )
        assert result.ok and "hello" in result.output

    def test_readonly_backend_rejects_write_and_network(self, tmp_path):
        from ksadk.harness.sandbox_backend import (
            LocalReadOnlySandboxBackend,
            SandboxPolicyViolation,
            SandboxSpec,
        )

        backend = LocalReadOnlySandboxBackend()
        with pytest.raises(SandboxPolicyViolation, match="只允许只读"):
            asyncio.run(backend.create(SandboxSpec(workspace_root=str(tmp_path), read_only=False)))
        with pytest.raises(SandboxPolicyViolation, match="网络"):
            asyncio.run(backend.create(
                SandboxSpec(workspace_root=str(tmp_path), network_egress=("api.x",))
            ))

    def test_readonly_backend_denies_write_command(self, tmp_path):
        """写入策略可验证：非白名单命令被拒。"""
        from ksadk.harness.sandbox_backend import (
            ExecuteRequest,
            LocalReadOnlySandboxBackend,
            SandboxSpec,
        )

        backend = LocalReadOnlySandboxBackend()
        handle = asyncio.run(backend.create(SandboxSpec(workspace_root=str(tmp_path))))
        result = asyncio.run(
            backend.execute(handle, ExecuteRequest(command="rm -rf /"))
        )
        assert not result.ok and result.error

    def test_collect_artifacts_and_close(self, tmp_path):
        from ksadk.harness.sandbox_backend import (
            LocalReadOnlySandboxBackend,
            SandboxSpec,
        )

        backend = LocalReadOnlySandboxBackend()
        handle = asyncio.run(backend.create(SandboxSpec(workspace_root=str(tmp_path))))
        assert asyncio.run(backend.collect_artifacts(handle)) == []
        asyncio.run(backend.close(handle))
