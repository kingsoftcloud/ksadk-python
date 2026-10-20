#!/usr/bin/env python3
"""Audit one branch's reachable source history without displaying matched values."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from open_source_audit import CONTENT_RULES, audit_paths

ROOT = Path(__file__).resolve().parents[1]
HISTORY_RULES = {
    "private-stage-endpoint",
    "personal-cluster-config",
    "fixed-private-registry-namespace",
    "internal-git-remote",
    "local-absolute-path",
    "public-doc-internal-endpoint",
    "public-doc-personal-agent-name",
    "public-doc-internal-scm",
}


# Messages are public prose, so apply the same context as curated docs. Names
# and emails retain ordinary open-source attribution; only concrete endpoints,
# credentials and private paths are rejected in author/committer fields.
_METADATA_CONTEXT_PATH = "docs/commit-metadata.md"
_IDENTITY_PROSE_RULES = {"public-doc-internal-scm", "public-doc-personal-agent-name"}
_IDENTITY_TRAILER_KEYS = {
    "co-authored-by", "signed-off-by", "reviewed-by", "acked-by",
    "tested-by", "reported-by", "suggested-by", "helped-by",
}


def _message_fields(message: str) -> list[tuple[str, str]]:
    prose, attribution = [], []
    identity_continuation = False
    for line in message.splitlines(keepends=True):
        key, separator, _value = line.partition(":")
        identity_line = bool(separator) and key.casefold() in _IDENTITY_TRAILER_KEYS
        if identity_line or (identity_continuation and line.startswith((" ", "\t"))):
            attribution.append(line)
            identity_continuation = True
        else:
            prose.append(line)
            identity_continuation = False
    return [("message", "".join(prose)), ("message-trailer", "".join(attribution))]


def _commit_metadata_findings(oid: str, data: bytes) -> list[dict[str, object]]:
    headers, _separator, message = data.partition(b"\n\n")
    encoding = "utf-8"
    fields: list[tuple[str, str]] = []
    for line in headers.splitlines():
        name, _space, value = line.partition(b" ")
        if name == b"encoding":
            encoding = value.decode("ascii", errors="replace")
        elif name in {b"author", b"committer"}:
            fields.append((name.decode("ascii"), value.decode("utf-8", errors="replace")))
    try:
        decoded = message.decode(encoding, errors="replace")
    except LookupError:
        decoded = message.decode("utf-8", errors="replace")
    fields.extend(_message_fields(decoded))
    return [
        {"commit": oid, "field": field, "rule": rule.name}
        for field, text in fields
        for rule in CONTENT_RULES
        if not (field != "message" and rule.name in _IDENTITY_PROSE_RULES)
        and rule.matches(_METADATA_CONTEXT_PATH, text)
    ]


def audit_history(root: Path, ref: str = "HEAD") -> list[dict[str, object]]:
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=root, text=True,
    ).strip()
    objects = subprocess.check_output(
        ["git", "-c", "core.quotePath=false", "rev-list", "--objects", revision],
        cwd=root, text=True,
    ).splitlines()
    findings = []
    # Object enumeration reports one path per blob. Equal content at a public
    # and a private path must not hide the latter from the path-policy audit.
    changes = subprocess.check_output(
        ["git", "-c", "core.quotePath=false", "log", "--format=", "--raw", "--no-abbrev",
         "--no-renames", "--diff-filter=AM", "--root", "-m", revision],
        cwd=root, text=True,
    ).splitlines()
    locations: dict[str, set[str]] = {}
    for change in changes:
        if not change.startswith(":"):
            continue
        metadata, path = change.split("\t", 1)
        oid = metadata.split()[3]
        locations.setdefault(oid, set()).add(path)
    for path in sorted({path for paths in locations.values() for path in paths}):
        for violation in audit_paths("public-repo", [path]).violations:
            findings.append({"path": path, "rule": violation.rule})
    process = subprocess.Popen(
        ["git", "cat-file", "--batch"], cwd=root,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        for item in objects:
            oid, _, path = item.partition(" ")
            process.stdin.write((oid + "\n").encode())
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3:
                raise RuntimeError("Cannot read a reachable Git object")
            size = int(header[2])
            data = process.stdout.read(size)
            terminator = process.stdout.read(1)
            if len(data) != size or terminator != b"\n":
                raise RuntimeError("Incomplete reachable Git object")
            if header[1] == b"commit":
                findings.extend(_commit_metadata_findings(oid, data))
                continue
            if header[1] != b"blob":
                continue
            if b"\0" in data:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for rule in CONTENT_RULES:
                if rule.name not in HISTORY_RULES:
                    continue
                for related_path in sorted(locations.get(oid, {path})):
                    if rule.matches(related_path, text):
                        findings.append({"blob": oid, "path": related_path, "rule": rule.name})
            if ".gitleaks.toml" in locations.get(oid, {path}) and "Historical rotated OPENAI_API_KEY" in text:
                findings.append({"blob": oid, "path": path, "rule": "historical-credential-waiver"})
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()
    if process.returncode:
        raise RuntimeError("Git history inspection failed")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--ref", default="HEAD")
    args = parser.parse_args()
    findings = audit_history(args.root, args.ref)
    print(json.dumps({"ok": not findings, "findings": findings}, ensure_ascii=False))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
