import asyncio
import time

import pytest

from ksadk.harness.native.completion import (
    AcceptanceItem,
    CompletionCandidate,
    TaskContract,
    VerificationItemResult,
    VerificationReport,
)
from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.model import ModelProfile, ModelTurnResult, normalize_response
from ksadk.harness.native.ports import (
    RunConfiguration,
    RunLimits,
    ToolDefinition,
    ToolResourceUsage,
    ToolResult,
    VerificationBundle,
)
from ksadk.harness.native.state import RunCommand, RunPhase, RunScope
from ksadk.harness.stores.native_memory import MemoryRunStore

SCOPE = RunScope(tenant_id="t", workspace_id="w", user_id="u", agent_id="a")


def config(**limits):
    return RunConfiguration(
        profile=ModelProfile(profile_id="p", profile_version="1", provider_family="test"),
        contract=TaskContract(
            revision="1",
            items=(
                AcceptanceItem(
                    acceptance_id="file",
                    verifier_ref="file-check",
                    verifier_version="1",
                    verification_method="deterministic",
                    description="file contents are correct",
                ),
            ),
        ),
        tools=(ToolDefinition(name="write", description="write fixture", parameters={}),),
        limits=RunLimits(**limits),
    )


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return normalize_response(
            request_id=request.request_id, attempt_id=request.attempt_id, **next(self.responses)
        )


class Tools:
    def __init__(self, path):
        self.path = path
        self.calls = []

    async def execute(self, request):
        self.calls.append(request)
        self.path.write_text(request.call.arguments["text"])
        return ToolResult(status="ok", content={"written": True})


@pytest.mark.asyncio
async def test_committed_uncertain_tool_status_is_diagnostic_without_raw_content(tmp_path):
    class UncertainTools:
        async def execute(self, _request):
            return ToolResult(
                status="uncertain",
                content={"code": "callback_failed", "secret": "do-not-log"},
            )

    k = kernel(ScriptedModel(call()), tools=UncertainTools(), verifier=Verifier(tmp_path / "x"))
    result = await drive(k)
    assert result.reason == "effect_outcome_unknown"
    receipts = [
        event.payload
        for event in k.events(result.run_id)
        if event.kind == "effect.received" and event.payload.get("kind") == "tool"
    ]
    assert len(receipts) == 1
    assert receipts[0]["status"] == "committed"
    assert receipts[0]["tool_result_status"] == "uncertain"
    assert "do-not-log" not in str(receipts[0])


@pytest.mark.asyncio
async def test_tool_internal_model_usage_is_counted_without_claiming_a_child_run(tmp_path):
    class VisionBackedTools(Tools):
        async def execute(self, request):
            await super().execute(request)
            return ToolResult(
                status="ok",
                content={"written": True, "vision_description_trust": "untrusted_model_output"},
                resource_usage=ToolResourceUsage(
                    model_calls=1,
                    input_tokens=20,
                    output_tokens=5,
                    cached_input_tokens=3,
                    reasoning_tokens=2,
                ),
            )

    path = tmp_path / "result.txt"
    k = kernel(
        ScriptedModel(call(), {"text": "Done."}),
        tools=VisionBackedTools(path),
        verifier=Verifier(path),
    )
    result = await drive(k)
    assert result.outcome == "verified"
    assert result.descendant_model_calls == 1
    assert result.child_run_ids == ()
    events = [
        event
        for event in k.events(result.run_id)
        if event.kind == "usage.reported" and event.payload.get("source") == "tool_model_effects"
    ]
    assert len(events) == 1
    assert events[0].payload["input_tokens"] == 20
    assert events[0].payload["output_tokens"] == 5
    assert events[0].payload["cached_input_tokens"] == 3
    assert events[0].payload["reasoning_tokens"] == 2


class Verifier:
    def __init__(self, path):
        self.path = path
        self.calls = 0

    async def verify(self, request):
        import hashlib

        self.calls += 1
        content = self.path.read_bytes() if self.path.exists() else b""
        subject = hashlib.sha256(content).hexdigest()
        candidate = CompletionCandidate(
            candidate_id=request.candidate_id,
            final_text=request.final_text,
            subject_digest=subject,
            subject_context_digest=request.subject_context_digest,
            environment_digest="fixture-v1",
        )
        item = VerificationItemResult(
            acceptance_id="file",
            status="passed" if content == b"correct" else "failed",
            verifier_id="file-check",
            verifier_version="1",
            evidence_refs=(f"fixture://{subject}",),
            verification_method="deterministic",
        )
        return VerificationBundle(
            candidate=candidate,
            report=VerificationReport(
                candidate_id=candidate.candidate_id,
                contract_revision=request.contract.revision,
                subject_digest=subject,
                subject_context_digest=request.subject_context_digest,
                environment_digest="fixture-v1",
                items=(item,),
                repairable=True,
                failure_fingerprint="file-contents",
            ),
        )


def call(text="correct"):
    return {"tool_calls": [{"call_id": "write-1", "name": "write", "arguments": {"text": text}}]}


def kernel(model, *, store=None, configuration=None, tools=None, verifier=None):
    return NativeKernel(
        store=store or MemoryRunStore(),
        scope=SCOPE,
        configuration=configuration or config(),
        model=model,
        tools=tools,
        verifier=verifier,
    )


async def drive(k):
    run = k.start(goal="write a correct file", session_id="s")
    return await k.run_until_idle(run.run_id)


