"""PluginEcosystemBridge/v1 frozen source and JSON Schema conformance."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from pydantic import ValidationError

import ksadk.plugins as plugin_api
from ksadk.plugins.ecosystem_bridge import (
    BridgeInspectExchange,
    BridgeProbeExchange,
    BridgeProbeRequest,
    BridgeProbeResult,
    BridgeRejection,
    PluginEcosystemBridgeFixture,
    PluginEcosystemBridgeTranscript,
    ecosystem_bridge_json_schema,
)

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "plugin" / "v1"
FIXTURES = (
    "plugin-ecosystem-bridge-codex.json",
    "plugin-ecosystem-bridge-dual-manifest.json",
    "plugin-ecosystem-bridge-host-missing.json",
)


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _validator() -> Draft202012Validator:
    schema = _json(CONTRACT_DIR / "plugin-ecosystem-bridge.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_bridge_fixture_matches_frozen_schema_and_source_model(fixture_name: str) -> None:
    payload = _json(CONTRACT_DIR / "fixtures" / fixture_name)
    _validator().validate(payload)
    parsed = PluginEcosystemBridgeFixture.model_validate(payload)
    assert parsed.model_dump(by_alias=True, mode="json") == payload


def test_schema_is_generated_from_the_public_frozen_source_contract() -> None:
    schema = _json(CONTRACT_DIR / "plugin-ecosystem-bridge.schema.json")
    assert schema == ecosystem_bridge_json_schema()
    assert plugin_api.PluginEcosystemBridgeFixture is PluginEcosystemBridgeFixture
    assert plugin_api.ecosystem_bridge_json_schema is ecosystem_bridge_json_schema


def test_codex_golden_traces_bridge_host_artifact_and_native_receipt() -> None:
    payload = _json(CONTRACT_DIR / "fixtures" / "plugin-ecosystem-bridge-codex.json")
    parsed = PluginEcosystemBridgeFixture.model_validate(payload).root
    assert isinstance(parsed, PluginEcosystemBridgeTranscript)

    bridge = parsed.describe_result.descriptor
    receipt = parsed.commit_result.receipt
    inventory = parsed.reconcile_result.inventory
    assert bridge.integration_mode == "bridged"
    assert bridge.maturity == "bridged-ready"
    assert receipt.bridge_digest == inventory.bridge_digest == bridge.bridge_digest
    assert receipt.host == inventory.host == parsed.stage_result.host
    assert receipt.native_receipt_digest == inventory.native_receipt_digest
    assert receipt.artifact_digest == inventory.artifact_digest
    assert parsed.dispose_result.state == "disposed"


def test_multiple_manifests_require_an_explicit_ecosystem_selection() -> None:
    payload = _json(CONTRACT_DIR / "fixtures" / "plugin-ecosystem-bridge-dual-manifest.json")
    parsed = PluginEcosystemBridgeFixture.model_validate(payload).root
    assert isinstance(parsed, BridgeProbeExchange)
    assert parsed.result.selection_required is True
    assert {candidate.ecosystem for candidate in parsed.result.candidates} == {
        "dsh",
        "codex",
    }

    auto_selected = deepcopy(payload)
    auto_selected["result"]["selectionRequired"] = False
    auto_selected["result"]["selectedManifestRef"] = auto_selected["result"]["candidates"][0][
        "manifestRef"
    ]
    with pytest.raises(ValidationError, match="multiple manifests require explicit selection"):
        PluginEcosystemBridgeFixture.model_validate(auto_selected)

    explicit = deepcopy(payload)
    selected = explicit["result"]["candidates"][1]["manifestRef"]
    explicit["request"]["selectedManifestRef"] = selected
    explicit["result"]["selectionRequired"] = False
    explicit["result"]["selectedManifestRef"] = selected
    accepted = PluginEcosystemBridgeFixture.model_validate(explicit).root
    assert isinstance(accepted, BridgeProbeExchange)
    assert accepted.result.selected_manifest_ref == selected


def test_external_permissions_and_secret_values_fail_closed() -> None:
    payload = _json(CONTRACT_DIR / "fixtures" / "plugin-ecosystem-bridge-codex.json")

    missing_permissions = deepcopy(payload)
    missing_permissions["inspectResult"]["descriptor"]["permissionsDeclared"] = False
    missing_permissions["planRequest"]["descriptor"]["permissionsDeclared"] = False
    with pytest.raises(ValidationError, match="explicit risk acceptance is required"):
        PluginEcosystemBridgeFixture.model_validate(missing_permissions)

    explicitly_accepted = deepcopy(missing_permissions)
    explicitly_accepted["planRequest"]["acceptUndeclaredPermissions"] = True
    explicitly_accepted["planResult"]["plan"]["permissionsDeclared"] = False
    explicitly_accepted["planResult"]["plan"]["undeclaredPermissionsAccepted"] = True
    explicitly_accepted["stageRequest"]["plan"]["permissionsDeclared"] = False
    explicitly_accepted["stageRequest"]["plan"]["undeclaredPermissionsAccepted"] = True
    accepted = PluginEcosystemBridgeFixture.model_validate(explicitly_accepted).root
    assert isinstance(accepted, PluginEcosystemBridgeTranscript)
    assert accepted.plan_result.plan.undeclared_permissions_accepted is True

    inspect_only = deepcopy(payload["inspectResult"]["descriptor"])
    inspect_only["permissionsDeclared"] = False
    inspect_only["pluginVersion"] = None
    descriptor = plugin_api.EcosystemPluginDescriptor.model_validate(inspect_only)
    assert descriptor.permissions_declared is False
    assert descriptor.plugin_version is None

    clear_secret = deepcopy(payload)
    clear_secret["inspectResult"]["descriptor"]["secretRefs"] = ["clear-secret"]
    with pytest.raises(ValidationError, match="references only"):
        PluginEcosystemBridgeFixture.model_validate(clear_secret)

    unexpected = deepcopy(payload)
    unexpected["describeResult"]["descriptor"]["executesPlugins"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PluginEcosystemBridgeFixture.model_validate(unexpected)


def test_missing_native_host_is_a_typed_rejection_not_fake_inventory() -> None:
    payload = _json(CONTRACT_DIR / "fixtures" / "plugin-ecosystem-bridge-host-missing.json")
    parsed = PluginEcosystemBridgeFixture.model_validate(payload).root
    assert isinstance(parsed, BridgeInspectExchange)
    assert parsed.result.status == "rejected"
    assert parsed.result.descriptor is None
    assert parsed.result.rejection is not None
    assert parsed.result.rejection.code == "host_unavailable"
    assert parsed.result.rejection.host is not None
    assert parsed.result.rejection.host.available is False

    false_ready = deepcopy(payload)
    false_ready["result"]["rejection"]["host"]["available"] = True
    false_ready["result"]["rejection"]["host"]["version"] = "1.0.0"
    false_ready["result"]["rejection"]["host"]["protocol"] = "codex.app-server/v1"
    false_ready["result"]["rejection"]["host"]["protocolVersion"] = "1.0.0"
    false_ready["result"]["rejection"]["host"]["digest"] = "sha256:" + "9" * 64
    with pytest.raises(ValidationError, match="cannot claim"):
        PluginEcosystemBridgeFixture.model_validate(false_ready)


def test_probe_with_no_candidate_returns_a_typed_rejection() -> None:
    request = BridgeProbeRequest(
        source_ref="file://fixtures/not-a-plugin",
        source_digest="sha256:" + "7" * 64,
    )
    result = BridgeProbeResult(
        candidates=(),
        selection_required=False,
        rejection=BridgeRejection(
            action="probe",
            code="unsupported",
            retryable=False,
            message="No supported plugin manifest was detected.",
        ),
    )
    exchange = BridgeProbeExchange(request=request, result=result)
    assert exchange.result.rejection is not None
    assert exchange.result.rejection.code == "unsupported"


@pytest.mark.parametrize(
    "source_ref",
    (
        "https://user:clear-secret@example.test/plugin",
        "https://example.test/plugin?api_key=clear-secret",
    ),
)
def test_bridge_references_cannot_embed_credentials(source_ref: str) -> None:
    with pytest.raises(ValidationError, match="must not embed"):
        BridgeProbeRequest(
            source_ref=source_ref,
            source_digest="sha256:" + "7" * 64,
        )


def test_bridge_contract_does_not_define_a_common_plugin_execution_abi() -> None:
    schema_text = json.dumps(ecosystem_bridge_json_schema(), sort_keys=True)
    assert "executePlugin" not in schema_text
    assert "runPlugin" not in schema_text
    assert "invokeTool" not in schema_text
    assert all(
        callable(getattr(plugin_api.PluginEcosystemBridge, name))
        for name in {
            "describe",
            "probe",
            "inspect",
            "plan",
            "stage",
            "commit",
            "reconcile",
            "rollback",
            "dispose",
        }
    )
