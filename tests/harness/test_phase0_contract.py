"""Phase 0 冻结契约单测：HarnessSpec / HarnessState / thread_ids / CapabilityRegistry。"""

from __future__ import annotations

import pytest

from ksadk.harness.capabilities import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityRegistry,
    CapabilityRegistryError,
)
from ksadk.harness.engine import (
    ThreadIdError,
    decode_thread_id,
    encode_thread_id,
    matches_tenant,
    single_agent_plan,
    validate_tenant,
)
from ksadk.harness.spec import (
    BudgetSource,
    CapabilityBinding,
    CapabilityBindings,
    ContextPolicy,
    HarnessSpec,
    MemoryPolicy,
    ModelBinding,
    PromptSpec,
    SandboxPolicy,
    build_manifest,
)
from ksadk.harness.state import (
    HarnessState,
    Message,
    MessageRole,
    RunStatus,
    ToolCall,
)


def _spec(**overrides) -> HarnessSpec:
    base = dict(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
        prompt=PromptSpec(instructions="你是一个财务分析助手。"),
    )
    base.update(overrides)
    return HarnessSpec(**base)


# ---------------------------------------------------------------- HarnessSpec


class TestHarnessSpec:
    def test_minimal_spec_valid_and_hashable(self):
        spec = _spec()
        assert spec.schema_version == "harness.ksadk.io/v1"
        h1, h2 = spec.content_hash(), _spec().content_hash()
        assert h1 == h2 and h1.startswith("sha256:")

    def test_hash_changes_with_prompt(self):
        assert _spec().content_hash() != _spec(
            prompt=PromptSpec(instructions="别的指令。")
        ).content_hash()

    def test_frozen(self):
        spec = _spec()
        with pytest.raises(Exception):
            spec.model = ModelBinding(profile_ref="model-profile://x@1")  # type: ignore[misc]

    def test_extra_field_forbidden(self):
        with pytest.raises(Exception):
            _spec(unknown_field=1)  # type: ignore[arg-type]

    def test_floating_model_ref_rejected(self):
        with pytest.raises(Exception, match="版本"):
            ModelBinding(profile_ref="model-profile://kimi-k3")

    def test_prompt_exactly_one_of_inline_or_ref(self):
        with pytest.raises(Exception, match="二选一"):
            PromptSpec()
        with pytest.raises(Exception, match="二选一"):
            PromptSpec(instructions="x", instructions_ref="skill://a@1")

    def test_memory_policy_scope_allowlist(self):
        with pytest.raises(Exception, match="scope"):
            MemoryPolicy(scopes=("galaxy",))

    def test_context_policy_thresholds(self):
        with pytest.raises(Exception):
            ContextPolicy(proactive_compaction_threshold=1.5)

    def test_manifest_shape(self):
        spec = _spec(
            capabilities=CapabilityBindings(
                mcp_bindings=(CapabilityBinding(capability_ref="mcp-binding://budget@1.2.0"),),
                skill_bindings=(CapabilityBinding(capability_ref="skill://analysis@0.3.1", required=False),),
            )
        )
        manifest = build_manifest(spec)
        assert manifest["engine"] == "managed-langgraph"
        assert manifest["modelProfileRef"] == "model-profile://kimi-k3@1.0.0"
        assert manifest["mcpRefs"] == ["mcp-binding://budget@1.2.0"]
        assert manifest["skillRefs"] == ["skill://analysis@0.3.1"]
        assert manifest["contentHash"] == spec.content_hash()
        assert "apiKey" not in str(manifest)

    def test_sandbox_default_read_only(self):
        assert SandboxPolicy().read_only is True


# -------------------------------------------------------------- HarnessState


