#!/usr/bin/env python3
"""Bind Phase 2 local, registry, deployment and pre-production evidence.

The local preflight deliberately cannot claim release completion.  This gate is
the second half of the release contract: it accepts independently collected
evidence only when every artifact and deployed consumer points at the same
final source commit and the same published Web package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__:
    from scripts.phase2_release_preflight import (
        PHASE2_E2E_STATUS_KEYS,
        PHASE2_EVIDENCE_SCHEMA_VERSION,
    )
else:
    from phase2_release_preflight import (  # type: ignore[no-redef]
        PHASE2_E2E_STATUS_KEYS,
        PHASE2_EVIDENCE_SCHEMA_VERSION,
    )

SCHEMA_VERSION = 1
WEB_PACKAGE_NAME = "@kingsoftcloud/ksadk-web"
WEB_PACKAGE_VERSION = "0.3.3"
REQUIRED_SCENARIOS = ("studioCreatedAgent", "historical082Agent")
REQUIRED_SURFACES = {"studio", "hosted-ui"}

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_NPM_INTEGRITY = re.compile(r"^sha512-[A-Za-z0-9+/]+={0,2}$")
_IMAGE_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_SECRET_KEY = re.compile(
    r"(?:password|secret|token|authorization|access[_-]?key|private[_-]?key|dsn)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/=-]+|postgres(?:ql)?://[^\s/@:]+:[^\s/@]+@)",
    re.IGNORECASE,
)


class ReleaseCandidateGateError(RuntimeError):
    """The supplied evidence cannot support a release claim."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseCandidateGateError(f"invalid evidence file: {path}") from error
    if not isinstance(payload, dict):
        raise ReleaseCandidateGateError(f"evidence must be a JSON object: {path}")
    _reject_secret_shaped_content(payload, path.name)
    return payload


