from __future__ import annotations

import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_BLOCKED_PATTERNS = (
    "os.system",
    "subprocess.call",
    "subprocess.Popen",
    "subprocess.run",
    "shutil.rmtree",
    "os.remove",
    "os.rmdir",
    "__import__('os').system",
    '__import__("os").system',
    "eval(",
    "exec(",
    "open('/etc",
    'open("/etc',
    "open('/proc",
    'open("/proc',
)

DEFAULT_BLOCKED_IMPORTS = (
    "subprocess",
    "socket",
)


class Language(str, Enum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    BASH = "bash"


class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    KILLED = "killed"


@dataclass(slots=True)
class ExecutionResult:
    status: ExecutionStatus
    stdout: str = ""
    stderr: str = ""
    return_value: Any = None
    execution_time_ms: float = 0.0
    files_created: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionConfig:
    timeout_seconds: float = 30.0
    max_memory_mb: int = 256
    max_output_bytes: int = 1_048_576
    allowed_imports: list[str] | None = None
    blocked_imports: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_IMPORTS))
    blocked_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_PATTERNS))
    allow_network: bool = False
    working_dir: str | None = None


LANGUAGE_EXTENSIONS = {
    Language.PYTHON: ".py",
    Language.JAVASCRIPT: ".js",
    Language.BASH: ".sh",
}

EXTENSION_LANGUAGES = {
    ".py": Language.PYTHON,
    ".js": Language.JAVASCRIPT,
    ".sh": Language.BASH,
}


def language_extension(language: Language) -> str:
    return LANGUAGE_EXTENSIONS[language]


def infer_language(file_path: str, default: Language = Language.PYTHON) -> Language:
    return EXTENSION_LANGUAGES.get(Path(file_path).suffix.lower(), default)


class BaseSandbox(ABC):
    def __init__(self, default_config: ExecutionConfig | None = None):
        self.default_config = default_config or ExecutionConfig()

    def resolve_config(self, config: ExecutionConfig | None) -> ExecutionConfig:
        return config or self.default_config

    @abstractmethod
    async def execute(
        self,
        code: str,
        language: Language = Language.PYTHON,
        config: ExecutionConfig | None = None,
    ) -> ExecutionResult:
        raise NotImplementedError

    async def execute_file(
        self,
        file_path: str,
        language: Language | None = None,
        config: ExecutionConfig | None = None,
    ) -> ExecutionResult:
        code = Path(file_path).read_text(encoding="utf-8")
        return await self.execute(code, language or infer_language(file_path), config)

    async def install_package(self, package: str) -> ExecutionResult:
        quoted_package = shlex.quote(package)
        return await self.execute(
            f"python3 -m pip install {quoted_package}",
            language=Language.BASH,
            config=ExecutionConfig(timeout_seconds=max(self.default_config.timeout_seconds, 120.0)),
        )

    async def cleanup(self) -> None:
        return None

    async def __aenter__(self) -> BaseSandbox:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.cleanup()
