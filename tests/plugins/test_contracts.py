"""P2-00A plugin composition contract and deterministic lock tests."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

import ksadk.plugins as plugin_api
from ksadk.plugins.context_contributor import (
    ContextContributorExchange,
    context_contributor_json_schema,
)
from ksadk.plugins.contracts import (
    CapabilityDefinition,
    CompositionProfile,
    PluginInventory,
    PluginLock,
    canonical_plugin_lock,
    plugin_lock_digest,
)
from ksadk.plugins.providers.dsh import (
    DSH_AGENT_PROVIDER_HOST_METHODS,
    DSH_AGENT_PROVIDER_HOST_PROTOCOL,
    DshAgentProviderDescriptor,
    DshAgentProviderInventory,
    DshAgentProviderPreflight,
)
from ksadk.plugins.subagents import (
    ChildHandle,
    SpawnSubagentRequest,
    SubagentEvent,
    SubagentResult,
    SubagentStatus,
)
from ksadk.studio.capabilities import compute_bundle_digest
from ksadk.studio.contracts import BundleManifest

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "plugin" / "v1"
CASES = (
    ("capability-definition.schema.json", "capability-definition.json", CapabilityDefinition),
    ("composition-profile.schema.json", "composition-profile.json", CompositionProfile),
    ("plugin-lock.schema.json", "plugin-lock.json", PluginLock),
    ("plugin-inventory.schema.json", "plugin-inventory.json", PluginInventory),
)


def test_unpublished_plugin_package_format_has_no_public_contract_or_api() -> None:
    """The Python admission record must not become a third plugin ecosystem."""

    forbidden = {
        CONTRACT_DIR / "plugin-manifest.schema.json",
        CONTRACT_DIR / "fixtures" / "plugin-manifest.json",
    }
    assert not any(path.exists() for path in forbidden)
    assert "PluginManifest" not in plugin_api.__all__
    assert not hasattr(plugin_api, "PluginManifest")


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(schema_name: str) -> Draft202012Validator:
    schema = _json(CONTRACT_DIR / schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _assert_invalid(validator: Draft202012Validator, payload: dict[str, Any]) -> None:
    assert list(validator.iter_errors(payload))


@pytest.mark.parametrize(("schema_name", "fixture_name", "model"), CASES)
def test_plugin_contract_fixtures_match_schema_and_source_type(
    schema_name: str, fixture_name: str, model: type,
) -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / fixture_name)
    _validator(schema_name).validate(fixture)
    parsed = model.model_validate(fixture)
    assert parsed.model_dump(by_alias=True, exclude_none=True, mode="json")


def test_bundle_schema_and_python_enforce_the_same_composition_mode_rules() -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / "agent-bundle-manifest-v2.json")
    validator = _validator("agent-bundle-manifest-v2.schema.json")
    validator.validate(fixture)
    BundleManifest.model_validate(fixture)

    composed_without_profile = deepcopy(fixture)
    composed_without_profile["compositionMode"] = "composed"
    composed_without_profile.pop("compositionProfileDigest", None)
    _assert_invalid(validator, composed_without_profile)
    with pytest.raises(ValidationError, match="compositionProfileDigest"):
        BundleManifest.model_validate(composed_without_profile)

    legacy_with_profile = deepcopy(fixture)
    legacy_with_profile["compositionMode"] = "legacy"
    legacy_with_profile["compositionProfileDigest"] = "sha256:" + "1" * 64
    _assert_invalid(validator, legacy_with_profile)
    with pytest.raises(ValidationError, match="compositionProfileDigest"):
        BundleManifest.model_validate(legacy_with_profile)


def test_plugin_contract_manifest_locks_every_schema_and_fixture() -> None:
    manifest = _json(CONTRACT_DIR / "manifest.json")
    assert manifest["contract_set"] == "plugin/v1"
    entries = {entry["path"]: entry for entry in manifest["files"]}
    aggregate = hashlib.sha256()
    expected_paths: set[str] = set()
    for path in sorted(CONTRACT_DIR.rglob("*.json")):
        if path.name == "manifest.json":
            continue
        payload = _json(path)
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        relative = path.relative_to(CONTRACT_DIR).as_posix()
        expected_paths.add(relative)
        assert entries[relative]["sha256"] == hashlib.sha256(canonical).hexdigest()
        assert entries[relative]["bytes"] == len(canonical)
        aggregate.update(relative.encode("utf-8") + b"\0" + canonical)
    assert set(entries) == expected_paths
    assert manifest["aggregate_digest"] == aggregate.hexdigest()


def test_plugin_lock_is_sorted_and_input_order_independent() -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / "plugin-lock.json")
    ordered = PluginLock.model_validate(fixture)
    reversed_input = PluginLock.model_validate(
        {**fixture, "plugins": list(reversed(fixture["plugins"]))}
    )
    assert canonical_plugin_lock(ordered) == canonical_plugin_lock(reversed_input)
    assert plugin_lock_digest(ordered) == plugin_lock_digest(reversed_input)
    assert [entry.id for entry in ordered.plugins] == sorted(entry.id for entry in ordered.plugins)


def test_plugin_lock_rejects_unpinned_cycle_or_duplicate_owner() -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / "plugin-lock.json")
    duplicate = {**fixture, "plugins": [*fixture["plugins"], fixture["plugins"][0]]}
    with pytest.raises(ValidationError, match="exactly one version"):
        PluginLock.model_validate(duplicate)

    missing = json.loads(json.dumps(fixture))
    missing["plugins"][1]["dependencies"][0]["id"] = "io.ksadk.missing"
    with pytest.raises(ValidationError, match="not pinned"):
        PluginLock.model_validate(missing)

    cycle = json.loads(json.dumps(fixture))
    cycle["plugins"][0]["dependencies"] = [
        {
            "id": "io.ksadk.codex-provider",
            "version": "1.0.0",
            "digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        }
    ]
    with pytest.raises(ValidationError, match="dependency cycle"):
        PluginLock.model_validate(cycle)


def test_composition_rejects_clear_secret_value_and_duplicate_capability() -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / "composition-profile.json")
    clear_secret = json.loads(json.dumps(fixture))
    clear_secret["agentProvider"]["config"]["apiKey"] = "do-not-serialize"
    with pytest.raises(ValidationError, match="secret reference"):
        CompositionProfile.model_validate(clear_secret)

    duplicate = json.loads(json.dumps(fixture))
    duplicate["capabilities"].append(duplicate["capabilities"][0])
    with pytest.raises(ValidationError, match="must not repeat"):
        CompositionProfile.model_validate(duplicate)


def test_subagent_provider_golden_matches_schema_and_all_source_types() -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / "subagent-provider.json")
    _validator("subagent-provider.schema.json").validate(fixture)

    request = SpawnSubagentRequest.model_validate(fixture["request"])
    handle = ChildHandle.model_validate(fixture["handle"])
    status = SubagentStatus.model_validate(fixture["status"])
    events = [SubagentEvent.model_validate(item) for item in fixture["events"]]
    result = SubagentResult.model_validate(fixture["result"])

    assert request.model_dump(by_alias=True, mode="json") == fixture["request"]
    assert handle.model_dump(by_alias=True, mode="json") == fixture["handle"]
    assert status.model_dump(by_alias=True, mode="json") == fixture["status"]
    assert [item.model_dump(by_alias=True, mode="json") for item in events] == fixture[
        "events"
    ]
    assert result.model_dump(by_alias=True, mode="json") == fixture["result"]
    assert handle.provider_ref == request.provider_ref
    assert handle.parent_session_id == request.parent_session_id
    assert handle.parent_run_id == request.parent_run_id
    assert status.handle_id == result.handle_id == handle.handle_id
    assert [(item.event_id, item.seq) for item in events] == [
        ("child-event-01", 1),
        ("child-event-02", 2),
    ]
    assert all(item.handle_id == handle.handle_id for item in events)


def test_dsh_agent_provider_host_golden_freezes_the_only_sidecar_abi() -> None:
    """The host ABI is public; it must not become a hidden third package format."""

    fixture = _json(CONTRACT_DIR / "fixtures" / "dsh-agent-provider-host.json")
    _validator("dsh-agent-provider-host.schema.json").validate(fixture)

    assert fixture["contractFormat"] == DSH_AGENT_PROVIDER_HOST_PROTOCOL
    assert set(fixture["handshake"]["methods"]) == DSH_AGENT_PROVIDER_HOST_METHODS
    assert len(fixture["requests"]) == len(fixture["responses"]) == len(
        DSH_AGENT_PROVIDER_HOST_METHODS
    )
    assert [frame["id"] for frame in fixture["requests"]] == [
        frame["id"] for frame in fixture["responses"]
    ]
    assert {frame["method"] for frame in fixture["requests"]} == DSH_AGENT_PROVIDER_HOST_METHODS

    descriptor = DshAgentProviderDescriptor.model_validate(fixture["descriptor"])
    preflight = DshAgentProviderPreflight.model_validate(fixture["preflight"])
    inventory = DshAgentProviderInventory.model_validate(fixture["inventory"])
    assert descriptor.definition == "agent.provider/v1"
    assert descriptor.slot == "agent.execution"
    assert preflight.ready is True
    assert inventory.provider_id == descriptor.provider_id
    assert inventory.descriptor_digest == preflight.descriptor_digest


def test_subagent_provider_contract_rejects_unpinned_secret_and_invalid_terminal() -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / "subagent-provider.json")
    validator = _validator("subagent-provider.schema.json")

    unpinned = deepcopy(fixture)
    unpinned["request"]["providerRef"] = "plugin://io.example.codex-child@latest"
    _assert_invalid(validator, unpinned)
    with pytest.raises(ValidationError, match="exact semantic version"):
        SpawnSubagentRequest.model_validate(unpinned["request"])

    clear_secret = deepcopy(fixture)
    clear_secret["request"]["metadata"] = {"apiKey": "clear-secret"}
    with pytest.raises(ValidationError, match="secret reference"):
        SpawnSubagentRequest.model_validate(clear_secret["request"])

    missing_error = deepcopy(fixture)
    missing_error["result"]["state"] = "failed"
    _assert_invalid(validator, missing_error)
    with pytest.raises(ValidationError, match="requires errorCode"):
        SubagentResult.model_validate(missing_error["result"])

    false_recovery = deepcopy(fixture)
    false_recovery["handle"]["resumable"] = True
    _assert_invalid(validator, false_recovery)
    with pytest.raises(ValidationError, match="requires a resumeDescriptor"):
        ChildHandle.model_validate(false_recovery["handle"])


def test_context_contributor_golden_matches_schema_and_source_projection() -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / "context-contributor.json")
    schema = _json(CONTRACT_DIR / "context-contributor.schema.json")
    assert schema == context_contributor_json_schema()
    _validator("context-contributor.schema.json").validate(fixture)

    exchange = ContextContributorExchange.model_validate(fixture)
    assert exchange.model_dump(by_alias=True, mode="json") == fixture
    projection = exchange.project(now=datetime(2026, 8, 27, tzinfo=timezone.utc))
    assert projection.capabilities.contributor_id == "workspace_rules"
    assert projection.request.session_id == fixture["request"]["scope"]["sessionId"]
    assert projection.request.metadata["policy_ref"] == fixture["request"]["policyRef"]
    assert projection.request.metadata["remaining_budget"] == fixture["request"][
        "remainingBudget"
    ]
    assert [item.item_id for item in projection.items] == [
        "contrib:workspace_rules:resource_manifest"
    ]
    assert projection.items[0].provenance == {
        "source_refs": ["workspace://AGENTS.md"],
        "classification": "internal",
        "expires_at": None,
    }
    assert plugin_api.ContextContributorExchange is ContextContributorExchange


def test_context_contributor_contract_rejects_unauthenticated_secret_and_over_budget() -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / "context-contributor.json")
    validator = _validator("context-contributor.schema.json")

    unauthenticated = deepcopy(fixture)
    unauthenticated["request"]["scope"]["authenticated"] = False
    _assert_invalid(validator, unauthenticated)
    with pytest.raises(ValidationError, match="Input should be True"):
        ContextContributorExchange.model_validate(unauthenticated)

    secret = deepcopy(fixture)
    secret["response"]["fragments"][0]["classification"] = "secret"
    secret["request"]["allowedClassifications"] = ["secret"]
    _assert_invalid(validator, secret)
    with pytest.raises(ValidationError, match="non-secret qualified name"):
        ContextContributorExchange.model_validate(secret)

    unapproved = deepcopy(fixture)
    unapproved["response"]["fragments"][0]["classification"] = "confidential"
    with pytest.raises(ValidationError, match="classification is not allowed"):
        ContextContributorExchange.model_validate(unapproved)

    over_budget = deepcopy(fixture)
    over_budget["response"]["fragments"][0]["tokenEstimate"] = 4001
    with pytest.raises(ValidationError, match="exceeds the effective remaining budget"):
        ContextContributorExchange.model_validate(over_budget)

    elevated = deepcopy(fixture)
    elevated["response"]["fragments"][0]["trustLevel"] = "platform"
    with pytest.raises(ValidationError, match="cannot elevate contributor trust"):
        ContextContributorExchange.model_validate(elevated)

    no_source = deepcopy(fixture)
    no_source["response"]["fragments"][0]["sourceRefs"] = []
    _assert_invalid(validator, no_source)
    with pytest.raises(ValidationError, match="at least 1 item"):
        ContextContributorExchange.model_validate(no_source)

    secret_source = deepcopy(fixture)
    secret_source["response"]["fragments"][0]["sourceRefs"] = [
        "secret://workspace/rules"
    ]
    _assert_invalid(validator, secret_source)
    with pytest.raises(ValidationError, match="cannot point at secret material"):
        ContextContributorExchange.model_validate(secret_source)

    clear_secret = deepcopy(fixture)
    clear_secret["request"]["metadata"] = {"apiKey": "clear-secret"}
    with pytest.raises(ValidationError, match="must contain a secret reference"):
        ContextContributorExchange.model_validate(clear_secret)

    clear_content_secret = deepcopy(fixture)
    clear_content_secret["response"]["fragments"][0]["content"] = {
        "accessToken": "clear-secret"
    }
    with pytest.raises(ValidationError, match="must contain a secret reference"):
        ContextContributorExchange.model_validate(clear_content_secret)

    embedded_source_credential = deepcopy(fixture)
    embedded_source_credential["response"]["fragments"][0]["sourceRefs"] = [
        "https://user:password@example.test/rules"
    ]
    with pytest.raises(ValidationError, match="embedded credentials"):
        ContextContributorExchange.model_validate(embedded_source_credential)

    duplicate = deepcopy(fixture)
    duplicate["response"]["fragments"].append(
        deepcopy(duplicate["response"]["fragments"][0])
    )
    with pytest.raises(ValidationError, match="item identity must be unique"):
        ContextContributorExchange.model_validate(duplicate)


def test_context_contributor_expiry_is_timezone_safe_and_enforced_at_projection() -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / "context-contributor.json")
    validator = _validator("context-contributor.schema.json")

    naive = deepcopy(fixture)
    naive["response"]["fragments"][0]["expiresAt"] = "2026-08-27T00:00:01"
    _assert_invalid(validator, naive)
    with pytest.raises(ValidationError, match="timezone"):
        ContextContributorExchange.model_validate(naive)

    expired = deepcopy(fixture)
    expired["response"]["fragments"][0]["expiresAt"] = "2026-08-27T00:00:01Z"
    exchange = ContextContributorExchange.model_validate(expired)
    with pytest.raises(ValueError, match="has expired"):
        exchange.project(now=datetime(2026, 8, 28, tzinfo=timezone.utc))


def test_agent_bundle_manifest_v2_golden_matches_schema_source_and_digest() -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / "agent-bundle-manifest-v2.json")
    validator = _validator("agent-bundle-manifest-v2.schema.json")
    validator.validate(fixture)

    manifest = BundleManifest.model_validate(fixture)
    assert manifest.bundle_format == "agentkit.bundle/v2"
    assert manifest.model_dump(by_alias=True, exclude_none=True, mode="json") == fixture
    assert compute_bundle_digest(manifest) == fixture["bundleDigest"]


def test_agent_bundle_manifest_v2_rejects_v1_tamper_and_unsafe_file() -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / "agent-bundle-manifest-v2.json")
    validator = _validator("agent-bundle-manifest-v2.schema.json")

    legacy = deepcopy(fixture)
    legacy["bundleFormat"] = "agentkit.bundle/v1"
    _assert_invalid(validator, legacy)
    # The installed source type intentionally retains the v1 read path while
    # the v2 schema remains a distinct cross-language admission contract.
    assert BundleManifest.model_validate(legacy).bundle_format == "agentkit.bundle/v1"

    tampered = deepcopy(fixture)
    tampered["agentId"] = "tampered-agent"
    validator.validate(tampered)
    assert compute_bundle_digest(BundleManifest.model_validate(tampered)) != tampered[
        "bundleDigest"
    ]

    unsafe = deepcopy(fixture)
    unsafe["files"][0]["path"] = "../composition-profile.json"
    _assert_invalid(validator, unsafe)