class TestHarnessState:
    def test_defaults(self):
        state = HarnessState(
            tenant_id="t1", user_id="u1", agent_id="a1", session_id="s1"
        )
        assert state.status == RunStatus.PENDING
        assert state.run_id.startswith("hr-")
        assert state.checkpoint_key() == f"t1/a1/s1/{state.run_id}"

    def test_tool_call_result_externalized(self):
        call = ToolCall(
            call_id="tc-1",
            name="budget_lookup",
            result_ref="artifact://big-result@1",
            result_preview="预算 42000 元…",
        )
        assert call.status == "pending"
        assert len(call.result_preview) <= 2048

    def test_message_roles(self):
        state = HarnessState(
            tenant_id="t1", user_id="u1", agent_id="a1", session_id="s1",
            messages=[Message(role=MessageRole.USER, content="为什么超预算？")],
        )
        assert state.messages[0].role is MessageRole.USER


# ---------------------------------------------------------------- thread_ids


class TestThreadIds:
    def test_roundtrip(self):
        tid = encode_thread_id(
            tenant_id="corp", user_id="u1", agent_id="ar-1", session_id="s-1", run_id="r-9"
        )
        assert tid == "tenant:corp/user:u1/agent:ar-1/session:s-1/run:r-9"
        decoded = decode_thread_id(tid)
        assert (decoded.tenant_id, decoded.run_id) == ("corp", "r-9")

    def test_slash_injected_rejected(self):
        with pytest.raises(ThreadIdError):
            encode_thread_id(
                tenant_id="corp", user_id="u/1", agent_id="a", session_id="s", run_id="r"
            )

    def test_validate_tenant(self):
        tid = encode_thread_id(
            tenant_id="corp", user_id="u", agent_id="a", session_id="s", run_id="r"
        )
        validate_tenant(tid, tenant_id="corp")
        with pytest.raises(ThreadIdError, match="租户不匹配"):
            validate_tenant(tid, tenant_id="other")

    def test_matches_tenant_lenient(self):
        assert matches_tenant("garbage", tenant_id="corp") is False

    def test_tenant_prefix(self):
        tid = decode_thread_id("tenant:corp/user:u/agent:a/session:s/run:r")
        assert tid.tenant_prefix() == "tenant:corp/"


# -------------------------------------------------------- CapabilityRegistry


def _cap(cap_id: str, *, deps: tuple[str, ...] = ()) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=cap_id, kind=CapabilityKind.MCP, name=cap_id, version="1.0.0", dependencies=deps
    )


class TestCapabilityRegistry:
    def test_register_and_resolve_order(self):
        registry = CapabilityRegistry()
        registry.register(_cap("mcp://base@1"))
        registry.register(_cap("mcp://derived@1", deps=("mcp://base@1",)))
        order = registry.resolve_order(["mcp://derived@1", "mcp://base@1"])
        assert [c.id for c in order] == ["mcp://base@1", "mcp://derived@1"]

    def test_duplicate_rejected(self):
        registry = CapabilityRegistry()
        registry.register(_cap("mcp://base@1"))
        with pytest.raises(CapabilityRegistryError, match="重复"):
            registry.register(_cap("mcp://base@1"))

    def test_missing_dependency_rejected(self):
        registry = CapabilityRegistry()
        with pytest.raises(CapabilityRegistryError, match="依赖未注册"):
            registry.register(_cap("mcp://x@1", deps=("mcp://ghost@1",)))

    def test_cycle_impossible_by_registration_order(self):
        """依赖必须先注册，环在注册阶段即被拦截——环不可能进入注册表。"""
        registry = CapabilityRegistry()
        with pytest.raises(CapabilityRegistryError, match="依赖未注册"):
            registry.register(_cap("mcp://a@1", deps=("mcp://b@1",)))


# ----------------------------------------------------------- ExecutionPlan


class TestExecutionPlan:
    def test_single_agent_plan_topology(self):
        plan = single_agent_plan()
        assert plan.strategy_kind == "single-agent"
        assert "reason" in plan.nodes and "final" in plan.nodes
        assert ("reason", "tool_calls") in plan.edges
