"""Cross-language fixtures are literal expected wire bytes, shared with Web/Server."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from ksadk.plugins.teams.cloud_contracts import (
    MaterialManifest,
    NodeReport,
    TeamsExecutionRef,
    canonical_bytes,
    contract_models,
    contract_schemas,
    contract_versions,
    parse_node_command,
)
from ksadk.plugins.teams.cloud_permits import PERMIT_MODELS

VECTORS = json.loads((Path(__file__).parent / "fixtures/teams-cloud-v1.json").read_text())


@pytest.mark.parametrize("case", VECTORS["canonicalVectors"], ids=lambda c: c["name"])
def test_canonical_wire_bytes(case):
    actual = canonical_bytes(case["value"])
    assert actual == case["canonical"].encode()
    assert "sha256:" + hashlib.sha256(actual).hexdigest() == case["digest"]


@pytest.mark.parametrize("case", VECTORS["canonicalInvalidVectors"], ids=lambda c: c["name"])
def test_reject_non_interoperable_json(case):
    with pytest.raises(ValueError):
        canonical_bytes(json.loads(case["json"]))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_reject_nonfinite_number(value):
    with pytest.raises(ValueError):
        canonical_bytes({"nested": [value]})


def parse_fixture(case):
    if case["model"] == "node-command":
        return parse_node_command(case["value"])
    if case["model"] == "node-probe-command":
        from ksadk.plugins.teams.cloud_contracts import parse_node_message

        return parse_node_message(case["value"])
    if case["model"] == "node-probe-report":
        from ksadk.plugins.teams.cloud_contracts import NodeProbeReport

        return NodeProbeReport.model_validate(case["value"])
    if case["model"] == "execution-reference":
        return TeamsExecutionRef.model_validate(case["value"])
    if case["model"] == "material-manifest":
        return MaterialManifest.model_validate(case["value"])
    if case["model"] == "node-report":
        return NodeReport.model_validate(case["value"])
    model = contract_models()[case["model"]]
    return (
        model.validate_python(case["value"])
        if isinstance(model, TypeAdapter)
        else model.model_validate(case["value"])
    )


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["name"])
def test_valid_shared_contracts(case):
    parsed = parse_fixture(case)
    if case["model"] == "material-manifest":
        assert parsed.manifest_digest == case["manifestDigest"]


@pytest.mark.parametrize("case", VECTORS["invalid"], ids=lambda c: c["name"])
def test_invalid_shared_contracts(case):
    with pytest.raises(ValueError):
        parse_fixture(case)


def test_published_schemas_and_digests_match_current_code():
    root = Path(__file__).parents[2] / "contracts/teams-cloud/v1"
    schemas = contract_schemas()
    schemas.update({name: model.model_json_schema() for name, model in PERMIT_MODELS.items()})
    manifest = json.loads((root / "manifest.json").read_text())
    assert set(manifest["schemas"]) == {name + ".schema.json" for name in schemas}
    assert all(manifest[name] == value for name, value in contract_versions().items())
    for name, schema in schemas.items():
        data = (root / f"{name}.schema.json").read_bytes()
        assert json.loads(data) == schema
        assert manifest["schemas"][f"{name}.schema.json"] == (
            "sha256:" + hashlib.sha256(data).hexdigest()
        )
    digest = manifest.pop("contractDigest")
    assert digest == "sha256:" + hashlib.sha256(canonical_bytes(manifest)).hexdigest()


def test_completion_size_limit_is_independent_of_valid_digests():
    from ksadk.plugins.teams.cloud_contracts import ExecutionResult, digest

    candidate = {"result": "x" * (1024 * 1024), "artifacts": []}
    example = next(
        case["value"]["executionResult"]
        for case in VECTORS["valid"]
        if case["name"] == "canonical-completion-terminal"
    )
    facts = {key: value for key, value in example.items() if key != "completionDigest"}
    facts["candidate"] = candidate
    facts["terminalEvidence"] = facts["terminalEvidence"] | {"resultDigest": digest(candidate)}
    with pytest.raises(ValueError, match="exceeds one MiB"):
        ExecutionResult.model_validate(facts | {"completionDigest": digest(facts)})


def test_packaged_contract_fingerprint_matches_exported_manifest():
    from ksadk.plugins.teams.cloud_contract_fingerprints import teams_cloud_contract_digest

    root = Path(__file__).parents[2] / "contracts/teams-cloud/v1"
    assert (
        teams_cloud_contract_digest()
        == json.loads((root / "manifest.json").read_text())["contractDigest"]
    )


def test_extended_contract_legacy_imports_are_exact_reexports():
    from ksadk.kernel.teams_effects import EffectRecord as legacy_record
    from ksadk.kernel.teams_host_http import EffectsRequest as legacy_request
    from ksadk.kernel.teams_tool_runtime import ExecutionEffectsPage as legacy_page
    from ksadk.plugins.teams.effect_contracts import (
        EffectRecord,
        EffectsRequest,
        ExecutionEffectsPage,
    )

    assert legacy_record is EffectRecord
    assert legacy_request is EffectsRequest
    assert legacy_page is ExecutionEffectsPage
