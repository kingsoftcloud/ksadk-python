"""Trusted Studio evidence for an explicitly requested Python test action.

The host freezes a test plan before a Native turn. Verification reads committed
tool intents and receipts, never model claims or compacted conversation text.
Passing the action proves that tests ran after observed edits; it does not prove
the user's broader coding goal.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path

from ksadk.harness.native.adapters import StrictToolExecutor, ToolAuthorizer, ToolBinding
from ksadk.harness.native.completion import (
    AcceptanceItem,
    TaskContract,
    VerificationItemResult,
    VerificationReport,
)
from ksadk.harness.native.model import ModelToolCall
from ksadk.harness.native.ports import (
    RunConfiguration,
    ToolDefinition,
    ToolPort,
    ToolRequest,
    ToolResult,
    VerificationBundle,
    VerificationPort,
    VerificationRequest,
)
from ksadk.harness.native.state import RunScope
from ksadk.harness.native.tool_recovery import query_sealed_tool_result
from ksadk.harness.stores.native_contract import RunStore

TEST_ACTION_ID = "requested_pytest_action"
TEST_ACTION_VERIFIER = "studio.committed-pytest-receipt"
TEST_ACTION_VERSION = "7"
TEST_ACTION_TOOL_NAME = "run_required_tests"
TEST_ACTION_TOOL = ToolDefinition(
    name=TEST_ACTION_TOOL_NAME,
    description=(
        "Run the Python tests frozen in the current user task contract inside the bound "
        "workspace. This tool takes no arguments, reports the actual pytest exit status, "
        "and can be called again after repairs."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)
_TEST_REQUEST = re.compile(
    r"\b(?:run|execute)\s+(?:(?:the|all|supplied|public|existing|provided)\s+){0,4}"
    r"(?:tests?|test\s+suite)\b"
    r"|\b(?:run|execute)\s+(?:(?:uv\s+run|python(?:3)?\s+-m)\s+)?pytest\b"
    r"|(?:运行|执行)\s*(?:(?:uv\s+run|python(?:3)?\s+-m)\s+)?pytest\b"
    r"|(?:运行|执行)(?:(?:已有|提供的|公开|全部)的?)?测试",
    re.IGNORECASE,
)
_TEST_LAUNCHERS = (
    ("uv", "run", "python3", "-m", "pytest"),
    ("uv", "run", "python", "-m", "pytest"),
    ("uv", "run", "pytest"),
    ("python3", "-m", "pytest"),
    ("python", "-m", "pytest"),
    ("pytest",),
)


def _requested_test_launcher(goal: str) -> tuple[str, ...]:
    for launcher in _TEST_LAUNCHERS:
        pattern = r"(?<![\w/])" + r"\s+".join(map(re.escape, launcher)) + r"\b"
        if re.search(pattern, goal):
            return launcher
    return ("python", "-m", "pytest")


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _discover_python_tests(root: Path) -> tuple[str, ...]:
    """Only inspect conventional local test locations; never execute discovery."""
    candidates = (*root.glob("test_*.py"), *((root / "tests").glob("test_*.py")))
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in candidates
            if path.is_file() and not path.is_symlink()
        )
    )


def extend_with_test_action(
    *, goal: str, contract: TaskContract, workspace_root: Path, has_command_tool: bool
) -> TaskContract:
    """Add a host-observable action only for an explicit, executable request."""
    if not has_command_tool or _TEST_REQUEST.search(goal) is None:
        return contract
    paths = _discover_python_tests(workspace_root)
    if not paths or any(item.acceptance_id == TEST_ACTION_ID for item in contract.items):
        return contract
    allowed = contract.allowed_tool_names
    if allowed is not None and not {"run_command", TEST_ACTION_TOOL_NAME}.intersection(allowed):
        raise ValueError("native_test_action_tool_out_of_task_scope")
    plan = {"kind": "pytest", "paths": paths}
    launcher = _requested_test_launcher(goal)
    if launcher != ("python", "-m", "pytest"):
        plan["launcher"] = launcher
    description = json.dumps(plan, sort_keys=True)
    item = AcceptanceItem(
        acceptance_id=TEST_ACTION_ID,
        verifier_ref=TEST_ACTION_VERIFIER,
        verifier_version=TEST_ACTION_VERSION,
        verification_method="deterministic",
        description=description,
    )
    revision = _digest(f"{contract.revision}\0{TEST_ACTION_VERSION}\0{description}")[7:31]
    return contract.model_copy(
        update={
            "revision": f"{contract.revision}+test-{revision}",
            "items": (*contract.items, item),
        }
    )


def bind_test_action_scope(
    contract: TaskContract, *, bound_tool_names: tuple[str, ...],
) -> TaskContract:
    """Expose the reserved action only when this goal has its trusted binding.

    Registration is activation-wide, but the frozen test plan is per goal.
    Apply the existing task tool scope to both model projection and dispatch;
    never broaden a scope supplied by another host policy.
    """
    if TEST_ACTION_TOOL_NAME not in bound_tool_names or _test_action_item(contract) is not None:
        return contract
    allowed = contract.allowed_tool_names
    if allowed is not None and TEST_ACTION_TOOL_NAME not in allowed:
        return contract
    scoped = tuple(name for name in (allowed if allowed is not None else bound_tool_names)
                   if name != TEST_ACTION_TOOL_NAME)
    revision = _digest(f"{contract.revision}\0test-action-scope/v1\0{scoped}")[7:31]
    return contract.model_copy(update={
        "allowed_tool_names": scoped, "revision": f"{contract.revision}+test-scope-{revision}",
    })


def _locked_root_cwd(cwd: object, workspace_root: Path | None) -> bool:
    if cwd in (None, ""):
        return True
    if workspace_root is None or not isinstance(cwd, str):
        return False
    raw = Path(cwd)
    target = raw if raw.is_absolute() else workspace_root / raw
    return target.resolve() == workspace_root.resolve()


def _pytest_command_scope(
    command: str, required_paths: tuple[str, ...], *, workspace_root: Path | None = None
) -> str | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return None
    if not words or any(word in {";", "||", "|", "&"} for word in words):
        return None
    segments: list[list[str]] = [[]]
    for word in words:
        if word == "&&":
            if not segments[-1]:
                return None
            segments.append([])
        else:
            segments[-1].append(word)
    # A successful pytest in another directory does not cover the frozen
    # workspace tests. A cd prelude is acceptable only if it resolves to the
    # host-locked root; the tool cwd is checked separately below.
    if not segments[-1]:
        return None
    for segment in segments[:-1]:
        if segment == ["pwd"]:
            continue
        if (
            len(segment) != 2
            or segment[0] != "cd"
            or not _locked_root_cwd(segment[1], workspace_root)
        ):
            return None
    command_words = segments[-1]
    if command_words[:2] == ["uv", "run"]:
        command_words = command_words[2:]
    executable = Path(command_words[0]).name if command_words else ""
    if executable in {"python", "python3"} and command_words[1:3] == ["-m", "pytest"]:
        arguments = command_words[3:]
    elif executable == "pytest":
        arguments = command_words[1:]
    else:
        return None
    output_flags = {"-q", "-v", "-s", "--disable-warnings"}
    if any(word.startswith("-") and word not in output_flags for word in arguments):
        return None
    paths = tuple(Path(word).as_posix() for word in arguments if word not in output_flags)
    if not paths:
        return "all_discovered"
    if len(paths) == len(required_paths) and set(paths) == set(required_paths):
        return "all_required_paths"
    return None


def _test_action_item(contract: TaskContract) -> AcceptanceItem | None:
    return next((item for item in contract.items if item.acceptance_id == TEST_ACTION_ID), None)


def _required_paths(item: AcceptanceItem) -> tuple[str, ...]:
    value = json.loads(item.description)
    paths = value.get("paths") if isinstance(value, dict) else None
    if (
        not isinstance(paths, list)
        or not paths
        or any(
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            for path in paths
        )
    ):
        raise ValueError("native_test_action_plan_invalid")
    return tuple(paths)


class StudioNativeRequiredTestsPort:
    """A typed, authorized test action backed by the Build-locked command tool."""

    def __init__(
        self,
        *,
        delegate: ToolPort,
        scope: RunScope,
        store: RunStore,
        command_binding: ToolBinding,
        authorize: ToolAuthorizer,
    ) -> None:
        self._delegate = delegate
        self._scope = scope.model_copy(deep=True)
        self._store = store
        self._command_binding = command_binding
        self._executor = StrictToolExecutor(
            scope=scope,
            tools=(ToolBinding(TEST_ACTION_TOOL, self._run),),
            authorize=authorize,
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        if request.call.name != TEST_ACTION_TOOL_NAME:
            return await self._delegate.execute(request)
        return await self._executor.execute(request)

    async def reconcile(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self._delegate, request, owned_names=(TEST_ACTION_TOOL_NAME,)
        )

    async def recover_prepared(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self._delegate, request, owned_names=(TEST_ACTION_TOOL_NAME,), method="recover_prepared"
        )

    async def resume_prepared(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self._delegate, request, owned_names=(TEST_ACTION_TOOL_NAME,), method="resume_prepared"
        )

    async def resume_checkpoint(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self._delegate, request, owned_names=(TEST_ACTION_TOOL_NAME,),
            method="resume_checkpoint"
        )

    async def _run(self, request: ToolRequest) -> ToolResult:
        try:
            run = self._store.load(self._scope, request.run_id)
            if run.scope != self._scope or not isinstance(run.task_contract, dict):
                raise ValueError("test_action_run_contract_unavailable")
            action = _test_action_item(TaskContract.model_validate(run.task_contract))
            if action is None or action.verifier_version != TEST_ACTION_VERSION:
                raise ValueError("test_action_not_requested")
            paths = _required_paths(action)
            plan = json.loads(action.description)
            raw_launcher = plan.get("launcher", ("python", "-m", "pytest"))
            if not isinstance(raw_launcher, (list, tuple)):
                raise ValueError("test_action_launcher_invalid")
            launcher = tuple(raw_launcher)
            if launcher not in _TEST_LAUNCHERS:
                raise ValueError("test_action_launcher_invalid")
        except (KeyError, ValueError):
            return ToolResult(status="error", content={"code": "test_action_not_bound"})
        command = shlex.join((*launcher, "-q", *paths))
        command_request = request.model_copy(
            update={
                "call": ModelToolCall(
                    call_id=request.call.call_id,
                    name="run_command",
                    arguments={"command": command, "approval": {"approved": True}},
                ),
                "definition": self._command_binding.definition,
            }
        )
        executed = await self._command_binding.callback(command_request)
        content = executed.content if isinstance(executed.content, dict) else {}
        exit_code = content.get("exit_code")
        passed = (
            executed.status == "ok"
            and content.get("ok") is True
            and type(exit_code) is int
            and exit_code == 0
        )
        return ToolResult(
            status="ok" if passed else "error",
            content={
                "ok": passed,
                "exit_code": exit_code if type(exit_code) is int else None,
                "action_id": TEST_ACTION_ID,
                "paths": list(paths),
                "stdout": content.get("stdout") if isinstance(content.get("stdout"), str) else "",
                "stderr": content.get("stderr") if isinstance(content.get("stderr"), str) else "",
            },
        )


def install_required_test_tool(
    configuration: RunConfiguration,
    delegate: ToolPort,
    *,
    workspace_root: Path,
    scope: RunScope,
    store: RunStore,
    command_binding: ToolBinding | None,
    authorize: ToolAuthorizer,
) -> tuple[RunConfiguration, ToolPort]:
    if command_binding is None or not _discover_python_tests(workspace_root):
        return configuration, delegate
    if any(item.name == TEST_ACTION_TOOL_NAME for item in configuration.tools):
        raise ValueError("reserved_test_action_tool_conflict")
    return (
        configuration.model_copy(update={"tools": (*configuration.tools, TEST_ACTION_TOOL)}),
        StudioNativeRequiredTestsPort(
            delegate=delegate,
            scope=scope,
            store=store,
            command_binding=command_binding,
            authorize=authorize,
        ),
    )


def _committed_test_evidence(
    store: RunStore,
    scope: RunScope,
    run_id: str,
    required_paths: tuple[str, ...],
    *,
    workspace_root: Path | None = None,
) -> tuple[str, str]:
    """Return status and an auditable reference from unpruned effect receipts."""
    last_mutation_seq = 0
    passed_seq = 0
    passed_ref = ""
    passed_revision: str | None = None
    awaiting_test_capture = False
    test_before_revision: str | None = None
    intervening_tool_before_capture = False
    latest_revision: str | None = None
    latest_observation_seq = 0
    observed_workspace = False
    last_tool_seq = 0
    failed_ref = ""
    observed: list[str] = []
    for event in store.events(scope, run_id):
        if event.kind != "effect.received":
            continue
        key = event.payload.get("operation_key")
        if not isinstance(key, str):
            continue
        if event.payload.get("kind") == "workspace":
            observed_workspace = True
            workspace_receipt = store.get_receipt(scope, run_id, key)
            workspace_result = workspace_receipt.result if workspace_receipt is not None else {}
            revision = workspace_result.get("revision")
            if (
                workspace_receipt is not None
                and workspace_receipt.status == "committed"
                and workspace_result.get("status") == "captured"
                and isinstance(revision, str)
                and revision
            ):
                latest_revision = revision
                latest_observation_seq = event.seq
                if awaiting_test_capture:
                    if not intervening_tool_before_capture or (
                        test_before_revision is not None and test_before_revision == revision
                    ):
                        passed_revision = revision
                    else:
                        passed_seq = 0
                        passed_ref = ""
                        passed_revision = None
                    awaiting_test_capture = False
                    test_before_revision = None
                    intervening_tool_before_capture = False
            else:
                # Unknown workspace state cannot preserve a previous pass.
                latest_revision = None
                passed_revision = None
                awaiting_test_capture = False
                test_before_revision = None
                intervening_tool_before_capture = False
            continue
        if event.payload.get("kind") != "tool":
            continue
        intent = store.get_intent(scope, run_id, key)
        receipt = store.get_receipt(scope, run_id, key)
        if intent is None or receipt is None or intent.kind != "tool":
            continue
        raw_calls = intent.payload.get("calls")
        if not isinstance(raw_calls, list):
            raw_calls = [intent.payload.get("call")]
        batch = (
            receipt.result.get("content", {}).get("batch_results")
            if isinstance(receipt.result.get("content"), dict)
            else None
        )
        results = batch if isinstance(batch, list) else [receipt.result]
        for call, result in zip(raw_calls, results, strict=False):
            if not isinstance(call, dict) or not isinstance(result, dict):
                continue
            if awaiting_test_capture:
                # The engine can execute multiple tools before its next
                # workspace capture. Preserve the pass only if the complete
                # batch leaves the pre-test subject byte-identical.
                intervening_tool_before_capture = True
            last_tool_seq = event.seq
            observed.append(event.event_id)
            if result.get("workspace_changed_paths"):
                last_mutation_seq = event.seq
            tool_name = call.get("name")
            if tool_name not in {"run_command", TEST_ACTION_TOOL_NAME}:
                continue
            arguments = call.get("arguments")
            command = arguments.get("command") if isinstance(arguments, dict) else None
            configured_cwd = arguments.get("cwd") if isinstance(arguments, dict) else None
            content = result.get("content")
            scope_name = (
                (
                    _pytest_command_scope(command, required_paths, workspace_root=workspace_root)
                    if isinstance(command, str) and _locked_root_cwd(configured_cwd, workspace_root)
                    else None
                )
                if tool_name == "run_command"
                else (
                    "host_locked_tool"
                    if isinstance(content, dict)
                    and content.get("action_id") == TEST_ACTION_ID
                    and content.get("paths") == list(required_paths)
                    else None
                )
            )
            if scope_name is None:
                # Shell commands can edit files without a path receipt. A later
                # unknown command makes an earlier test revision unprovable.
                last_mutation_seq = event.seq
                continue
            success = (
                receipt.status == "committed"
                and result.get("status") == "ok"
                and isinstance(content, dict)
                and content.get("ok") is True
                and type(content.get("exit_code")) is int
                and content.get("exit_code") == 0
            )
            if success:
                passed_seq = event.seq
                passed_ref = f"native-event:{event.event_id}"
                passed_revision = None
                awaiting_test_capture = True
                test_before_revision = latest_revision
                intervening_tool_before_capture = False
            else:
                failed_ref = f"native-event:{event.event_id}"
                passed_seq = 0
                passed_ref = ""
                passed_revision = None
                awaiting_test_capture = False
                test_before_revision = None
                intervening_tool_before_capture = False
    if (
        observed_workspace
        and passed_seq
        and passed_revision is not None
        and passed_revision == latest_revision
        and last_tool_seq < latest_observation_seq
    ):
        return "passed", passed_ref
    if not observed_workspace and passed_seq > last_mutation_seq:
        return "passed", passed_ref
    if failed_ref:
        return "failed", failed_ref
    return "failed", _digest("\0".join((run_id, *observed, str(last_mutation_seq))))


class StudioNativeTestActionVerifier:
    """Compose action evidence with any existing Studio semantic verifier."""

    def __init__(
        self,
        *,
        scope: RunScope,
        store: RunStore,
        baseline: VerificationPort,
        workspace_root: Path | None = None,
    ) -> None:
        self._scope = scope.model_copy(deep=True)
        self._store = store
        self._baseline = baseline
        self._workspace_root = workspace_root.resolve() if workspace_root is not None else None

    async def verify(self, request: VerificationRequest) -> VerificationBundle:
        if request.scope != self._scope:
            raise ValueError("native_test_action_scope_mismatch")
        action = _test_action_item(request.contract)
        if action is None:
            return await self._baseline.verify(request)
        if (
            action.verifier_ref != TEST_ACTION_VERIFIER
            or action.verifier_version != TEST_ACTION_VERSION
            or action.verification_method != "deterministic"
        ):
            raise ValueError("native_test_action_identity_mismatch")
        required_paths = _required_paths(action)
        base_items = tuple(
            item for item in request.contract.items if item.acceptance_id != TEST_ACTION_ID
        )
        base_contract = request.contract.model_copy(update={"items": base_items})
        baseline = await self._baseline.verify(
            request.model_copy(update={"contract": base_contract})
        )
        status, reference = _committed_test_evidence(
            self._store,
            request.scope,
            request.run_id,
            required_paths,
            workspace_root=self._workspace_root,
        )
        results = list(baseline.report.items) if baseline.report else []
        covered = {item.acceptance_id for item in results}
        for item in base_items:
            if item.acceptance_id not in covered:
                results.append(
                    VerificationItemResult(
                        acceptance_id=item.acceptance_id,
                        status="not_run",
                        verifier_id=item.verifier_ref,
                        verifier_version=item.verifier_version,
                        verification_method=item.verification_method,
                        evidence_refs=(),
                    )
                )
        results.append(
            VerificationItemResult(
                acceptance_id=TEST_ACTION_ID,
                status=status,
                verifier_id=TEST_ACTION_VERIFIER,
                verifier_version=TEST_ACTION_VERSION,
                verification_method="deterministic",
                evidence_refs=(reference,),
                safe_summary=(
                    "The requested Python tests completed after the observed workspace edits."
                    if status == "passed"
                    else "No successful pytest run covered the requested tests after the latest "
                    "workspace action. Run the authorized test command, inspect its result, "
                    "and repair failures before finishing."
                ),
            )
        )
        candidate = baseline.candidate
        report = VerificationReport(
            candidate_id=candidate.candidate_id,
            contract_revision=request.contract.revision,
            subject_digest=candidate.subject_digest,
            subject_context_digest=candidate.subject_context_digest,
            environment_digest=candidate.environment_digest,
            items=tuple(results),
            repairable=status == "failed" or bool(baseline.report and baseline.report.repairable),
            failure_fingerprint=(
                _digest(f"{request.contract.revision}\0{reference}\0{status}")
                if status == "failed"
                else baseline.report.failure_fingerprint
                if baseline.report
                else ""
            ),
        )
        return VerificationBundle(
            candidate=candidate, report=report, post_workspace=baseline.post_workspace
        )


__all__ = ["StudioNativeTestActionVerifier", "extend_with_test_action"]
