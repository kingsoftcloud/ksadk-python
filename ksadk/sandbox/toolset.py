from __future__ import annotations

from typing import Any, Optional

from ksadk.sandbox.base import (
    BaseSandbox,
    ExecutionConfig,
    ExecutionResult,
    ExecutionStatus,
    Language,
)


class SandboxToolset:
    _CONFIG_KEYS = frozenset(ExecutionConfig.__dataclass_fields__.keys())

    def __init__(self, sandbox: BaseSandbox):
        self.sandbox = sandbox

    async def execute_python(self, code: str, config: Optional[dict[str, Any]] = None) -> str:
        """Execute Python code in the sandbox.

        Args:
            code: Python source code to run.
            config: Optional JSON object with execution options.
                Supported keys: timeout_seconds, max_memory_mb, max_output_bytes,
                allowed_imports, blocked_imports, blocked_patterns,
                allow_network, working_dir.
        """
        return await self._execute(code, Language.PYTHON, self._coerce_config(config))

    async def execute_bash(self, command: str, config: Optional[dict[str, Any]] = None) -> str:
        """Execute a bash command in the sandbox.

        Args:
            command: Bash command or shell script to run.
            config: Optional JSON object with execution options.
                Supported keys: timeout_seconds, max_memory_mb, max_output_bytes,
                allowed_imports, blocked_imports, blocked_patterns,
                allow_network, working_dir.
        """
        return await self._execute(command, Language.BASH, self._coerce_config(config))

    async def execute_javascript(self, code: str, config: Optional[dict[str, Any]] = None) -> str:
        """Execute JavaScript code in the sandbox.

        Args:
            code: JavaScript source code to run.
            config: Optional JSON object with execution options.
                Supported keys: timeout_seconds, max_memory_mb, max_output_bytes,
                allowed_imports, blocked_imports, blocked_patterns,
                allow_network, working_dir.
        """
        return await self._execute(code, Language.JAVASCRIPT, self._coerce_config(config))

    def get_tools(self) -> list:
        return [
            self.execute_python,
            self.execute_bash,
            self.execute_javascript,
        ]

    async def _execute(
        self,
        code: str,
        language: Language,
        config: Optional[ExecutionConfig],
    ) -> str:
        result = await self.sandbox.execute(code, language=language, config=config)
        return self._format_result(result)

    def _coerce_config(self, config: Any) -> Optional[ExecutionConfig]:
        if config is None:
            return None
        if isinstance(config, ExecutionConfig):
            return config
        if not isinstance(config, dict):
            raise TypeError("config must be a JSON object or ExecutionConfig")

        unknown_keys = sorted(set(config) - self._CONFIG_KEYS)
        if unknown_keys:
            raise ValueError(
                "Unsupported config keys: " + ", ".join(unknown_keys)
            )

        payload = {
            key: value
            for key, value in config.items()
            if value is not None and key in self._CONFIG_KEYS
        }
        return ExecutionConfig(**payload)

    def _format_result(self, result: ExecutionResult) -> str:
        if result.status is ExecutionStatus.SUCCESS:
            return result.stdout or "(no output)"

        detail = result.stderr or result.stdout or "Execution failed without output."
        return f"Error ({result.status.value}): {detail}"
