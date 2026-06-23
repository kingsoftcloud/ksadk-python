"""Shared remote terminal exec command policy."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

SHELL_METACHARS = set("|&;<>()$`\\\n\r")
FORBIDDEN_LAUNCHERS = {
    "bash",
    "sh",
    "zsh",
    "fish",
    "python",
    "python3",
    "node",
    "npx",
    "pnpm",
    "npm",
    "yarn",
    "uv",
    "uvx",
    "hermes",
}

TERMINAL_EXEC_ALLOWLIST_ENV = "KSADK_TERMINAL_EXEC_SUBCOMMAND_ALLOWLIST"

COMMON_REMOTE_EXEC_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("cat",),
    ("date",),
    ("df",),
    ("du",),
    ("find",),
    ("git", "diff"),
    ("git", "log"),
    ("git", "rev-parse"),
    ("git", "show"),
    ("git", "status"),
    ("head",),
    ("id",),
    ("ls",),
    ("ps",),
    ("pwd",),
    ("stat",),
    ("tail",),
    ("uname",),
    ("wc",),
    ("whoami",),
)


@dataclass(frozen=True)
class TerminalExecPolicy:
    name: str
    default_exact: tuple[tuple[str, ...], ...] = ()
    default_prefixes: tuple[tuple[str, ...], ...] = ()
    default_bounded_prefixes: tuple[tuple[tuple[str, ...], int, int], ...] = ()
    env_names: tuple[str, ...] = (TERMINAL_EXEC_ALLOWLIST_ENV,)


GENERIC_TERMINAL_EXEC_POLICY = TerminalExecPolicy(
    name="terminal",
    default_prefixes=COMMON_REMOTE_EXEC_PREFIXES,
)

HERMES_TERMINAL_EXEC_POLICY = TerminalExecPolicy(
    name="Hermes",
    default_exact=(
        ("cron", "list"),
        ("cron", "status"),
        ("config", "check"),
        ("config", "env-path"),
        ("config", "path"),
        ("config", "show"),
        ("doctor",),
        ("gateway", "status"),
        ("insights",),
        ("sessions", "list"),
        ("skills", "audit"),
        ("skills", "check"),
        ("skills", "list"),
        ("status",),
        ("tools", "list"),
        ("version",),
    ),
    default_bounded_prefixes=(
        (("sessions", "export"), 3, 3),
        (("sessions", "show"), 3, 3),
    ),
)

OPENCLAW_TERMINAL_EXEC_POLICY = TerminalExecPolicy(
    name="OpenClaw",
    default_prefixes=(
        *COMMON_REMOTE_EXEC_PREFIXES,
        ("openclaw", "channels", "login"),
    ),
)

_ALLOWLIST_ENTRY_SPLIT_RE = re.compile(r"[\n;,]+")
_ALLOWLIST_TOKEN_SPLIT_RE = re.compile(r"\s+")


def normalize_exec_argv(
    argv: Iterable[str],
    *,
    policy_name: str = "terminal",
    allow_shell_metachars: bool = False,
    allow_forbidden_launchers: bool = False,
) -> list[str]:
    normalized = [str(item).strip() for item in argv]
    if not normalized:
        raise ValueError(f"{policy_name} exec requires argv")
    for index, item in enumerate(normalized):
        if not item:
            raise ValueError(f"{policy_name} exec argv contains an empty argument")
        if not allow_shell_metachars and any(char in SHELL_METACHARS for char in item):
            raise ValueError(f"{policy_name} exec does not allow shell metacharacters: {item}")
        if index == 0 and item.startswith("-"):
            raise ValueError(f"{policy_name} exec command is invalid: {item}")
        if not allow_forbidden_launchers and index == 0 and item in FORBIDDEN_LAUNCHERS:
            raise ValueError(f"{policy_name} exec launcher is not allowed: {item}")
    return normalized


def validate_terminal_exec_argv(
    argv: Iterable[str],
    *,
    policy: TerminalExecPolicy = GENERIC_TERMINAL_EXEC_POLICY,
) -> list[str]:
    if _env_allowlist_all(policy.env_names):
        return normalize_exec_argv(
            argv,
            policy_name=policy.name,
            allow_shell_metachars=True,
            allow_forbidden_launchers=True,
        )

    normalized = normalize_exec_argv(argv, policy_name=policy.name)
    allowed_exact = tuple(tuple(item) for item in policy.default_exact)
    allowed_prefixes = (
        tuple(tuple(item) for item in policy.default_prefixes)
        + _env_allowlist_prefixes(policy.env_names, policy_name=policy.name)
    )

    if _matches_exact_or_prefix(
        normalized,
        allowed_exact=allowed_exact,
        allowed_prefixes=allowed_prefixes,
        allowed_bounded_prefixes=policy.default_bounded_prefixes,
    ):
        return normalized
    suggested_prefix = normalized[0]
    raise ValueError(
        f"{policy.name} exec subcommand is not allowed: {' '.join(normalized)}. "
        f"Set {TERMINAL_EXEC_ALLOWLIST_ENV}='{suggested_prefix}' to allow this prefix, "
        f"or {TERMINAL_EXEC_ALLOWLIST_ENV}='*' to allow all remote exec commands."
    )


def _env_allowlist_prefixes(
    env_names: Sequence[str],
    *,
    policy_name: str,
) -> tuple[tuple[str, ...], ...]:
    prefixes: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for env_name in env_names:
        for prefix in _parse_allowlist_prefixes(os.getenv(env_name), policy_name=policy_name):
            if prefix not in seen:
                prefixes.append(prefix)
                seen.add(prefix)
    return tuple(prefixes)


def _env_allowlist_all(env_names: Sequence[str]) -> bool:
    for env_name in env_names:
        raw = os.getenv(env_name)
        if not raw:
            continue
        for entry in _ALLOWLIST_ENTRY_SPLIT_RE.split(raw):
            if entry.strip() == "*":
                return True
    return False


def _parse_allowlist_prefixes(raw: str | None, *, policy_name: str) -> tuple[tuple[str, ...], ...]:
    if not raw:
        return ()
    prefixes: list[tuple[str, ...]] = []
    for entry in _ALLOWLIST_ENTRY_SPLIT_RE.split(raw):
        entry = entry.strip()
        if not entry:
            continue
        if entry == "*":
            continue
        tokens = tuple(token for token in _ALLOWLIST_TOKEN_SPLIT_RE.split(entry) if token)
        normalize_exec_argv(tokens, policy_name=policy_name)
        prefixes.append(tokens)
    return tuple(prefixes)


def _matches_exact_or_prefix(
    argv: Sequence[str],
    *,
    allowed_exact: Sequence[Sequence[str]],
    allowed_prefixes: Sequence[Sequence[str]],
    allowed_bounded_prefixes: Sequence[tuple[Sequence[str], int, int]],
) -> bool:
    argv_tuple = tuple(argv)
    for item in allowed_exact:
        if argv_tuple == tuple(item):
            return True
    for prefix in allowed_prefixes:
        prefix_tuple = tuple(prefix)
        if prefix_tuple and argv_tuple[: len(prefix_tuple)] == prefix_tuple:
            return True
    for prefix, min_len, max_len in allowed_bounded_prefixes:
        prefix_tuple = tuple(prefix)
        if (
            prefix_tuple
            and argv_tuple[: len(prefix_tuple)] == prefix_tuple
            and len(argv_tuple) >= min_len
            and len(argv_tuple) <= max_len
        ):
            return True
    return False
