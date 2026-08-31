"""Secret and local-path admission for immutable Agent Bundles.

This is deliberately narrower than a generic source-code linter.  A Bundle
may legitimately contain documentation and examples, so we inspect all
structured deployment inputs and only use high-confidence token signatures in
runtime source files.  The result is suitable for a build/deployment boundary:
it prevents a resolved Bundle from becoming a durable copy of a credential
without treating every mention of the word ``token`` as a secret.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:password|passwd|api[_-]?key|access[_-]?key|secret[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|credential|private[_-]?key)(?:[_-]?ref)?$",
    re.IGNORECASE,
)
_REFERENCE_PREFIXES = ("env://", "secret://", "keyring://", "vault://")
_HIGH_CONFIDENCE_TOKENS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}")),
)
_STRUCTURED_SUFFIXES = frozenset({".json", ".yaml", ".yml"})
_RUNTIME_SOURCE_SUFFIXES = frozenset({".py", ".js", ".mjs", ".cjs", ".ts", ".tsx"})


@dataclass(frozen=True)
class BundleSecurityFinding:
    """One deterministic admission finding without leaking the matched value."""

    path: str
    kind: str
    field: str | None = None


class BundleSecurityError(ValueError):
    """Raised when a Bundle would persist a literal credential or host path."""

    code = "bundle_secret_detected"

    def __init__(self, findings: tuple[BundleSecurityFinding, ...]) -> None:
        self.findings = findings
        locations = ", ".join(
            f"{finding.path}{f' ({finding.field})' if finding.field else ''}"
            for finding in findings[:3]
        )
        suffix = " …" if len(findings) > 3 else ""
        super().__init__(f"Agent Bundle contains literal secret material: {locations}{suffix}")


def scan_bundle_security(root: str | Path) -> tuple[BundleSecurityFinding, ...]:
    """Return high-confidence security findings for an already materialized Bundle.

    Structured files receive semantic key/value inspection. Runtime source is
    only searched for credential signatures; user Markdown and arbitrary
    package assets are intentionally outside this detector to avoid false
    positive build failures.
    """

    bundle_root = Path(root).resolve()
    findings: list[BundleSecurityFinding] = []
    for path in sorted(
        (candidate for candidate in bundle_root.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(bundle_root).as_posix(),
    ):
        relative = path.relative_to(bundle_root).as_posix()
        suffix = path.suffix.lower()
        if suffix not in _STRUCTURED_SUFFIXES and not (
            relative.startswith("runtime/") and suffix in _RUNTIME_SOURCE_SUFFIXES
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if suffix == ".json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                findings.append(BundleSecurityFinding(relative, "invalid-structured-input"))
                continue
            _scan_value(payload, relative, "$", findings)
        elif suffix in {".yaml", ".yml"}:
            # Bundle-owned YAML is a narrow runtime launch declaration.  It
            # has no credential-bearing fields today, but token patterns still
            # catch accidental inline credentials without introducing a second
            # YAML parser/security grammar.
            _scan_text(text, relative, findings)
        else:
            _scan_text(text, relative, findings)
    return tuple(findings)


def assert_bundle_security(root: str | Path) -> None:
    findings = scan_bundle_security(root)
    if findings:
        raise BundleSecurityError(findings)


def _scan_value(
    value: Any,
    relative: str,
    field: str,
    findings: list[BundleSecurityFinding],
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_field = f"{field}.{key_text}"
            normalized = key_text.replace("_", "").replace("-", "").lower()
            # Metadata that *names* secret fields is not a secret value.
            if normalized in {"secretfields", "credentialfields"}:
                continue
            if _SENSITIVE_KEY.search(key_text) and _has_literal_secret(child):
                findings.append(
                    BundleSecurityFinding(relative, "literal-secret-field", child_field)
                )
            _scan_value(child, relative, child_field, findings)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _scan_value(child, relative, f"{field}[{index}]", findings)
        return
    if isinstance(value, str):
        _scan_text(value, relative, findings, field=field)
        split = urlsplit(value)
        # ``plugin://name@version`` is a pinned package reference, not URL
        # userinfo. Only network endpoint schemes can carry credentials here.
        if split.scheme in {"http", "https"} and (
            split.username is not None or split.password is not None
        ):
            findings.append(BundleSecurityFinding(relative, "url-credentials", field))
        if value.startswith(("/Users/", "/home/", "C:\\Users\\")):
            findings.append(BundleSecurityFinding(relative, "local-home-path", field))


def _has_literal_secret(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not value.startswith(_REFERENCE_PREFIXES)


def _scan_text(
    text: str,
    relative: str,
    findings: list[BundleSecurityFinding],
    *,
    field: str | None = None,
) -> None:
    for kind, pattern in _HIGH_CONFIDENCE_TOKENS:
        if pattern.search(text):
            findings.append(BundleSecurityFinding(relative, kind, field))


__all__ = [
    "BundleSecurityError",
    "BundleSecurityFinding",
    "assert_bundle_security",
    "scan_bundle_security",
]