@pytest.mark.asyncio
async def test_explicit_read_only_tools_overlap_but_commit_receipts_in_model_order():
    class ParallelReads:
        def __init__(self):
            self.active = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, request):
            self.active += 1
            if self.active == 2:
                self.started.set()
            await self.release.wait()
            self.active -= 1
            return ToolResult(status="ok", content={"name": request.call.name})

    tools = ParallelReads()
    definitions = tuple(
        ToolDefinition(
            name=name,
            description="Read fixture",
            parameters={"type": "object"},
            read_only=True,
            concurrent_safe=True,
        )
        for name in ("first", "second")
    )
    cfg = config().model_copy(update={"tools": definitions})
    model = ScriptedModel(
        {
            "tool_calls": [
                {"call_id": "a", "name": "first", "arguments": {}},
                {"call_id": "b", "name": "second", "arguments": {}},
            ]
        },
        {"text": "done"},
    )
    k = kernel(model, configuration=cfg, tools=tools)
    pending = asyncio.create_task(drive(k))
    try:
        await asyncio.wait_for(tools.started.wait(), timeout=5)
    finally:
        tools.release.set()
    result = await asyncio.wait_for(pending, timeout=5)
    assert result.tool_calls == 2
    assert [m["tool_call_id"] for m in result.messages if m["role"] == "tool"] == ["a", "b"]
    effects = [e for e in k.events(result.run_id) if e.kind == "effect.received"]
    batch = [e for e in effects if e.payload.get("kind") == "tool"]
    assert len(batch) == 1
    assert batch[0].payload["tool_result_status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("overspend", [False, True])
async def test_parallel_delegation_partitions_parent_budget_and_accounts_each_child(overspend):
    class ChildPort:
        def __init__(self):
            self.requests = []

        async def execute(self, request):
            self.requests.append(request)
            return ToolResult(
                status="ok",
                content={"task": request.call.arguments["task"]},
                resource_usage=ToolResourceUsage(
                    model_calls=2 if overspend else 1,
                    child_run_ids=(f"child-{request.call.call_id}",),
                ),
            )

    port = ChildPort()
    delegated = ToolDefinition(
        name="delegate",
        description="Independent read-only child",
        parameters={"type": "object"},
        read_only=True,
        concurrent_safe=True,
        delegated_read_only=True,
    )
    cfg = config(max_model_calls=4, max_tool_calls=2).model_copy(
        update={"tools": (delegated,)}
    )
    model = ScriptedModel(
        {
            "tool_calls": [
                {"call_id": "a", "name": "delegate", "arguments": {"task": "one"}},
                {"call_id": "b", "name": "delegate", "arguments": {"task": "two"}},
            ]
        },
        {"text": "done"},
    )
    k = kernel(model, configuration=cfg, tools=port)
    result = await drive(k)
    assert [request.budget.max_model_calls_remaining for request in port.requests] == [1, 1]
    assert all(request.budget.parent_finalization_reserved for request in port.requests)
    assert result.descendant_model_calls == (4 if overspend else 2)
    assert result.child_run_ids == ("child-a", "child-b")
    assert [
        event.payload["call_id"]
        for event in k.events(result.run_id)
        if event.kind == "tool.child_settled"
    ] == ["a", "b"]
    if overspend:
        assert result.outcome == "blocked"
        assert result.reason == "tool_resource_budget_violation"
    else:
        assert result.phase == RunPhase.COMPLETED
        assert result.model_calls == 2
        assert [m["tool_call_id"] for m in result.messages if m["role"] == "tool"] == ["a", "b"]


@pytest.mark.asyncio
async def test_write_tools_remain_serial_even_when_the_model_batches_them():
    class Writes:
        def __init__(self):
            self.active = 0
            self.peak = 0
            self.order = []

        async def execute(self, request):
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.order.append(request.call.call_id)
            await asyncio.sleep(0)
            self.active -= 1
            return ToolResult(status="ok")

    tools = Writes()
    cfg = config().model_copy(
        update={"tools": (ToolDefinition(name="write", description="", parameters={}),)}
    )
    model = ScriptedModel(
        {
            "tool_calls": [
                {"call_id": "a", "name": "write", "arguments": {"text": "one"}},
                {"call_id": "b", "name": "write", "arguments": {"text": "two"}},
            ]
        },
        {"text": "done"},
    )
    result = await drive(kernel(model, configuration=cfg, tools=tools))
    assert result.tool_calls == 2
    assert tools.peak == 1
    assert tools.order == ["a", "b"]


@pytest.mark.asyncio
async def test_parallel_readonly_batch_rejects_a_tool_that_reports_workspace_mutation():
    class MisdeclaredRead:
        async def execute(self, request):
            return ToolResult(
                status="ok",
                workspace_changed_paths=("changed.txt",) if request.call.name == "first" else (),
            )

    cfg = config().model_copy(
        update={
            "tools": tuple(
                ToolDefinition(
                    name=name,
                    description="Read fixture",
                    parameters={"type": "object"},
                    read_only=True,
                    concurrent_safe=True,
                )
                for name in ("first", "second")
            )
        }
    )
    model = ScriptedModel(
        {
            "tool_calls": [
                {"call_id": "a", "name": "first", "arguments": {}},
                {"call_id": "b", "name": "second", "arguments": {}},
            ]
        }
    )
    result = await drive(kernel(model, configuration=cfg, tools=MisdeclaredRead()))
    assert result.outcome == "blocked"
    assert result.reason == "parallel_readonly_contract_violation"
    assert result.final_text == ""


def test_parallel_tool_authority_requires_read_only_and_no_observation_state():
    with pytest.raises(ValueError, match="concurrent_safe"):
        ToolDefinition(name="write", description="", parameters={}, concurrent_safe=True)
    with pytest.raises(ValueError, match="concurrent_safe"):
        ToolDefinition(
            name="browser",
            description="",
            parameters={},
            read_only=True,
            concurrent_safe=True,
            observation_role="browser",
        )


@pytest.mark.asyncio
async def test_real_file_failure_reenters_model_and_repairs(tmp_path):
    path = tmp_path / "result.txt"
    model = ScriptedModel(call("wrong"), {"text": "done"}, call(), {"text": "fixed"})
    executor, verifier = Tools(path), Verifier(path)
    k = kernel(model, tools=executor, verifier=verifier)
    result = await drive(k)
    assert result.phase == RunPhase.COMPLETED
    assert result.outcome == "verified"
    assert path.read_text() == "correct"
    assert result.repair_attempts == 1
    assert verifier.calls == 2
    assert len(executor.calls) == 2
    events = k.events(result.run_id)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert sum(e.kind == "run.completed" for e in events) == 1
    assert sum(e.kind == "text.completed" for e in events) == 1
    assert events[-1].payload["outcome"] == "verified"
    assert any(m.get("kind") == "verification_observation" for m in model.requests[2].messages)
    feedback = next(
        message["content"]
        for message in model.requests[2].messages
        if message.get("kind") == "verification_observation"
    )
    assert "fixture://" not in feedback
    assert "evidence_refs" not in feedback
    assert "fixture://" in str(result.verification)


@pytest.mark.asyncio
async def test_observed_failure_repairs_even_when_another_required_check_was_not_run(tmp_path):
    class MixedVerifier(Verifier):
        async def verify(self, request):
            bundle = await super().verify(request)
            file_passed = bundle.report.items[0].status == "passed"
            downstream = VerificationItemResult(
                acceptance_id="downstream",
                status="passed" if file_passed else "not_run",
                verifier_id="downstream-check",
                verifier_version="1",
                evidence_refs=("fixture://downstream",),
                verification_method="deterministic",
            )
            report = bundle.report.model_copy(
                update={"items": (*bundle.report.items, downstream)}
            )
            return bundle.model_copy(update={"report": report})

    path = tmp_path / "result.txt"
    task = config().model_copy(
        update={
            "contract": TaskContract(
                revision="1",
                items=(
                    *config().contract.items,
                    AcceptanceItem(
                        acceptance_id="downstream",
                        verifier_ref="downstream-check",
                        verifier_version="1",
                        verification_method="deterministic",
                        description="Downstream action can run after the file is correct.",
                    ),
                ),
            )
        }
    )
    model = ScriptedModel(call("wrong"), {"text": "done"}, call(), {"text": "fixed"})
    verifier = MixedVerifier(path)
    result = await drive(kernel(model, configuration=task, tools=Tools(path), verifier=verifier))
    assert (result.phase, result.outcome, result.repair_attempts) == (
        RunPhase.COMPLETED, "verified", 1
    )
    assert verifier.calls == 2
    assert any(
        message.get("kind") == "verification_observation"
        and '"not_run"' in message.get("content", "")
        for message in model.requests[2].messages
    )


@pytest.mark.asyncio
async def test_tagged_browser_error_is_durable_in_kernel_run():
    class BrowserTools:
        async def execute(self, _request):
            return ToolResult(
                status="error",
                content={
                    "ok": False,
                    "entry_path": "index.html",
                    "page_errors": ["uncaught TypeError"],
                    "actions": [{"type": "click", "ok": False}],
                    "actions_ok": False,
                },
            )

    browser_config = config().model_copy(
        update={
            "tools": (
                ToolDefinition(
                    name="browser_inspect_workspace",
                    description="Inspect page",
                    parameters={},
                    observation_role="browser",
                ),
            ),
        }
    )
    model = ScriptedModel(
        {
            "tool_calls": [
                {
                    "call_id": "browser-1",
                    "name": "browser_inspect_workspace",
                    "arguments": {"path": "index.html"},
                }
            ]
        },
        {"text": "The page is done."},
    )
    result = await drive(kernel(model, configuration=browser_config, tools=BrowserTools()))
    assert result.unresolved_interaction_observation is not None
    assert result.unresolved_interaction_observation["entry_path"] == "index.html"


@pytest.mark.asyncio
async def test_no_tools_no_verifier_does_not_claim_verified():
    result = await drive(kernel(ScriptedModel({"text": "everything is complete"})))
    assert result.phase == RunPhase.COMPLETED
    assert result.outcome == "unverified"
    assert result.reason == "verification_report_missing"


@pytest.mark.asyncio
async def test_repeated_failure_without_new_evidence_stops(tmp_path):
    k = kernel(
        ScriptedModel({"text": "done"}, {"text": "done again"}),
        verifier=Verifier(tmp_path / "missing"),
    )
    result = await drive(k)
    assert result.outcome == "unverified"
    assert result.reason == "repeated_verification_failure"
    assert result.model_calls == 2


@pytest.mark.asyncio
async def test_mixed_report_item_repair_authority_runs_tool_then_reverifies(tmp_path):
    class MixedVerifier(Verifier):
        async def verify(self, request):
            bundle = await super().verify(request)
            file_item = bundle.report.items[0]
            missing = file_item.status != "passed"
            file_item = file_item.model_copy(
                update={
                    "status": "not_run" if missing else "passed",
                    "safe_summary": (
                        "Write the required file and verify it." if missing else "File passed."
                    ),
                    "repairable": True if missing else None,
                }
            )
            other = file_item.model_copy(
                update={
                    "acceptance_id": "second_check",
                    "status": "not_run" if missing else "passed",
                    "safe_summary": "Second check waits for the file." if missing else "Passed.",
                    "repairable": None,
                }
            )
            return bundle.model_copy(
                update={
                    "report": bundle.report.model_copy(
                        update={
                            "items": (file_item, other),
                            "repairable": False,
                            "failure_fingerprint": "file-missing",
                        }
                    )
                }
            )

    task = config(max_model_calls=4)
    task = task.model_copy(
        update={
            "contract": task.contract.model_copy(
                update={
                    "items": (
                        *task.contract.items,
                        task.contract.items[0].model_copy(
                            update={"acceptance_id": "second_check"}
                        ),
                    )
                }
            )
        }
    )
    path = tmp_path / "result.txt"
    model = ScriptedModel({"text": "done"}, call(), {"text": "done after repair"})
    tools = Tools(path)
    verifier = MixedVerifier(path)
    result = await drive(kernel(model, configuration=task, tools=tools, verifier=verifier))

    assert result.outcome == "verified"
    assert len(tools.calls) == 1
    assert verifier.calls == 2


@pytest.mark.asyncio
async def test_new_evidence_uri_for_same_failed_subject_is_not_progress(tmp_path):
    class RotatingReferences(Verifier):
        async def verify(self, request):
            bundle = await super().verify(request)
            item = bundle.report.items[0].model_copy(
                update={"evidence_refs": (f"evidence://{request.candidate_id}",)}
            )
            return bundle.model_copy(
                update={"report": bundle.report.model_copy(update={"items": (item,)})}
            )

    k = kernel(
        ScriptedModel({"text": "done"}, {"text": "still done"}),
        verifier=RotatingReferences(tmp_path / "missing"),
    )
    result = await drive(k)
    assert result.reason == "repeated_verification_failure"
    assert result.model_calls == 2


@pytest.mark.asyncio
async def test_max_model_budget_is_not_completion():
    # Exercise the explicit host policy that spends its only call on tools.
    k = kernel(
        ScriptedModel(call()),
        configuration=config(max_model_calls=1, finalization_reserve_model_calls=0),
    )
    result = await drive(k)
    assert result.phase == RunPhase.FAILED
    assert result.outcome == "budget_exhausted"
    assert result.reason == "model_budget_exhausted"
    terminals = [
        e.kind for e in k.events(result.run_id) if e.kind in {"run.completed", "run.failed"}
    ]
    assert terminals == ["run.failed"]


@pytest.mark.asyncio
async def test_model_budget_reserves_truthful_no_tool_finalization(tmp_path):
    model = ScriptedModel(
        call(),
        {"text": "I wrote the file, but no independent check verified the full task."},
    )
    executor = Tools(tmp_path / "result.txt")
    result = await drive(
        kernel(
            model,
            tools=executor,
            configuration=config(max_model_calls=2, finalization_reserve_model_calls=1),
        )
    )

    assert result.phase == RunPhase.COMPLETED
    assert result.outcome == "unverified"
    assert result.model_calls == 2
    assert len(executor.calls) == 1
    assert model.requests[0].tools
    assert model.requests[1].tools == ()
    assert "finalization turn" in model.requests[1].instructions


@pytest.mark.asyncio
async def test_failed_verification_at_model_budget_limit_delivers_partial_report(tmp_path):
    path = tmp_path / "result.txt"
    model = ScriptedModel(
        call("wrong"),
        {"text": "The file is still wrong. I could not complete the requested change."},
    )
    result = await drive(
        kernel(
            model,
            tools=Tools(path),
            verifier=Verifier(path),
            configuration=config(max_model_calls=2, finalization_reserve_model_calls=1),
        )
    )
    assert result.phase == RunPhase.COMPLETED
    assert result.outcome == "unverified"
    assert result.reason == "repair_budget_unavailable"
    assert result.final_text == (
        "The file is still wrong. I could not complete the requested change."
    )
    assert result.model_calls == 2
    assert model.requests[1].tools == ()
    assert path.read_text() == "wrong"


@pytest.mark.asyncio
async def test_text_only_verification_can_repair_without_tools(tmp_path):
    class AnswerVerifier:
        async def verify(self, request):
            accepted = request.final_text.strip() == "42"
            candidate = CompletionCandidate(
                candidate_id=request.candidate_id,
                final_text=request.final_text,
                subject_digest="answer-subject",
                subject_context_digest=request.subject_context_digest,
                environment_digest="answer-environment",
            )
            report = VerificationReport(
                candidate_id=request.candidate_id,
                contract_revision=request.contract.revision,
                subject_digest=candidate.subject_digest,
                subject_context_digest=request.subject_context_digest,
                environment_digest=candidate.environment_digest,
                items=(
                    VerificationItemResult(
                        acceptance_id="file",
                        status="passed" if accepted else "failed",
                        verifier_id="file-check",
                        verifier_version="1",
                        verification_method="deterministic",
                        evidence_refs=("fixture://answer",),
                        safe_summary="correct" if accepted else "recompute the answer",
                    ),
                ),
                repairable=not accepted,
                failure_fingerprint="answer-wrong",
            )
            return VerificationBundle(candidate=candidate, report=report)

    model = ScriptedModel({"text": "41"}, {"text": "42"})
    result = await drive(
        kernel(
            model,
            verifier=AnswerVerifier(),
            configuration=config(max_model_calls=3, max_tool_calls=0),
        )
    )
    assert result.outcome == "verified", (result.reason, result.model_calls)
    assert result.final_text == "42"
    assert result.model_calls == 2
    assert all(request.tools == () for request in model.requests)
    rejected = [message for message in model.requests[1].messages
                if message.get("kind") == "rejected_final_candidate"]
    assert len(rejected) == 1 and rejected[0]["content"] == "41"
    assert rejected[0]["candidate_status"] == "rejected"


@pytest.mark.asyncio
async def test_model_backed_tool_consumption_enters_reserved_finalization(tmp_path):
    class ModelBackedTools(Tools):
        async def execute(self, request):
            await super().execute(request)
            return ToolResult(
                status="ok",
                content={"written": True},
                resource_usage=ToolResourceUsage(model_calls=1),
            )

    model = ScriptedModel(call(), {"text": "The file was written; verification is missing."})
    result = await drive(
        kernel(
            model,
            tools=ModelBackedTools(tmp_path / "result.txt"),
            configuration=config(max_model_calls=4, finalization_reserve_model_calls=2),
        )
    )
    assert result.outcome == "unverified"
    assert result.model_calls == 2
    assert result.descendant_model_calls == 1
    assert model.requests[1].tools == ()


@pytest.mark.asyncio
async def test_model_backed_tool_batch_cannot_spend_reserved_final_turn(tmp_path):
    class BudgetedTools(Tools):
        def __init__(self, path):
            super().__init__(path)
            self.model_allowances = []

        async def execute(self, request):
            await super().execute(request)
            allowance = request.budget.max_model_calls_remaining
            self.model_allowances.append(allowance)
            return ToolResult(
                status="ok",
                content={"written": True},
                resource_usage=ToolResourceUsage(model_calls=1 if allowance else 0),
            )

    model = ScriptedModel(
        {
            "tool_calls": [
                {"call_id": "write-1", "name": "write", "arguments": {"text": "correct"}},
                {"call_id": "write-2", "name": "write", "arguments": {"text": "correct"}},
            ]
        },
        {"text": "Two writes were attempted; no independent check verified the task."},
    )
    tools = BudgetedTools(tmp_path / "result.txt")
    result = await drive(
        kernel(
            model,
            tools=tools,
            configuration=config(max_model_calls=3, finalization_reserve_model_calls=1),
        )
    )
    assert tools.model_allowances == [1, 0]
    assert result.model_calls == 2
    assert result.descendant_model_calls == 1
    assert result.tool_calls == 2
    assert model.requests[1].tools == ()
    assert result.outcome == "unverified"


@pytest.mark.asyncio
async def test_unadvertised_finalization_tool_call_cannot_execute(tmp_path):
    executor = Tools(tmp_path / "result.txt")
    model = ScriptedModel(call())
    result = await drive(
        kernel(
            model,
            tools=executor,
            configuration=config(max_model_calls=1, finalization_reserve_model_calls=1),
        )
    )
    assert model.requests[0].tools == ()
    assert result.reason == "tool_call_after_finalization_reserve"
    assert result.tool_calls == 0
    assert executor.calls == []


@pytest.mark.asyncio
async def test_tool_budget_refuses_remaining_batch_and_allows_verified_final(tmp_path):
    path = tmp_path / "result.txt"
    model = ScriptedModel(
        {
            "tool_calls": [
                {"call_id": "write-1", "name": "write", "arguments": {"text": "correct"}},
                {"call_id": "write-2", "name": "write", "arguments": {"text": "wrong"}},
            ]
        },
        {"text": "The file is correct. The second requested write was refused."},
    )
    executor = Tools(path)
    k = kernel(
        model,
        tools=executor,
        verifier=Verifier(path),
        configuration=config(max_tool_calls=1),
    )
    result = await drive(k)

    assert result.phase == RunPhase.COMPLETED
    assert result.outcome == "verified"
    assert path.read_text() == "correct"
    assert result.tool_calls == len(executor.calls) == 1
    assert model.requests[0].tools
    assert model.requests[1].tools == ()
    assert "budget is exhausted" in model.requests[1].instructions
    refused = [
        message
        for message in model.requests[1].messages
        if message.get("tool_call_id") == "write-2"
    ]
    assert len(refused) == 1
    assert '"executed": false' in refused[0]["content"]
    events = [event for event in k.events(result.run_id) if event.kind == "budget.exhausted"]
    assert len(events) == 1
    assert events[0].payload["refused_call_ids"] == ["write-2"]


@pytest.mark.asyncio
async def test_sealed_malformed_tool_arguments_get_receipt_without_host_effect(tmp_path):
    path = tmp_path / "result.txt"
    model = ScriptedModel(
        {
            "tool_calls": [
                {
                    "call_id": "bad-wire-1",
                    "name": "write",
                    "arguments": {},
                    "wire_arguments_invalid": True,
                }
            ]
        },
        call(),
        {"text": "The file is correct."},
    )
    tools = Tools(path)
    result = await drive(kernel(model, tools=tools, verifier=Verifier(path)))
    assert result.outcome == "verified"
    assert path.read_text() == "correct"
    assert len(tools.calls) == 1
    assert result.tool_calls == 2
    receipts = [
        message
        for message in model.requests[1].messages
        if message.get("tool_call_id") == "bad-wire-1"
    ]
    assert len(receipts) == 1
    assert "malformed_tool_arguments" in receipts[0]["content"]


@pytest.mark.asyncio
async def test_tool_call_after_exhausted_budget_fails_without_execution(tmp_path):
    executor = Tools(tmp_path / "result.txt")
    model = ScriptedModel(call(), call("wrong"))
    result = await drive(kernel(model, tools=executor, configuration=config(max_tool_calls=1)))

    assert result.phase == RunPhase.FAILED
    assert result.outcome == "budget_exhausted"
    assert result.reason == "tool_call_after_budget_exhaustion"
    assert len(executor.calls) == 1
    assert model.requests[1].tools == ()


@pytest.mark.asyncio
async def test_zero_tool_budget_never_advertises_tools():
    model = ScriptedModel({"text": "No tools were available."})
    result = await drive(kernel(model, configuration=config(max_tool_calls=0)))

    assert result.phase == RunPhase.COMPLETED
    assert result.outcome == "unverified"
    assert model.requests[0].tools == ()
    assert "budget is exhausted" in model.requests[0].instructions


@pytest.mark.asyncio
async def test_no_bound_tools_get_no_tool_recovery_feedback():
    model = ScriptedModel(
        {"finish_reason": "malformed_tool_call"},
        {"text": "I cannot use a tool in this run."},
    )
    configuration = config().model_copy(update={"tools": ()})
    result = await drive(kernel(model, configuration=configuration))

    assert result.phase == RunPhase.COMPLETED
    assert result.outcome == "unverified"
    assert all(request.tools == () for request in model.requests)
    observations = [
        message
        for message in model.requests[1].messages
        if message.get("kind") == "model_recovery_observation"
    ]
    assert len(observations) == 1
    assert "no available tools" in observations[0]["content"]


@pytest.mark.asyncio
async def test_model_policy_abort_without_answer_has_failed_terminal():
    k = kernel(ScriptedModel({"finish_reason": "policy_abort"}))
    result = await drive(k)
    assert result.phase == RunPhase.FAILED
    assert result.outcome == "blocked"
    assert result.reason == "model_policy_abort"
    terminals = [
        e.kind for e in k.events(result.run_id) if e.kind in {"run.completed", "run.failed"}
    ]
    assert terminals == ["run.failed"]


@pytest.mark.asyncio
async def test_truncated_tool_batch_never_executes(tmp_path):
    executor = Tools(tmp_path / "never")
    model = ScriptedModel({**call(), "finish_reason": "length"}, {"text": "partial"})
    result = await drive(kernel(model, tools=executor))
    assert not executor.calls
    assert result.continuations == 1
    assert result.outcome == "unverified"


@pytest.mark.asyncio
async def test_output_continuation_limit_resets_after_valid_tool_batch(tmp_path):
    path = tmp_path / "result.txt"
    model = ScriptedModel(
        {"text": "stale-one", "finish_reason": "length"},
        call(),
        {"text": "stale-two", "finish_reason": "length"},
        {"tool_calls": [{"call_id": "write-2", "name": "write", "arguments": {"text": "correct"}}]},
        {"text": "last ", "finish_reason": "length"},
        {"text": "suffix"},
    )
    executor = Tools(path)
    configuration = config().model_copy(
        update={"profile": config().profile.model_copy(update={"max_continuations": 1})}
    )
    k = kernel(model, configuration=configuration, tools=executor, verifier=Verifier(path))

    result = await drive(k)

    assert result.phase == RunPhase.COMPLETED
    assert result.outcome == "verified"
    assert result.final_text == "last suffix"
    assert result.continuations == 1
    assert len(executor.calls) == 2
    assert [event.kind for event in k.events(result.run_id)].count("run.completed") == 1


@pytest.mark.asyncio
async def test_output_continuation_limit_still_caps_one_unbroken_chain():
    configuration = config()
    configuration = configuration.model_copy(
        update={"profile": configuration.profile.model_copy(update={"max_continuations": 1})}
    )
    model = ScriptedModel(
        {"text": "first", "finish_reason": "length"},
        {"text": "second", "finish_reason": "length"},
    )

    k = kernel(model, configuration=configuration)
    result = await drive(k)

    assert result.phase == RunPhase.FAILED
    assert result.outcome == "unverified"
    assert result.reason == "continuation_budget_exhausted"
    assert result.continuations == 1
    assert len(model.requests) == 2
    assert [
        event.payload["model_response_kind"]
        for event in k.events(result.run_id)
        if event.kind == "effect.received" and event.payload.get("kind") == "model"
    ] == ["output_limit", "output_limit"]
    assert "first" not in str(
        [
            event.payload
            for event in k.events(result.run_id)
            if event.kind == "effect.received" and event.payload.get("kind") == "model"
        ]
    )


@pytest.mark.asyncio
async def test_empty_output_limit_resumes_from_committed_state_without_fake_suffix():
    model = ScriptedModel(
        {"text": "", "finish_reason": "length"},
        {"text": "The task remains unfinished."},
    )
    result = await drive(kernel(model))

    assert result.phase == RunPhase.COMPLETED
    assert result.final_text == "The task remains unfinished."
    resumed = model.requests[1].messages
    assert not any(message.get("kind") == "continuation_fragment" for message in resumed)
    observation = next(
        message for message in resumed if message.get("kind") == "continuation_observation"
    )
    assert observation["continuation_mode"] == "empty_output"
    assert "missing suffix" not in observation["content"]


@pytest.mark.asyncio
async def test_oversized_unfinished_output_gets_one_bounded_fresh_attempt(tmp_path):
    path = tmp_path / "result.txt"
    model = ScriptedModel(
        {"text": "unfinished tool JSON part one", "finish_reason": "length"},
        {"text": "unfinished tool JSON part two", "finish_reason": "length"},
        call("correct"),
        {"text": "done"},
    )
    configuration = config(max_output_restarts=1).model_copy(
        update={"profile": config().profile.model_copy(update={"max_continuations": 1})}
    )
    executor = Tools(path)
    result = await drive(
        kernel(model, configuration=configuration, tools=executor, verifier=Verifier(path))
    )
    assert result.phase == RunPhase.COMPLETED
    assert result.output_restarts == 1
    assert len(executor.calls) == 1
    assert path.read_text() == "correct"
    assert "unfinished tool JSON" not in str(model.requests[2].messages)
    assert "was not executed" in str(model.requests[2].messages)


@pytest.mark.asyncio
async def test_cancel_wins_over_pause_and_final_candidate():
    k = kernel(ScriptedModel({"text": "complete"}))
    run = k.start(goal="do work", session_id="s")
    k.submit(run.run_id, RunCommand(command_id="pause", kind="pause"))
    k.submit(run.run_id, RunCommand(command_id="cancel", kind="cancel"))
    result = await k.run_until_idle(run.run_id)
    assert result.phase == RunPhase.CANCELED
    assert result.model_calls == 0
    assert k.submit(run.run_id, RunCommand(command_id="late", kind="resume")).status == "rejected"


@pytest.mark.asyncio
async def test_pause_resume_same_phase_and_idempotent_commands():
    k = kernel(ScriptedModel({"text": "hello"}))
    run = k.start(goal="answer", session_id="s")
    command = RunCommand(command_id="pause", kind="pause")
    assert k.submit(run.run_id, command).status == "accepted"
    assert k.submit(run.run_id, command).status == "duplicate"
    assert (await k.run_until_idle(run.run_id)).phase == RunPhase.PAUSED
    k.submit(run.run_id, RunCommand(command_id="resume", kind="resume"))
    assert (await k.run_until_idle(run.run_id)).phase == RunPhase.COMPLETED
    assert sum(event.kind == "run.started" for event in k.events(run.run_id)) == 1


@pytest.mark.asyncio
async def test_approval_bound_to_current_interaction_and_tool(tmp_path):
    class Approving(Tools):
        async def execute(self, request):
            if request.interaction is None:
                return ToolResult(status="approval_required", interaction_id="approval-1")
            assert request.interaction == {"interaction_id": "approval-1", "decision": "allow"}
            return await super().execute(request)

    executor = Approving(tmp_path / "result")
    k = kernel(
        ScriptedModel(call(), {"text": "done"}),
        tools=executor,
        configuration=config(max_tool_calls=1),
    )
    run = k.start(goal="write", session_id="s")
    paused = await k.run_until_idle(run.run_id)
    assert paused.phase == RunPhase.AWAITING_INTERACTION
    requested = [e for e in k.events(run.run_id) if e.kind == "approval.requested"]
    assert len(requested) == 1
    assert requested[0].payload["approval_id"] == paused.pending_interaction
    k.submit(run.run_id, RunCommand(command_id="pause", kind="pause"))
    await k.run_until_idle(run.run_id)
    k.submit(run.run_id, RunCommand(command_id="resume", kind="resume"))
    await k.run_until_idle(run.run_id)
    k.submit(
        run.run_id,
        RunCommand(
            command_id="bad",
            kind="submit_interaction",
            payload={"interaction_id": "old", "decision": "allow"},
        ),
    )
    assert (await k.run_until_idle(run.run_id)).phase == RunPhase.AWAITING_INTERACTION
    assert not executor.calls
    k.submit(
        run.run_id,
        RunCommand(
            command_id="good",
            kind="submit_interaction",
            payload={"interaction_id": paused.pending_interaction, "decision": "allow"},
        ),
    )
    result = await k.run_until_idle(run.run_id)
    assert result.phase == RunPhase.COMPLETED
    assert len(executor.calls) == 1
    resolved = [e for e in k.events(run.run_id) if e.kind == "approval.resolved"]
    assert len(resolved) == 1
    assert resolved[0].payload["approval_id"] == paused.pending_interaction


@pytest.mark.asyncio
async def test_running_intent_after_takeover_is_not_reexecuted():
    model = ScriptedModel({"text": "never call"})
    k = kernel(model)
    run = k.start(goal="do work", session_id="s")
    while not run.pending_intent:
        run = await k.advance(run.run_id)
    k.store.claim_intent(
        SCOPE, run.run_id, run.pending_intent, fencing_token=k.fencing_token(run.run_id)
    )
    successor = kernel(model, store=k.store)
    successor.attach(run.run_id)
    result = await successor.run_until_idle(run.run_id)
    assert result.phase == RunPhase.INTERRUPTED
    assert result.reason == "effect_outcome_unknown"
    assert not model.requests


@pytest.mark.asyncio
async def test_in_process_model_timeout_retries_without_replaying_a_tool(tmp_path, monkeypatch):
    from ksadk.harness.native import effect_control

    class EffectClock:
        elapsed = 0.0

        def time(self):
            return self.elapsed

    clock = EffectClock()

    class EffectAsyncio:
        def get_running_loop(self):
            return clock

        def __getattr__(self, name):
            return getattr(asyncio, name)

    # Exercise real cancellation while keeping this retry-state test independent
    # of filesystem latency and unrelated event-loop scheduling. The neighboring
    # late-model/tool tests retain the real clock and the same hard deadline.
    monkeypatch.setattr(effect_control, "asyncio", EffectAsyncio())
    first_model_cancelled = asyncio.Event()
    path = tmp_path / "result.txt"

    class SlowFirstModel(ScriptedModel):
        async def complete(self, request):
            if not self.requests:
                self.requests.append(request)
                clock.elapsed += 0.03
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    first_model_cancelled.set()
                    raise
                raise AssertionError("timed-out model request should be canceled")
            return await super().complete(request)

    model = SlowFirstModel(call(), {"text": "fixed"})
    tools, verifier = Tools(path), Verifier(path)
    k = kernel(
        model,
        configuration=config(effect_timeout_seconds=0.02),
        tools=tools,
        verifier=verifier,
    )
    result = await drive(k)
    assert result.phase == RunPhase.COMPLETED
    assert result.outcome == "verified"
    assert first_model_cancelled.is_set()
    assert len(model.requests) == 3
    assert len(tools.calls) == 1
    assert path.read_text() == "correct"
    assert result.model_calls == 3
    receipts = [
        k.store.get_receipt(SCOPE, result.run_id, event.payload["operation_key"])
        for event in k.events(result.run_id)
        if event.kind == "effect.received"
    ]
    assert (
        sum(
            receipt.status == "uncertain" and receipt.result.get("reason") == "effect_timeout"
            for receipt in receipts
        )
        == 1
    )
    unavailable = [
        event.payload for event in k.events(result.run_id) if event.kind == "usage.unavailable"
    ]
    assert sum(item["source"] == "model_effect_timeout" for item in unavailable) == 1
    assert sum(item["source"] == "provider_usage_missing" for item in unavailable) == 2


@pytest.mark.asyncio
async def test_in_process_model_timeout_stops_at_the_pinned_retry_limit():
    class AlwaysSlow:
        def __init__(self):
            self.requests = []

        async def complete(self, request):
            self.requests.append(request)
            await asyncio.sleep(0.1)
            raise AssertionError("timed-out model request should be canceled")

    model = AlwaysSlow()
    base = config(effect_timeout_seconds=0.02)
    profile = base.profile.model_copy(
        update={"max_provider_retries": 1, "retry_initial_delay_seconds": 0.0}
    )
    k = kernel(model, configuration=base.model_copy(update={"profile": profile}))
    result = await drive(k)
    assert result.phase == RunPhase.FAILED
    assert result.outcome == "failed"
    assert result.reason == "provider_retryable_error_retry_exhausted"
    assert len(model.requests) == result.model_calls == 2
    assert sum(event.kind == "usage.unavailable" for event in k.events(result.run_id)) == 2
    assert sum(event.kind == "run.failed" for event in k.events(result.run_id)) == 1
    assert not any(event.kind == "run.completed" for event in k.events(result.run_id))


@pytest.mark.asyncio
async def test_model_result_after_effect_deadline_is_not_committed_when_loop_wakes_late():
    class BlockingModel:
        def __init__(self):
            self.requests = []

        async def complete(self, request):
            self.requests.append(request)
            # A synchronous provider adapter can stall the event loop past the
            # deadline before the timeout waiter gets another scheduling turn.
            time.sleep(0.04)
            return normalize_response(
                request_id=request.request_id,
                attempt_id=request.attempt_id,
                text="late answer",
            )

    model = BlockingModel()
    base = config(effect_timeout_seconds=0.02)
    profile = base.profile.model_copy(update={"max_provider_retries": 0})
    k = kernel(model, configuration=base.model_copy(update={"profile": profile}))
    result = await drive(k)
    assert result.phase == RunPhase.FAILED
    assert result.reason == "provider_retryable_error_retry_exhausted"
    assert len(model.requests) == result.model_calls == 1
    assert not any(event.kind == "text.completed" for event in k.events(result.run_id))
    receipts = [
        k.store.get_receipt(SCOPE, result.run_id, event.payload["operation_key"])
        for event in k.events(result.run_id)
        if event.kind == "effect.received"
    ]
    assert len(receipts) == 1
    assert receipts[0].status == "uncertain"
    assert receipts[0].result == {"reason": "effect_timeout"}


@pytest.mark.asyncio
async def test_provider_failure_category_is_recorded_without_exposing_transport_text():
    class TimeoutModel:
        async def complete(self, request):
            return ModelTurnResult(
                response_kind="provider_retryable_error",
                request_id=request.request_id,
                attempt_id=request.attempt_id,
                failure_kind="request_timeout",
                provider_failure_category="timeout",
                provider_status_code=504,
                gateway_error_code="MODEL_REQUEST_FAILED",
            )

    base = config()
    profile = base.profile.model_copy(update={"max_provider_retries": 0})
    k = kernel(TimeoutModel(), configuration=base.model_copy(update={"profile": profile}))
    result = await drive(k)
    failures = [
        event for event in k.events(result.run_id) if event.kind == "model.provider_failure"
    ]
    assert result.reason == "provider_retryable_error_retry_exhausted"
    assert len(failures) == 1
    assert failures[0].payload["failure_kind"] == "request_timeout"
    assert failures[0].payload["provider_failure_category"] == "timeout"
    assert failures[0].payload["provider_status_code"] == 504
    assert failures[0].payload["gateway_error_code"] == "MODEL_REQUEST_FAILED"
    assert failures[0].payload["response_kind"] == "provider_retryable_error"


@pytest.mark.asyncio
async def test_provider_retry_budget_is_separate_from_invalid_model_response_budget():
    model = ScriptedModel(
        {"finish_reason": "provider_retryable_error"},
        {"finish_reason": "provider_retryable_error"},
        {"text": "Recovered after transport errors."},
    )
    base = config()
    profile = base.profile.model_copy(
        update={
            "max_retries": 0,
            "max_provider_retries": 2,
            "retry_initial_delay_seconds": 0.0,
        }
    )
    k = kernel(model, configuration=base.model_copy(update={"profile": profile}))

    result = await drive(k)

    assert result.phase == RunPhase.COMPLETED
    assert result.model_calls == len(model.requests) == 3
    assert result.provider_errors == result.model_errors == 0
    assert sum(event.kind == "run.completed" for event in k.events(result.run_id)) == 1
    assert sum(event.kind == "usage.unavailable" for event in k.events(result.run_id)) == 3


@pytest.mark.asyncio
async def test_invalid_model_response_still_stops_at_its_own_budget_after_transport_error():
    model = ScriptedModel(
        {"finish_reason": "provider_retryable_error"},
        {"finish_reason": "empty_response"},
    )
    base = config()
    profile = base.profile.model_copy(
        update={
            "max_retries": 0,
            "max_provider_retries": 2,
            "retry_initial_delay_seconds": 0.0,
        }
    )
    k = kernel(model, configuration=base.model_copy(update={"profile": profile}))

    result = await drive(k)

    assert result.phase == RunPhase.FAILED
    assert result.reason == "empty_response_retry_exhausted"
    assert result.model_calls == len(model.requests) == 2
    assert result.provider_errors == 0
    assert result.model_errors == 1
    assert sum(event.kind == "run.failed" for event in k.events(result.run_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["empty_response", "malformed_tool_call"])
async def test_exhausted_invalid_model_response_has_one_failed_terminal(reason):
    model = ScriptedModel(*({"finish_reason": reason} for _ in range(3)))
    base = config()
    profile = base.profile.model_copy(update={"max_retries": 2, "retry_initial_delay_seconds": 0.0})
    k = kernel(model, configuration=base.model_copy(update={"profile": profile}))

    result = await drive(k)

    assert result.phase == RunPhase.FAILED
    assert result.outcome == "failed"
    assert result.reason == f"{reason}_retry_exhausted"
    terminals = [
        event.kind
        for event in k.events(result.run_id)
        if event.kind.startswith("run.")
        and event.kind in {"run.completed", "run.failed", "run.interrupted"}
    ]
    assert terminals == ["run.failed"]


@pytest.mark.asyncio
async def test_in_process_tool_timeout_remains_unknown_and_is_not_replayed(tmp_path):
    class SlowTool(Tools):
        async def execute(self, request):
            self.calls.append(request)
            await asyncio.sleep(0.1)
            return ToolResult(status="ok", content={"written": True})

    tools = SlowTool(tmp_path / "result.txt")
    model = ScriptedModel(call())
    k = kernel(model, configuration=config(effect_timeout_seconds=0.02), tools=tools)
    result = await drive(k)
    assert result.phase == RunPhase.INTERRUPTED
    assert result.reason == "effect_outcome_unknown"
    assert len(tools.calls) == 1
    assert len(model.requests) == 1
    assert not (tmp_path / "result.txt").exists()


@pytest.mark.asyncio
async def test_late_blocking_tool_result_keeps_committed_side_effect_uncertain(tmp_path):
    class BlockingTool(Tools):
        async def execute(self, request):
            self.calls.append(request)
            self.path.write_text(request.call.arguments["text"])
            time.sleep(0.04)
            return ToolResult(status="ok", content={"written": True})

    path = tmp_path / "result.txt"
    tools = BlockingTool(path)
    model = ScriptedModel(call())
    k = kernel(model, configuration=config(effect_timeout_seconds=0.02), tools=tools)
    result = await drive(k)
    assert result.phase == RunPhase.INTERRUPTED
    assert result.reason == "effect_outcome_unknown"
    assert path.read_text() == "correct"
    assert len(tools.calls) == len(model.requests) == 1
    assert not any(event.kind == "run.completed" for event in k.events(result.run_id))


@pytest.mark.asyncio
async def test_mismatched_configuration_cannot_attach():
    k = kernel(ScriptedModel({"text": "unused"}))
    run = k.start(goal="work", session_id="s")
    other = kernel(k.model, store=k.store, configuration=config(max_repairs=1))
    with pytest.raises(ValueError, match="configuration"):
        other.attach(run.run_id)


@pytest.mark.asyncio
async def test_cancel_wakes_active_effect():
    started, released = asyncio.Event(), asyncio.Event()

    class Waiting:
        async def complete(self, request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()

    k = kernel(Waiting())
    run = k.start(goal="work", session_id="s")
    task = asyncio.create_task(k.run_until_idle(run.run_id))
    await asyncio.wait_for(started.wait(), 2)
    k.submit(run.run_id, RunCommand(command_id="cancel", kind="cancel"))
    result = await asyncio.wait_for(task, 2)
    assert result.phase == RunPhase.CANCELED
    assert released.is_set()


@pytest.mark.asyncio
async def test_steering_cannot_silently_rewrite_locked_acceptance():
    k = kernel(ScriptedModel({"text": "never"}))
    run = k.start(goal="original goal", session_id="s")
    k.submit(run.run_id, RunCommand(command_id="steer", kind="steer", payload={"text": "new goal"}))
    result = await k.run_until_idle(run.run_id)
    assert result.outcome == "blocked"
    assert result.reason == "contract_revision_required"
    assert result.goal == "original goal"
    assert result.model_calls == 0
