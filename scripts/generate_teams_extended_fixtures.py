"""Reproduce effects/build/release vectors from the canonical Python DTOs."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from pydantic import TypeAdapter

from ksadk.plugins.teams.build_artifacts import build_archive
from ksadk.plugins.teams.cloud_contracts import contract_models, digest
from ksadk.plugins.teams.cloud_permits import EXECUTE_OPERATIONS, RECOVERY_OPERATIONS
from ksadk.plugins.teams.effect_contracts import EffectRequest


def update(path):
    vectors = json.loads(path.read_text())
    for group in ("valid", "invalid"):
        vectors[group] = [
            item for item in vectors[group] if not item["name"].startswith("extended-")
        ]
    models = contract_models()

    def parse(model, value):
        dto = models[model]
        return (
            dto.validate_python(value)
            if isinstance(dto, TypeAdapter)
            else dto.model_validate(value)
        )

    def valid(name, model, value):
        result = parse(model, value).model_dump(mode="json")
        vectors["valid"].append({"name": "extended-" + name, "model": model, "value": result})
        invalid(name + "-extra-field", model, result | {"unrecognized": True})
        return result

    def invalid(name, model, value):
        try:
            parse(model, value)
        except ValueError:
            vectors["invalid"].append({"name": "extended-" + name, "model": model, "value": value})
        else:
            raise AssertionError(f"invalid fixture unexpectedly accepted: {name}")

    ref = copy.deepcopy(
        next(item["value"] for item in vectors["valid"] if item["model"] == "execution-reference")
    )
    request = valid(
        "effect-request",
        "effect-request",
        {
            "ref": ref,
            "context_ref": "context-original",
            "store_incarnation": "kernel-original",
            "journal_incarnation": "journal-original",
            "native_run_id": "native-original",
            "tool_call_id": "call-original",
            "effect_index": 0,
            "tool_name": "send-approved",
            "adapter_version": "adapter/v1",
            "effect_class": "external_idempotent",
            "payload_digest": digest({"fixture": "tool-arguments"}),
        },
    )
    typed = EffectRequest.model_validate(request)
    outcome = valid(
        "effect-completed",
        "effect-outcome",
        {
            "phase": "completed",
            "external_ref": "provider-operation",
            "evidence_ref": "verified-evidence",
            "result_digest": digest("redacted-result"),
            "definitively_not_applied": False,
        },
    )
    valid(
        "effect-not-applied",
        "effect-outcome",
        outcome | {"phase": "failed", "definitively_not_applied": True},
    )
    record = valid(
        "effect-record",
        "effect-record",
        {
            "request": request,
            "phase": "completed",
            "revision": 3,
            "outcome": outcome,
            "evidence_digest": digest(
                {
                    "requestDigest": typed.request_digest,
                    "phase": "completed",
                    "revision": 3,
                    "outcome": outcome,
                }
            ),
        },
    )
    valid(
        "effect-prepared",
        "effect-prepared-receipt",
        {
            "effect_key": typed.effect_key,
            "payload_digest": typed.payload_digest,
            "request_digest": typed.request_digest,
            "revision": 1,
            "phase": "prepared",
        },
    )
    valid(
        "effect-report",
        "effect-report-receipt",
        {
            "effectKey": typed.effect_key,
            "journalRevision": 3,
            "evidenceDigest": record["evidence_digest"],
            "authorityRevision": 3,
            "phase": "completed",
        },
    )
    reconcile = valid(
        "effect-reconcile",
        "effect-reconcile-input",
        {
            "expectedRevision": 2,
            "expectedEvidenceDigest": digest("unknown-evidence"),
            "decision": "accept_risk",
            "evidenceRef": "owner-explanation",
            "reason": "人工确认风险范围",
            "idempotencyKey": "resolve-original-key",
        },
    )
    resolution = {key: value for key, value in reconcile.items() if key != "idempotencyKey"} | {
        "resolutionId": "resolution-original",
        "kind": "manual_risk_acceptance",
        "actorSubject": "owner-original",
        "ownerScopeRef": "scope-original",
        "verifiedOutcome": None,
    }
    projection = valid(
        "effect-projection",
        "effect-projection",
        {
            "effectKey": typed.effect_key,
            "groupId": ref["groupId"],
            "teamRunId": ref["teamRunId"],
            "toolName": request["tool_name"],
            "effectClass": request["effect_class"],
            "phase": "resolved",
            "revision": 3,
            "evidenceDigest": reconcile["expectedEvidenceDigest"],
            "resolution": resolution,
            "resolutionConflict": False,
            "outcome": None,
        },
    )
    valid("effect-unicode-id", "effect-projection", projection | {"toolName": "😀" * 256})
    valid(
        "effect-projection-page",
        "effect-projection-page",
        {
            "scope": {
                "authorityId": ref["authorityId"],
                "ownerScopeRef": "scope-original",
                "groupId": ref["groupId"],
            },
            "teamRunId": ref["teamRunId"],
            "items": [projection],
            "nextCursor": None,
        },
    )
    valid(
        "execution-effects-page",
        "execution-effects-page",
        {
            "contextRef": request["context_ref"],
            "storeIncarnation": request["store_incarnation"],
            "nativeRunId": request["native_run_id"],
            "journalIncarnation": request["journal_incarnation"],
            "items": [record],
            "nextCursor": typed.effect_key + ":3",
        },
    )
    permit = {
        "issuer": "fixture-server",
        "kid": "fixture-key",
        "authorityId": ref["authorityId"],
        "issuedAt": "2026-09-19T00:00:00.000Z",
        "expiresAt": "2026-09-19T00:00:30.000Z",
        "nonce": "fixture-nonce",
        "permitKind": "recovery",
        "subjectRef": "owner-original",
        "agentInstanceId": "instance-original",
        "sessionId": ref["sessionId"],
        "commandId": ref["commandId"],
        "allowedOperations": ["observe"],
        "payloadDigest": digest("native-payload"),
        "policyDigest": digest("policy"),
        "leaderEpoch": 1,
        "dispatchEpoch": 1,
        "attemptEpoch": 1,
        "grantRevision": 1,
    }
    envelope = valid(
        "execution-effects-request",
        "execution-effects-request",
        {
            "contextRef": request["context_ref"],
            "expectedIncarnation": request["store_incarnation"],
            "permit": permit,
        },
    )
    for kind, operations in (("execute", EXECUTE_OPERATIONS), ("recovery", RECOVERY_OPERATIONS)):
        valid(
            "execution-permit-" + kind,
            "execution-effects-request",
            envelope
            | {
                "permit": permit | {"permitKind": kind, "allowedOperations": sorted(operations)},
            },
        )
    invalid(
        "effect-run-changed", "effect-request", request | {"ref": ref | {"nativeRunId": "other"}}
    )
    invalid("effect-class-unsafe", "effect-request", request | {"effect_class": "shell"})
    invalid("effect-index-negative", "effect-request", request | {"effect_index": -1})
    invalid("effect-failure-unproven", "effect-outcome", outcome | {"phase": "failed"})
    invalid("effect-completed-no-outcome", "effect-record", record | {"outcome": None})
    invalid("effect-unknown-has-outcome", "effect-record", record | {"phase": "unknown"})
    invalid(
        "effect-resolution-unverified",
        "effect-projection",
        projection | {"resolution": resolution | {"decision": "confirmed_applied"}},
    )
    invalid(
        "effect-projection-missing-resolution",
        "effect-projection",
        projection | {"resolution": None},
    )
    invalid("effect-whitespace-id", "effect-projection", projection | {"toolName": "tool\x85name"})
    valid(
        "effect-non-whitespace-separator",
        "effect-projection",
        projection | {"toolName": "tool\x1cname"},
    )
    invalid("effects-limit", "execution-effects-request", envelope | {"limit": 101})
    invalid(
        "effects-permit-wrong-kind",
        "execution-effects-request",
        envelope | {"permit": permit | {"allowedOperations": ["enqueue"]}},
    )
    invalid(
        "effects-permit-too-long",
        "execution-effects-request",
        envelope | {"permit": permit | {"expiresAt": "2026-09-19T00:02:00.000Z"}},
    )
    invalid(
        "effects-permit-duplicates",
        "execution-effects-request",
        envelope | {"permit": permit | {"allowedOperations": ["observe", "observe"]}},
    )
    build = build_archive(
        {"entrypoint.py": b"# frozen fixture\n"},
        entrypoint="entrypoint.py",
        agent_definition={"name": "fixture"},
        tools_policy={"tools": []},
        behavior_config={"temperature": 0.7},
        dependency_lock=b"fixed==1.0",
    )
    manifest = valid("build-manifest", "build-manifest", build.manifest.model_dump(mode="json"))
    loaded = valid(
        "loaded-build", "loaded-build-evidence", build.loaded_evidence().model_dump(mode="json")
    )
    receipt = valid(
        "build-receipt",
        "build-artifact-receipt",
        {
            "buildArtifactRef": "tbuild_original",
            "state": "verified",
            "authorityId": ref["authorityId"],
            "codeArtifactDigest": build.code_digest,
            "buildManifestDigest": build.manifest_digest,
            "sizeBytes": len(build.archive),
            "manifest": manifest,
        },
    )
    valid(
        "build-lookup-input", "build-artifact-lookup-input", {"idempotencyKey": "original-upload"}
    )
    valid("build-lookup-missing", "build-artifact-lookup-result", {"status": "missing"})
    valid("build-lookup-recorded", "build-artifact-lookup-result", receipt | {"status": "recorded"})
    valid(
        "deployment-receipt",
        "deployment-artifact-receipt",
        {
            "agentId": "agent-original",
            "versionId": "version-original",
            "instanceId": "instance-original",
            "codeArtifactDigest": build.code_digest,
            "buildManifestDigest": build.manifest_digest,
            "bundleDigest": digest("separate-bundle-domain"),
            "contractDigest": digest("fixture-contract"),
            "deployedAt": "2026-09-19T00:00:00.000Z",
        },
    )
    release = valid(
        "release-input",
        "release-verify-input",
        {
            "buildArtifactRef": receipt["buildArtifactRef"],
            "localBindingRef": "local-fixed",
            "cloudBindingRef": "cloud-fixed",
            "idempotencyKey": "verify-original",
        },
    )
    valid(
        "release-receipt",
        "release-verification-receipt",
        {
            **{key: value for key, value in release.items() if key != "idempotencyKey"},
            "releaseRef": "trelease_original",
            "evidenceDigest": digest("proof"),
            "verifiedAt": "2026-09-19T00:00:00.000Z",
        },
    )
    valid("release-unicode-id", "release-verify-input", release | {"idempotencyKey": "😀" * 200})
    invalid("build-entrypoint-absent", "build-manifest", manifest | {"entrypoint": "absent.py"})
    invalid(
        "build-files-unsorted",
        "build-manifest",
        manifest | {"files": list(reversed(manifest["files"]))},
    )
    invalid(
        "build-descriptor-different",
        "build-manifest",
        manifest | {"toolsPolicyDigest": digest("changed")},
    )
    invalid("build-path-traversal", "build-manifest", manifest | {"entrypoint": "../entrypoint.py"})
    invalid(
        "loaded-workspace-writable", "loaded-build-evidence", loaded | {"immutableWorkspace": False}
    )
    invalid(
        "loaded-inputs-mutable", "loaded-build-evidence", loaded | {"externalMutableInputs": True}
    )
    invalid(
        "build-oversized", "build-artifact-receipt", receipt | {"sizeBytes": 20 * 1024 * 1024 + 1}
    )
    invalid("release-caller-verification", "release-verify-input", release | {"verified": True})
    path.write_text(json.dumps(vectors, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", required=True, type=Path)
    update(parser.parse_args().fixtures)