def _reject_secret_shaped_content(value: Any, location: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{location}.{key}"
            if _SECRET_KEY.search(str(key)):
                raise ReleaseCandidateGateError(f"secret-shaped evidence key: {child}")
            _reject_secret_shaped_content(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_shaped_content(item, f"{location}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        raise ReleaseCandidateGateError(f"secret-shaped evidence value: {location}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _require_web_identity(payload: Mapping[str, Any], *, location: str) -> tuple[str, str]:
    package = payload.get("package")
    version = payload.get("version")
    integrity = payload.get("npmIntegrity")
    if package != WEB_PACKAGE_NAME or version != WEB_PACKAGE_VERSION:
        raise ReleaseCandidateGateError(f"{location} Web package identity is invalid")
    if not isinstance(integrity, str) or not _NPM_INTEGRITY.fullmatch(integrity):
        raise ReleaseCandidateGateError(f"{location} npm integrity is invalid")
    return str(version), integrity


def _validate_local(payload: Mapping[str, Any], expected_commit: str) -> None:
    if (
        payload.get("schemaVersion") != PHASE2_EVIDENCE_SCHEMA_VERSION
        or payload.get("phase") != "phase2"
        or payload.get("scope") != "local-source-and-package"
    ):
        raise ReleaseCandidateGateError("local Phase 2 evidence schema is invalid")
    if payload.get("sourceCommit") != expected_commit:
        raise ReleaseCandidateGateError("local evidence is not bound to the final commit")
    if payload.get("localStatus") != "passed":
        raise ReleaseCandidateGateError("local Phase 2 preflight has not passed")
    if (
        payload.get("overallStatus") != "incomplete"
        or payload.get("releaseStatus") != "not_evaluated"
    ):
        raise ReleaseCandidateGateError("local evidence makes an invalid release claim")
    statuses = payload.get("e2e")
    if not isinstance(statuses, Mapping) or set(statuses) != set(PHASE2_E2E_STATUS_KEYS):
        raise ReleaseCandidateGateError("local Phase 2 E2E evidence is incomplete")
    if any(status != "passed" for status in statuses.values()):
        raise ReleaseCandidateGateError("a local Phase 2 E2E gate did not pass")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {"wheel", "sdist"}:
        raise ReleaseCandidateGateError("local release artifact evidence is incomplete")
    if not all(
        isinstance(item, Mapping) and _SHA256.fullmatch(str(item.get("sha256") or ""))
        for item in artifacts.values()
    ):
        raise ReleaseCandidateGateError("local release artifact digest is invalid")


def _validate_web(payload: Mapping[str, Any]) -> str:
    if payload.get("schemaVersion") != 1 or payload.get("status") != "published":
        raise ReleaseCandidateGateError("Web registry evidence is not published")
    _version, integrity = _require_web_identity(payload, location="registry")
    source_commit = str(payload.get("sourceCommit") or "")
    if not _COMMIT.fullmatch(source_commit):
        raise ReleaseCandidateGateError("Web registry source commit is invalid")
    if payload.get("registry") != "https://registry.npmjs.org":
        raise ReleaseCandidateGateError("Web package was not verified against the public registry")
    return integrity


def _validate_deployment(payload: Mapping[str, Any], expected_integrity: str) -> str:
    if payload.get("schemaVersion") != 1 or payload.get("environment") != "preproduction":
        raise ReleaseCandidateGateError("Hosted UI deployment evidence is invalid")
    image = str(payload.get("hostedUiImage") or "")
    if not _IMAGE_DIGEST.fullmatch(image):
        raise ReleaseCandidateGateError("Hosted UI image is not digest-pinned")
    if not isinstance(payload.get("helmRevision"), int) or int(payload["helmRevision"]) < 1:
        raise ReleaseCandidateGateError("Hosted UI Helm revision is invalid")
    web = payload.get("webPackage")
    if not isinstance(web, Mapping):
        raise ReleaseCandidateGateError("Hosted UI Web package evidence is missing")
    _version, integrity = _require_web_identity(web, location="deployment")
    if integrity != expected_integrity:
        raise ReleaseCandidateGateError("Hosted UI does not consume the published Web artifact")
    return image


def _validate_scenario(name: str, payload: Mapping[str, Any]) -> None:
    if payload.get("status") != "passed":
        raise ReleaseCandidateGateError(f"pre-production scenario did not pass: {name}")
    for field in ("agentId", "sessionId"):
        if not isinstance(payload.get(field), str) or not str(payload[field]).strip():
            raise ReleaseCandidateGateError(f"pre-production scenario {name} lacks {field}")
    if not isinstance(payload.get("turns"), int) or int(payload["turns"]) < 2:
        raise ReleaseCandidateGateError(f"pre-production scenario {name} is not multi-turn")
    if not isinstance(payload.get("streamChunks"), int) or int(payload["streamChunks"]) < 2:
        raise ReleaseCandidateGateError(f"pre-production scenario {name} did not prove streaming")
    if payload.get("duplicateItems") != 0:
        raise ReleaseCandidateGateError(f"pre-production scenario {name} observed duplicates")
    surfaces = payload.get("surfaces")
    if not isinstance(surfaces, list) or not REQUIRED_SURFACES.issubset(set(surfaces)):
        raise ReleaseCandidateGateError(f"pre-production scenario {name} lacks a UI surface")
    expected_cleanup = "deleted" if name == "studioCreatedAgent" else "preserved"
    if payload.get("cleanupStatus") != expected_cleanup:
        raise ReleaseCandidateGateError(f"pre-production scenario {name} cleanup is invalid")


def _validate_preprod(
    payload: Mapping[str, Any],
    *,
    expected_commit: str,
    expected_integrity: str,
    expected_image: str,
) -> None:
    if payload.get("schemaVersion") != 1 or payload.get("environment") != "preproduction":
        raise ReleaseCandidateGateError("pre-production evidence schema is invalid")
    if payload.get("sourceCommit") != expected_commit:
        raise ReleaseCandidateGateError("pre-production evidence is not bound to the final commit")
    if payload.get("hostedUiImage") != expected_image:
        raise ReleaseCandidateGateError("pre-production image differs from deployed Hosted UI")
    web = payload.get("webPackage")
    if not isinstance(web, Mapping):
        raise ReleaseCandidateGateError("pre-production Web package evidence is missing")
    _version, integrity = _require_web_identity(web, location="pre-production")
    if integrity != expected_integrity:
        raise ReleaseCandidateGateError("pre-production did not use the published Web artifact")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, Mapping) or set(scenarios) != set(REQUIRED_SCENARIOS):
        raise ReleaseCandidateGateError("pre-production scenario matrix is incomplete")
    for name in REQUIRED_SCENARIOS:
        scenario = scenarios[name]
        if not isinstance(scenario, Mapping):
            raise ReleaseCandidateGateError(f"pre-production scenario is invalid: {name}")
        _validate_scenario(name, scenario)


def build_release_candidate_report(
    *,
    expected_commit: str,
    local_path: Path,
    web_path: Path,
    deployment_path: Path,
    preprod_path: Path,
) -> dict[str, Any]:
    expected_commit = expected_commit.lower()
    if not _COMMIT.fullmatch(expected_commit):
        raise ReleaseCandidateGateError("final source commit must be a full Git SHA")
    local = _load_object(local_path)
    web = _load_object(web_path)
    deployment = _load_object(deployment_path)
    preprod = _load_object(preprod_path)
    _validate_local(local, expected_commit)
    integrity = _validate_web(web)
    image = _validate_deployment(deployment, integrity)
    _validate_preprod(
        preprod,
        expected_commit=expected_commit,
        expected_integrity=integrity,
        expected_image=image,
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "phase": "phase2",
        "scope": "final-release-candidate",
        "overallStatus": "passed",
        "sourceCommit": expected_commit,
        "contractDigest": local["contractDigest"],
        "artifacts": local["artifacts"],
        "webPackage": {
            "package": WEB_PACKAGE_NAME,
            "version": WEB_PACKAGE_VERSION,
            "npmIntegrity": integrity,
            "sourceCommit": web["sourceCommit"],
        },
        "hostedUi": {
            "image": image,
            "helmRevision": deployment["helmRevision"],
        },
        "scenarios": {name: "passed" for name in REQUIRED_SCENARIOS},
        "inputs": {
            "local": _sha256(local_path),
            "webRegistry": _sha256(web_path),
            "deployment": _sha256(deployment_path),
            "preproduction": _sha256(preprod_path),
        },
    }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--local", required=True, type=Path)
    parser.add_argument("--web-registry", required=True, type=Path)
    parser.add_argument("--deployment", required=True, type=Path)
    parser.add_argument("--preprod", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = build_release_candidate_report(
            expected_commit=args.expected_commit,
            local_path=args.local,
            web_path=args.web_registry,
            deployment_path=args.deployment,
            preprod_path=args.preprod,
        )
    except ReleaseCandidateGateError as error:
        print(f"Phase 2 release candidate gate failed: {error}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Phase 2 release candidate gate passed: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
