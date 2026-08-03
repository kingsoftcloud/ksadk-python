"""Minimal local Agent Harness that executes immutable AgentBundles."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from ksadk.studio.contracts import (
    BuildStatus,
    MCPServerRef,
    ResolvedAgentSpec,
    RunRecord,
    RunStatus,
    ToolContract,
    Usage,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.event_store import RunEventStore
from ksadk.studio.mcp_runtime import MCPRuntimeAdapter
from ksadk.studio.model_client import OpenAICompatibleModelClient, ToolCall
from ksadk.studio.repository import BuildRepository
from ksadk.studio.workspace import Workspace


class ContextManager:
    @staticmethod
    def estimate_tokens(messages: list[dict]) -> int:
        characters = sum(len(str(message.get("content") or "")) for message in messages)
        return max(1, (characters + 3) // 4)

    def fit(
        self,
        messages: list[dict],
        *,
        max_input_tokens: int,
        reserve_output_tokens: int,
        threshold_ratio: float,
        enabled: bool = True,
    ) -> tuple[list[dict], bool]:
        budget = max_input_tokens - reserve_output_tokens
        threshold = int(budget * threshold_ratio)
        if self.estimate_tokens(messages) <= threshold:
            return messages, False
        if not enabled:
            if self.estimate_tokens(messages) > budget:
                raise StudioError(
                    "CONTEXT_BUDGET_EXCEEDED",
                    "当前输入超过 Agent 上下文预算，且上下文压缩已关闭",
                    status_code=422,
                )
            return messages, False
        compacted = list(messages)
        removed = False
        # Keep the system message, the most recent historical message and current input.
        while len(compacted) > 3 and self.estimate_tokens(compacted) > threshold:
            compacted.pop(1)
            removed = True
        if self.estimate_tokens(compacted) > budget:
            raise StudioError(
                "CONTEXT_BUDGET_EXCEEDED",
                "当前输入超过 Agent 上下文预算",
                status_code=422,
            )
        return compacted, removed


class LocalAgentRuntime:
    def __init__(
        self,
        workspace: Workspace,
        *,
        model_client: OpenAICompatibleModelClient | None = None,
        build_repository: BuildRepository | None = None,
        event_store: RunEventStore | None = None,
        mcp_runtime: MCPRuntimeAdapter | None = None,
    ) -> None:
        self.workspace = workspace
        self.model_client = model_client or OpenAICompatibleModelClient()
        self.build_repository = build_repository or BuildRepository(workspace)
        self.event_store = event_store or RunEventStore(workspace)
        self.mcp_runtime = mcp_runtime or MCPRuntimeAdapter(workspace)
        self.context_manager = ContextManager()

    async def run(
        self,
        build_id: str,
        user_input: str,
        *,
        session_id: str | None = None,
    ) -> RunRecord:
        build = self.build_repository.get(build_id)
        if build.status != BuildStatus.SUCCEEDED or not build.artifact_path:
            raise StudioError(
                "BUILD_NOT_READY",
                "只有成功 Build 可以本地运行",
                status_code=409,
                details={"buildId": build_id, "status": build.status},
            )
        resolved = self._load_resolved(build.artifact_path)
        run_id = f"run_{uuid4().hex}"
        session = session_id or f"ses_{uuid4().hex}"
        trace_id = f"trace_{uuid4().hex}"
        record = RunRecord(
            id=run_id,
            build_id=build_id,
            agent_id=resolved.agent_id,
            session_id=session,
            trace_id=trace_id,
            input=user_input,
        )
        self.event_store.create(record)
        self.event_store.append(
            run_id,
            "run.created",
            {"buildId": build_id, "sessionId": session, "traceId": trace_id},
        )
        started = time.monotonic()
        record.status = RunStatus.RUNNING
        record.started_at = datetime.now(timezone.utc)
        self.event_store.save(record)
        self.event_store.append(run_id, "run.started", {})
        try:
            messages = self._messages_for_session(resolved, session, user_input)
            budget = (
                resolved.context.max_input_tokens
                - resolved.context.reserve_output_tokens
            )
            threshold = int(
                budget * resolved.context.compaction.threshold_ratio
            )
            compaction_needed = (
                resolved.context.compaction.enabled
                and self.context_manager.estimate_tokens(messages) > threshold
            )
            if compaction_needed:
                self.event_store.append(
                    run_id,
                    "context.compaction.started",
                    {
                        "messageCount": len(messages),
                        "thresholdTokens": threshold,
                        "tokenEstimate": True,
                    },
                )
            messages, compacted = self.context_manager.fit(
                messages,
                max_input_tokens=resolved.context.max_input_tokens,
                reserve_output_tokens=resolved.context.reserve_output_tokens,
                threshold_ratio=resolved.context.compaction.threshold_ratio,
                enabled=resolved.context.compaction.enabled,
            )
            if compacted:
                self.event_store.append(
                    run_id,
                    "context.compaction.completed",
                    {
                        "messageCount": len(messages),
                        "thresholdTokens": threshold,
                        "tokenEstimate": True,
                    },
                )
            output, usage = await asyncio.wait_for(
                self._execute(resolved, messages, run_id),
                timeout=resolved.execution.timeout_seconds,
            )
            record.output = output
            record.usage = usage
            record.status = RunStatus.COMPLETED
            self.event_store.append(run_id, "message.delta", {"text": output})
            self.event_store.append(
                run_id,
                "run.completed",
                {
                    "output": output,
                    "usage": usage.model_dump(by_alias=True),
                },
            )
        except asyncio.TimeoutError:
            record.status = RunStatus.TIMED_OUT
            record.error = {"code": "RUN_TIMED_OUT", "message": "Agent 运行超时"}
            self.event_store.append(run_id, "run.failed", record.error)
        except asyncio.CancelledError:
            record.status = RunStatus.CANCELLED
            record.error = {"code": "RUN_CANCELLED", "message": "Agent 运行已取消"}
            self.event_store.append(run_id, "run.cancelled", record.error)
        except StudioError as exc:
            record.status = RunStatus.FAILED
            record.error = {"code": exc.code, "message": exc.message}
            self.event_store.append(run_id, "run.failed", record.error)
        finally:
            record.completed_at = datetime.now(timezone.utc)
            record.duration_ms = int((time.monotonic() - started) * 1000)
            self.event_store.save(record)
        return record

    async def _execute(
        self,
        resolved: ResolvedAgentSpec,
        messages: list[dict],
        run_id: str,
    ) -> tuple[str, Usage]:
        tools = [self._openai_tool(tool) for tool in resolved.capabilities.tools]
        total = Usage()
        if resolved.execution.strategy == "plan-act-observe":
            self.event_store.append(
                run_id,
                "plan.created",
                {
                    "strategy": resolved.execution.strategy,
                    "maxSteps": resolved.execution.max_steps,
                },
            )
        for step in range(1, resolved.execution.max_steps + 1):
            self.event_store.append(
                run_id,
                "model.requested",
                {
                    "model": resolved.model.model,
                    "messageCount": len(messages),
                    "step": step,
                },
            )
            response = await self.model_client.complete(
                resolved.model,
                messages=messages,
                network_policy=resolved.security.network,
                timeout_seconds=resolved.execution.timeout_seconds,
                max_attempts=resolved.execution.retry.max_attempts,
                backoff_seconds=resolved.execution.retry.backoff_seconds,
                tools=tools or None,
            )
            total = Usage(
                input_tokens=total.input_tokens + response.usage.input_tokens,
                output_tokens=total.output_tokens + response.usage.output_tokens,
                total_tokens=total.total_tokens + response.usage.total_tokens,
            )
            self.event_store.append(
                run_id,
                "model.completed",
                {
                    "finishReason": response.finish_reason,
                    "usage": response.usage.model_dump(by_alias=True),
                    "step": step,
                },
            )
            if not response.tool_calls:
                return response.content, total
            messages.append(response.raw_message)
            for call in response.tool_calls:
                self.event_store.append(
                    run_id,
                    "tool.requested",
                    {"toolCallId": call.id, "tool": call.name},
                )
                result = await self._execute_tool(call, resolved)
                self._validate_tool_output(call.name, result, resolved)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(result, ensure_ascii=False, sort_keys=True),
                    }
                )
                self.event_store.append(
                    run_id,
                    "tool.completed",
                    {"toolCallId": call.id, "tool": call.name, "status": "succeeded"},
                )
        raise StudioError(
            "AGENT_MAX_STEPS_EXCEEDED",
            "Agent 超过最大执行步数",
            status_code=422,
        )

    def _messages_for_session(
        self,
        resolved: ResolvedAgentSpec,
        session_id: str,
        user_input: str,
    ) -> list[dict]:
        system = resolved.instructions.system
        if resolved.instructions.task:
            system = f"{system.rstrip()}\n\nTask contract:\n{resolved.instructions.task}"
        skill_instructions = [
            str(skill.get("instructions") or "").strip()
            for skill in resolved.capabilities.skills
            if str(skill.get("instructions") or "").strip()
        ]
        if skill_instructions:
            system = (
                f"{system.rstrip()}\n\nInstalled skills:\n"
                + "\n\n".join(skill_instructions)
            )
        messages: list[dict] = [{"role": "system", "content": system}]
        for previous in self.event_store.list_runs(session_id=session_id):
            if previous.status == RunStatus.COMPLETED and previous.build_id:
                messages.append({"role": "user", "content": previous.input})
                messages.append({"role": "assistant", "content": previous.output})
        messages.append({"role": "user", "content": user_input})
        return messages

    def _load_resolved(self, artifact_path: str) -> ResolvedAgentSpec:
        archive = self.workspace.resolve(artifact_path, must_exist=True)
        path = archive.parent / "agent-bundle" / "resolved-agent-spec.json"
        try:
            return cast(
                ResolvedAgentSpec,
                ResolvedAgentSpec.model_validate_json(path.read_text(encoding="utf-8")),
            )
        except (OSError, ValueError) as exc:
            raise StudioError(
                "BUILD_ARTIFACT_INVALID",
                "Build 缺少有效的 ResolvedAgentSpec",
                status_code=500,
            ) from exc

    @staticmethod
    def _openai_tool(tool: ToolContract) -> dict:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }

    async def _execute_tool(
        self,
        call: ToolCall,
        resolved: ResolvedAgentSpec,
    ) -> dict[str, Any]:
        contracts = resolved.capabilities.tools
        contract = next((item for item in contracts if item.name == call.name), None)
        if contract is None:
            raise StudioError(
                "TOOL_CONTRACT_NOT_FOUND",
                "模型请求了未声明的 Tool",
                status_code=422,
                details={"tool": call.name},
            )
        try:
            arguments = cast(dict[str, Any], json.loads(call.arguments or "{}"))
        except ValueError as exc:
            raise StudioError(
                "TOOL_ARGUMENTS_INVALID",
                "Tool arguments 不是合法 JSON",
                status_code=422,
                details={"tool": call.name},
            ) from exc
        argument_errors = sorted(
            Draft202012Validator(contract.input_schema).iter_errors(arguments),
            key=lambda error: list(error.absolute_path),
        )
        if argument_errors:
            first = argument_errors[0]
            raise StudioError(
                "TOOL_ARGUMENTS_INVALID",
                "Tool arguments 不满足声明的 JSON Schema",
                status_code=422,
                details={
                    "tool": call.name,
                    "path": list(first.absolute_path),
                    "reason": first.message,
                },
            )
        missing_permissions = sorted(
            set(contract.permissions) - set(resolved.security.allowed_permissions)
        )
        if missing_permissions:
            raise StudioError(
                "TOOL_PERMISSION_DENIED",
                "Runtime 拒绝未授权 Tool",
                status_code=403,
                details={"tool": call.name, "permissions": missing_permissions},
            )
        if contract.approval in {"always", "policy"}:
            raise StudioError(
                "TOOL_APPROVAL_REQUIRED",
                "Tool 调用需要人工或策略审批",
                status_code=409,
                details={"tool": call.name, "approval": contract.approval},
            )
        if contract.executor == "mcp":
            raw_server = next(
                (
                    item
                    for item in resolved.capabilities.mcp_servers
                    if item.get("name") == contract.mcp_server
                ),
                None,
            )
            if raw_server is None:
                raise StudioError(
                    "CAPABILITY_UNRESOLVED",
                    "MCP Tool 的 Server 未解析",
                    status_code=422,
                    details={"tool": call.name, "server": contract.mcp_server},
                )
            server = MCPServerRef.model_validate(raw_server)
            return await self.mcp_runtime.call(
                server,
                tool_name=call.name,
                arguments=arguments,
                timeout_seconds=contract.timeout_seconds,
            )
        if call.name == "builtin.echo":
            return arguments
        if call.name == "builtin.current_time":
            timezone_name = str(arguments.get("timezone") or "UTC")
            try:
                zone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError as exc:
                raise StudioError(
                    "TOOL_ARGUMENTS_INVALID",
                    "未知时区",
                    status_code=422,
                    details={"timezone": timezone_name},
                ) from exc
            return {
                "iso8601": datetime.now(zone).isoformat(),
                "timezone": timezone_name,
            }
        if call.name == "workspace.read":
            path = self._workspace_file(arguments, must_exist=True)
            max_chars = int(arguments.get("maxChars") or 50000)
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise StudioError(
                    "TOOL_FILE_ENCODING_UNSUPPORTED",
                    "Read 只支持 UTF-8 文本文件",
                    status_code=422,
                    details={"path": self.workspace.relative(path)},
                ) from exc
            return {
                "path": self.workspace.relative(path),
                "content": content[:max_chars],
                "truncated": len(content) > max_chars,
            }
        if call.name == "workspace.write":
            path = self._workspace_file(arguments)
            content = str(arguments["content"])
            if len(content.encode("utf-8")) > 1_000_000:
                raise StudioError(
                    "TOOL_OUTPUT_TOO_LARGE",
                    "Write 单次写入不能超过 1MB",
                    status_code=422,
                )
            self.workspace.atomic_write_text(path, content)
            return {
                "path": self.workspace.relative(path),
                "bytesWritten": len(content.encode("utf-8")),
            }
        if call.name == "workspace.edit":
            path = self._workspace_file(arguments, must_exist=True)
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise StudioError(
                    "TOOL_FILE_ENCODING_UNSUPPORTED",
                    "Edit 只支持 UTF-8 文本文件",
                    status_code=422,
                    details={"path": self.workspace.relative(path)},
                ) from exc
            old = str(arguments["oldText"])
            occurrences = content.count(old)
            if occurrences == 0:
                raise StudioError(
                    "TOOL_EDIT_TARGET_NOT_FOUND",
                    "Edit 未找到要替换的文本",
                    status_code=409,
                    details={"path": self.workspace.relative(path)},
                )
            replace_all = bool(arguments.get("replaceAll"))
            replacements = occurrences if replace_all else 1
            updated = content.replace(
                old,
                str(arguments["newText"]),
                -1 if replace_all else 1,
            )
            self.workspace.atomic_write_text(path, updated)
            return {
                "path": self.workspace.relative(path),
                "replacements": replacements,
            }
        if call.name == "workspace.glob":
            pattern = self._workspace_pattern(str(arguments["pattern"]))
            limit = int(arguments.get("limit") or 200)
            glob_matches = self._glob_files(pattern, limit=limit + 1)
            return {
                "matches": glob_matches[:limit],
                "truncated": len(glob_matches) > limit,
            }
        if call.name == "workspace.grep":
            pattern = self._workspace_pattern(
                str(arguments.get("filePattern") or "**/*")
            )
            query = str(arguments["query"])
            limit = int(arguments.get("limit") or 200)
            grep_matches: list[dict[str, Any]] = []
            for relative in self._glob_files(pattern, limit=10_000):
                path = self.workspace.resolve(relative, must_exist=True)
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (UnicodeDecodeError, OSError):
                    continue
                for line_number, line in enumerate(lines, start=1):
                    if query in line:
                        grep_matches.append(
                            {
                                "path": relative,
                                "line": line_number,
                                "text": line[:1000],
                            }
                        )
                        if len(grep_matches) > limit:
                            break
                if len(grep_matches) > limit:
                    break
            return {
                "matches": grep_matches[:limit],
                "truncated": len(grep_matches) > limit,
            }
        if contract.executor == "deferred":
            raise StudioError(
                "TOOL_RESULT_REQUIRED",
                "自定义 Tool 需要会话客户端提交执行结果",
                status_code=409,
                details={"tool": call.name, "toolCallId": call.id},
            )
        raise StudioError(
            "TOOL_RUNTIME_UNAVAILABLE",
            "Tool 没有可用的本地执行器",
            status_code=501,
            details={"tool": call.name},
        )

    @staticmethod
    def _validate_tool_output(
        tool_name: str,
        result: dict[str, Any],
        resolved: ResolvedAgentSpec,
    ) -> None:
        contract = next(
            (item for item in resolved.capabilities.tools if item.name == tool_name),
            None,
        )
        if contract is None:
            return
        errors = sorted(
            Draft202012Validator(contract.output_schema).iter_errors(result),
            key=lambda error: list(error.absolute_path),
        )
        if not errors:
            return
        first = errors[0]
        raise StudioError(
            "TOOL_OUTPUT_INVALID",
            "Tool 输出不满足声明的 JSON Schema",
            status_code=502,
            details={
                "tool": tool_name,
                "path": list(first.absolute_path),
                "reason": first.message,
            },
        )

    def _workspace_file(
        self,
        arguments: dict[str, Any],
        *,
        must_exist: bool = False,
    ) -> Path:
        raw = str(arguments.get("path") or "")
        path = PurePath(raw)
        if not raw or path.is_absolute() or ".." in path.parts:
            raise StudioError(
                "WORKSPACE_PATH_FORBIDDEN",
                "Tool path 必须是工作区内的相对路径",
                status_code=403,
                details={"path": raw},
            )
        if path.parts and path.parts[0] == ".agentkit":
            raise StudioError(
                "WORKSPACE_PATH_FORBIDDEN",
                "Tool 不能访问 AgentKit 内部状态目录",
                status_code=403,
                details={"path": raw},
            )
        target = self.workspace.resolve(Path(*path.parts), must_exist=must_exist)
        if must_exist and not target.is_file():
            raise StudioError(
                "TOOL_FILE_NOT_FOUND",
                "Tool 目标文件不存在",
                status_code=404,
                details={"path": raw},
            )
        return target

    @staticmethod
    def _workspace_pattern(pattern: str) -> str:
        path = PurePath(pattern)
        if (
            not pattern
            or path.is_absolute()
            or ".." in path.parts
            or (path.parts and path.parts[0] == ".agentkit")
        ):
            raise StudioError(
                "WORKSPACE_PATH_FORBIDDEN",
                "Tool pattern 必须限制在工作区内",
                status_code=403,
                details={"pattern": pattern},
            )
        return pattern

    def _glob_files(self, pattern: str, *, limit: int) -> list[str]:
        matches: list[str] = []
        for path in self.workspace.root.glob(pattern):
            if len(matches) >= limit:
                break
            if not path.is_file() or path.is_symlink():
                continue
            try:
                relative = self.workspace.relative(path)
            except StudioError:
                continue
            if relative.startswith(".agentkit/"):
                continue
            matches.append(relative)
        return sorted(set(matches))
