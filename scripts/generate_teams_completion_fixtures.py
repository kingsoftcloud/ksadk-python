"""Add deterministic complete-result vectors to the shared Teams fixture corpus."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from ksadk.plugins.teams.cloud_contracts import (
    ExecutionResult,
    NodeProbeCommand,
    digest,
    node_probe_digest,
)


def update(path: Path):
    data = json.loads(path.read_text())
    ref = data["valid"][0]["value"]
    candidate = {"result": "Canonical team result", "artifacts": []}
    evidence = {
        "sessionId": ref["sessionId"],
        "runId": "run-example",
        "commandId": ref["commandId"],
        "terminalSeq": 12,
        "terminalEventDigest": digest({"fixture": "terminal"}),
        "resultDigest": digest(candidate),
    }
    result = ExecutionResult.from_host_result(
        {
            "status": "succeeded",
            "candidate": candidate,
            "usage": {"totalTokens": 11},
            "terminalEvidence": evidence,
        }
    ).model_dump(mode="json")
    value = {
        "nodeCommandId": "33333333-3333-4333-8333-333333333333",
        "nodeGeneration": 1,
        "commandDigest": digest({"fixture": "lookup"}),
        "resultRevision": 1,
        "phase": "terminal",
        "receipt": {
            "status": "accepted",
            "commandId": ref["commandId"],
            "idempotencyKey": ref["idempotencyKey"],
            "payloadDigest": digest({"fixture": "command"}),
            "storeIncarnation": "original-store",
            "acceptedSeq": 1,
            "runId": "run-example",
            "nativeStatus": "succeeded",
            "terminalEvidence": evidence,
        },
        "executionResult": result,
    }
    variants = []
    missing = copy.deepcopy(value)
    missing.pop("executionResult")
    variants.append(("missing-result", missing))
    usage = copy.deepcopy(value)
    usage["executionResult"]["usage"]["totalTokens"] = 12
    variants.append(("changed-usage", usage))
    changed = copy.deepcopy(value)
    changed["executionResult"]["candidate"]["result"] = "Different candidate"
    changed["executionResult"]["completionDigest"] = digest(
        {k: v for k, v in changed["executionResult"].items() if k != "completionDigest"}
    )
    variants.append(("changed-candidate-evidence", changed))
    wrong = copy.deepcopy(value)
    wrong["receipt"]["terminalEvidence"]["terminalSeq"] = 13
    variants.append(("changed-receipt-terminal", wrong))
    nonterminal = copy.deepcopy(value)
    nonterminal["phase"] = "waiting"
    variants.append(("nonterminal-result", nonterminal))
    for group in ("valid", "invalid"):
        data[group] = [
            case for case in data[group] if not case["name"].startswith("canonical-completion-")
        ]
    data["valid"].append(
        {"name": "canonical-completion-terminal", "model": "node-report", "value": value}
    )
    data["invalid"].extend(
        {"name": "canonical-completion-" + name, "model": "node-report", "value": v}
        for name, v in variants
    )
    probe = NodeProbeCommand(
        nodeCommandId="44444444-4444-4444-8444-444444444444",
        operation="describe",
        operationKey="probe-example",
        authorityId=ref["authorityId"],
        nodeId="node-example",
        bindingRef="binding-example",
        localBindingRef="local-build:build-example",
        expectedDigests={
            "bundle": digest("bundle"),
            "contract": digest("contract"),
            "capabilities": digest({"teamsReady": False}),
        },
        nodeGeneration=1,
        claimLeaseUntil="2026-09-19T00:00:30.000Z",
        authorization={"permitKind": "probe", "permit": "signed-probe-fixture"},
        commandDigest=digest("placeholder"),
    )
    probe = probe.model_copy(update={"commandDigest": node_probe_digest(probe)}).model_dump(
        mode="json"
    )
    report = {
        "reportKind": "probe",
        "nodeCommandId": probe["nodeCommandId"],
        "nodeGeneration": 1,
        "commandDigest": probe["commandDigest"],
        "resultRevision": 1,
        "phase": "described",
        "probeResult": {
            "bindingRef": "binding-example",
            "localBindingRef": "local-build:build-example",
            "agentInstanceId": "instance-example",
            "storeIncarnation": "original-store",
            "capabilities": {"teamsReady": False},
            "capabilitiesDigest": digest({"teamsReady": False}),
            "bundleDigest": digest("bundle"),
            "contractDigest": digest("contract"),
        },
    }
    for group in ("valid", "invalid"):
        data[group] = [case for case in data[group] if not case["name"].startswith("node-probe-")]
    data["valid"].extend(
        [
            {"name": "node-probe-command", "model": "node-probe-command", "value": probe},
            {"name": "node-probe-report", "model": "node-probe-report", "value": report},
        ]
    )
    for missing_key in ("bundle", "contract", "capabilities"):
        invalid = copy.deepcopy(probe)
        invalid["expectedDigests"].pop(missing_key)
        data["invalid"].append(
            {
                "name": "node-probe-missing-" + missing_key,
                "model": "node-probe-command",
                "value": invalid,
            }
        )
    invalid = copy.deepcopy(probe)
    invalid["expectedDigests"]["unknown"] = digest("unknown")
    data["invalid"].append(
        {"name": "node-probe-extra-digest", "model": "node-probe-command", "value": invalid}
    )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", type=Path, default=Path("tests/contracts/fixtures/teams-cloud-v1.json")
    )
    update(parser.parse_args().fixture)
