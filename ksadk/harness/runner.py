"""Runner that composes model reasoning with real MCP and sandbox tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict

from ksadk.harness.config import HarnessConfig
from ksadk.harness.reasoner import (
    HarnessReasoner,
    HarnessReasoningTurn,
    HarnessToolCall,
    LiteLLMHarnessReasoner,
)
from ksadk.harness.sandbox import HarnessSandboxExecutor
from ksadk.harness.tools import HarnessTool, load_mcp_tools, sandbox_tools, tool_result_text
from ksadk.runners.base_runner import BaseRunner

_MAX_REASONING_TURNS = 8


class YamlAgentRunner(BaseRunner):
    """Execute one Harness config through the shared runner/runtime contract."""

    def __init__(
        self,
        config: HarnessConfig,
        *,
        agent_name: str = "harness-agent",
        reasoner: HarnessReasoner | None = None,
        workspace_root: str | Path = ".",
    ) -> None:
        super().__init__(
            detection_result=SimpleNamespace(
                name=agent_name,
                type=SimpleNamespace(value="harness"),
            ),
            project_dir=str(workspace_root),
        )
        self._config = config
        self._reasoner = reasoner or LiteLLMHarnessReasoner()
        self._sandbox = HarnessSandboxExecutor(
            workspace_root=workspace_root,
            read_only=config.sandbox.read_only,
        )
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._tools: tuple[HarnessTool, ...] | None = None
        self._mcp_toolsets: list[Any] = []

    @property
    def harness_config(self) -> HarnessConfig:
        return self._config

    @property
    def sandbox(self) -> HarnessSandboxExecutor:
        return self._sandbox

    @property
    def workspace_root(self) -> Path:
        return self._sandbox.workspace_root

    def load_agent(self) -> None:
        # MCP discovery is async and therefore happens lazily on first invocation.
        self._loaded = True

    def prepare_for_request(self, model: str | None) -> None:
        # Request model is carried in input_data. Do not mutate process-wide model env.
        del model

    def _effective(self, input_data: Dict[str, Any]) -> tuple[str, str]:
        metadata = input_data.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        model = str(
            metadata.get("model_override") or input_data.get("model") or self._config.model
        ).strip()
        prompt = str(
            metadata.get("prompt_override") or input_data.get("instructions") or self._config.prompt
        ).strip()
        return model, prompt

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        tools = await self._ensure_tools()
        model, prompt = self._effective(input_data)
        user_input = str(input_data.get("input") or "")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_input},
        ]
        execution_log: list[dict[str, Any]] = []

        for _turn_number in range(_MAX_REASONING_TURNS):
            turn = await self._reasoner.complete(
                model=model,
                prompt=prompt,
                messages=tuple(messages),
                tools=tools,
            )
            if turn.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": turn.final_text,
                        "tool_calls": [
                            {
                                "id": call.call_id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                                },
                            }
                            for call in turn.tool_calls
                        ],
                    }
                )
                for call in turn.tool_calls:
                    tool = next((item for item in tools if item.name == call.name), None)
                    if tool is None:
                        raise RuntimeError(
                            f"Harness tool {call.name!r} is not available; it may be filtered "
                            "or was not exposed by its MCP server"
                        )
                    result = await tool.call(call.arguments, call_id=call.call_id)
                    content = tool_result_text(result)
                    execution_log.append(
                        {
                            "call_id": call.call_id,
                            "name": call.name,
                            "source": tool.source,
                            "result": result,
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "name": call.name,
                            "content": content,
                        }
                    )
                continue
            if turn.final_text is None:
                raise RuntimeError(
                    "Harness reasoner returned neither a final response nor a tool call"
                )
            return {
                "output": turn.final_text,
                "model": model,
                "prompt": prompt,
                "tool_calls": execution_log,
                "sandbox_read_only": self._config.sandbox.read_only,
            }
        raise RuntimeError(f"Harness reasoning exceeded {_MAX_REASONING_TURNS} turns")

    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        result = await self.invoke(input_data)
        yield {"delta": result["output"], "type": "text"}
        yield {
            "output": result["output"],
            "type": "final",
            "model": result["model"],
            "tool_calls": result["tool_calls"],
            "sandbox_read_only": result["sandbox_read_only"],
        }

    async def close(self) -> None:
        toolsets, self._mcp_toolsets = self._mcp_toolsets, []
        self._tools = None
        if toolsets:
            await asyncio.gather(*(toolset.close() for toolset in toolsets), return_exceptions=True)

    async def _ensure_tools(self) -> tuple[HarnessTool, ...]:
        if self._tools is not None:
            return self._tools
        async with self._load_lock:
            if self._tools is not None:
                return self._tools
            tools = sandbox_tools(self._sandbox)
            try:
                for spec in self._config.mcp_tools:
                    toolset, discovered = await load_mcp_tools(spec)
                    self._mcp_toolsets.append(toolset)
                    tools.extend(discovered)
                duplicates = sorted(
                    {
                        tool.name
                        for tool in tools
                        if sum(item.name == tool.name for item in tools) > 1
                    }
                )
                if duplicates:
                    raise RuntimeError(f"Harness tool names are not unique: {duplicates}")
            except Exception:
                await self.close()
                raise
            self._tools = tuple(tools)
            return self._tools


__all__ = [
    "HarnessReasoner",
    "HarnessReasoningTurn",
    "HarnessToolCall",
    "LiteLLMHarnessReasoner",
    "YamlAgentRunner",
]
