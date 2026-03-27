from __future__ import annotations

from ksadk.sandbox.base import (
    BaseSandbox,
    ExecutionConfig,
    ExecutionResult,
    ExecutionStatus,
    Language,
)


class SandboxToolset:
    def __init__(self, sandbox: BaseSandbox):
        self.sandbox = sandbox

    async def execute_python(self, code: str, config: ExecutionConfig | None = None) -> str:
        return await self._execute(code, Language.PYTHON, config)

    async def execute_bash(self, command: str, config: ExecutionConfig | None = None) -> str:
        return await self._execute(command, Language.BASH, config)

    async def execute_javascript(self, code: str, config: ExecutionConfig | None = None) -> str:
        return await self._execute(code, Language.JAVASCRIPT, config)

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
        config: ExecutionConfig | None,
    ) -> str:
        result = await self.sandbox.execute(code, language=language, config=config)
        return self._format_result(result)

    def _format_result(self, result: ExecutionResult) -> str:
        if result.status is ExecutionStatus.SUCCESS:
            return result.stdout or "(no output)"

        detail = result.stderr or result.stdout or "Execution failed without output."
        return f"Error ({result.status.value}): {detail}"
