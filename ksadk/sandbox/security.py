from __future__ import annotations

import ast
from dataclasses import dataclass, field

from ksadk.sandbox.base import DEFAULT_BLOCKED_PATTERNS, ExecutionConfig, Language

DEFAULT_BASH_BLOCKED_PATTERNS = (
    "rm -rf /",
    "mkfs",
    "curl ",
    "wget ",
    "nc ",
    "ssh ",
)

DEFAULT_JAVASCRIPT_BLOCKED_PATTERNS = (
    "require('child_process')",
    'require("child_process")',
    "child_process.exec(",
    "child_process.spawn(",
)

NETWORK_PATTERNS = {
    Language.PYTHON: (
        "import requests",
        "requests.",
        "urllib.request",
        "urllib.",
        "http.client",
        "socket.",
        "socket(",
        "aiohttp",
    ),
    Language.BASH: (
        "curl ",
        "wget ",
        "nc ",
        "ssh ",
        "python -m http.server",
        "python3 -m http.server",
    ),
    Language.JAVASCRIPT: (
        "fetch(",
        "XMLHttpRequest",
        "require('http')",
        'require("http")',
        "require('https')",
        'require("https")',
    ),
}

PYTHON_NETWORK_IMPORT_PREFIXES = (
    "requests",
    "urllib.request",
    "http.client",
    "socket",
    "aiohttp",
)


@dataclass(slots=True)
class SecurityPolicy:
    blocked_patterns: tuple[str, ...] = field(default_factory=lambda: DEFAULT_BLOCKED_PATTERNS)
    bash_blocked_patterns: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_BASH_BLOCKED_PATTERNS
    )
    javascript_blocked_patterns: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_JAVASCRIPT_BLOCKED_PATTERNS
    )

    def check_safety(
        self,
        code: str,
        language: Language = Language.PYTHON,
        config: ExecutionConfig | None = None,
    ) -> bool:
        return self.validate(code, language=language, config=config) is None

    def validate(
        self,
        code: str,
        language: Language = Language.PYTHON,
        config: ExecutionConfig | None = None,
    ) -> str | None:
        config = config or ExecutionConfig()

        if language is Language.PYTHON:
            violation = self._check_python_imports(code, config)
            if violation:
                return violation

        for pattern in self._iter_patterns(language, config):
            if pattern in code:
                return f"blocked pattern '{pattern}'"

        if not config.allow_network:
            network_pattern = self._check_network_usage(code, language)
            if network_pattern:
                return f"network access is disabled: '{network_pattern}'"

        return None

    def _iter_patterns(self, language: Language, config: ExecutionConfig) -> tuple[str, ...]:
        patterns = list(self.blocked_patterns)
        patterns.extend(config.blocked_patterns)

        if language is Language.BASH:
            patterns.extend(self.bash_blocked_patterns)
        elif language is Language.JAVASCRIPT:
            patterns.extend(self.javascript_blocked_patterns)

        return tuple(dict.fromkeys(patterns))

    def _check_python_imports(self, code: str, config: ExecutionConfig) -> str | None:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return None

        allowed = set(config.allowed_imports or [])
        blocked = set(config.blocked_imports or [])

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else []
            else:
                continue

            for name in names:
                root = name.split(".", 1)[0]
                if blocked and (name in blocked or root in blocked):
                    return f"blocked import '{name}'"
                if allowed and name not in allowed and root not in allowed:
                    return f"import '{name}' is not in allowed_imports"

        return None

    def _check_network_usage(self, code: str, language: Language) -> str | None:
        if language is Language.PYTHON:
            network_import = self._check_python_network_import_usage(code)
            if network_import:
                return network_import

        patterns = list(NETWORK_PATTERNS.get(language, ()))
        if language is Language.BASH:
            patterns.extend(NETWORK_PATTERNS.get(Language.PYTHON, ()))
            patterns.extend(NETWORK_PATTERNS.get(Language.JAVASCRIPT, ()))

        for pattern in dict.fromkeys(patterns):
            if pattern in code:
                return pattern
        return None

    def _check_python_network_import_usage(self, code: str) -> str | None:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return None

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if violation := self._match_python_network_import(alias.name):
                        return violation
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    full_name = f"{module}.{alias.name}" if module else alias.name
                    if violation := self._match_python_network_import(full_name):
                        return violation

        return None

    @staticmethod
    def _match_python_network_import(name: str) -> str | None:
        normalized = name.strip()
        for prefix in PYTHON_NETWORK_IMPORT_PREFIXES:
            if normalized == prefix or normalized.startswith(f"{prefix}."):
                return normalized
        return None
