"""Installed tools are capabilities, not per-goal acceptance evidence."""

import hashlib
from types import SimpleNamespace

import pytest

from ksadk.harness.native.completion import AcceptanceItem, TaskContract, reduce_completion
from ksadk.harness.native_runtime import TaskContractInput
from ksadk.studio.contracts import RunStatus
from ksadk.studio.native_final_shape import StudioFinalShapeVerifier
from ksadk.studio.native_product import _activation_contract, _goal_contract
from ksadk.studio.service import StudioService
from tests.studio.test_native_final_shape import SCOPE, request
from tests.studio.test_native_product_durability import (
    RecordingClient,
    _suspend_product,
    _wait_for,
)

EVIDENCE_REF = "studio.delivery-evidence-rejection"
EVIDENCE_VERSION = "1"


def contract(goal, *, parent=None):
    return _goal_contract(TaskContractInput(
        goal=goal, session_id="session", parent_run_id="parent" if parent else None,
        parent_goal="Previous web task" if parent else None, parent_contract=parent,
    ))


def verifier(tmp_path, *, changed=(), errors=(), observed=True, visual=False):
    # An existing page must not turn a Python edit into a web delivery.
    (tmp_path / "index.html").write_text("<p>existing page</p>")
    state = SimpleNamespace(
        messages=(), workspace_modified_paths=changed,
        unresolved_interaction_observation=None, last_web_mutation_tool_call=1,
        last_browser_observation={"entry_path": "index.html", "tool_call": 2}
        if observed else None,
    )
    calls = []

    class Browser:
        async def verify_page(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                status="ok", status_code=200, error_code=None, controls=(), page_errors=errors,
                console=(), network_failures=(), screenshot=None,
            )

    return StudioFinalShapeVerifier(
        SCOPE, build_id="build", workspace_root=tmp_path,
        store=SimpleNamespace(load=lambda *_: state), browser=Browser(),
        require_visual_feedback=visual,
    ), calls


def test_new_goals_pin_general_evidence_without_inheriting_parent_output_protocol():
    parent = contract("Build the page. Return only a JSON object.")
    current = contract("Fix the Python session manager.", parent=parent)
    item = current.items[0]
    assert (item.verifier_ref, item.verifier_version) == (EVIDENCE_REF, EVIDENCE_VERSION)
    assert item.required and item.verification_method == "deterministic"
    assert current.final_submission_schema is None
    assert current.final_submission_payload_key is None


@pytest.mark.parametrize("browser_available", [False, True])
def test_activation_anchor_preserves_legacy_configuration_for_cold_attach(browser_available):
    anchor = _activation_contract("activation", browser_available=browser_available)
    revision = hashlib.sha256(b"activation\0\0Initial activation contract").hexdigest()[:24]
    assert anchor.model_dump(mode="json") == {
        "revision": f"studio-goal-{revision}",
        "items": [{
            "acceptance_id": "current_user_goal", "required": True,
            "verifier_ref": "studio.browser-smoke-rejection" if browser_available
            else "studio.independent-evidence-unavailable",
            "verifier_version": "9" if browser_available else "1",
            "verification_method": "deterministic" if browser_available else "human",
            "description": "Initial activation contract",
        }],
        "allowed_tool_names": None, "final_submission_schema": None,
        "final_submission_payload_key": None, "host_resolved_answer": None,
    }
    assert contract("New real goal.").items[0].verifier_ref == EVIDENCE_REF


@pytest.mark.asyncio
async def test_python_changes_do_not_consume_browser_or_claim_semantic_success(tmp_path):
    port, calls = verifier(tmp_path, changed=("app/events.py",))
    task = contract("Fix the Python session manager.")
    result = await port.verify(request("unused", "Done.").model_copy(update={"contract": task}))
    assert calls == []
    assert result.report is not None
    item = result.report.items[0]
    assert item.status == "not_run" and not result.report.repairable
    assert "browser" not in item.safe_summary.lower()
    assert "semantics" in item.safe_summary.lower()
    assert reduce_completion(task, result.candidate, result.report).outcome == "unverified"


@pytest.mark.parametrize("checkpoint", [False, True])
@pytest.mark.asyncio
async def test_real_web_change_rejects_page_errors_at_final_and_checkpoint(tmp_path, checkpoint):
    port, calls = verifier(tmp_path, changed=("index.html",), errors=("ReferenceError: broken",))
    task = contract("Implement the requested feature.")
    req = request("unused", "Done.").model_copy(update={
        "contract": task, "purpose": "checkpoint" if checkpoint else "final",
    })
    result = await port.verify(req)
    assert len(calls) == 1 and calls[0]["entry_path"] == "index.html"
    assert result.report.items[0].status == "failed"
    assert result.report.repairable
    assert reduce_completion(task, result.candidate, result.report).action == "repair"


@pytest.mark.parametrize("observed,visual", [(True, False), (False, False), (True, True)])
@pytest.mark.asyncio
async def test_new_binding_keeps_browser_observation_and_visual_gates(tmp_path, observed, visual):
    port, calls = verifier(tmp_path, changed=("index.html",), observed=observed, visual=visual)
    task = contract("Build the requested page.")
    result = await port.verify(request("unused", "Done.").model_copy(update={"contract": task}))
    assert len(calls) == 1
    expected = "not_run" if observed and not visual else "failed"
    assert result.report.items[0].status == expected
    assert result.report.repairable is (expected == "failed")
    if expected == "not_run":
        assert reduce_completion(task, result.candidate, result.report).outcome == "unverified"


@pytest.mark.asyncio
async def test_explicit_output_shape_does_not_hide_web_failure(tmp_path):
    port, calls = verifier(tmp_path, changed=("index.html",), errors=("script error",))
    task = contract("Build the requested page. Return only a JSON object.")
    result = await port.verify(request("unused", '{"done":true}').model_copy(
        update={"contract": task}
    ))
    assert len(calls) == 1 and result.report.items[0].status == "failed"
    assert "Browser reported an error" in result.report.items[0].safe_summary


@pytest.mark.asyncio
async def test_legacy_shape_contract_is_not_silently_upgraded_to_browser_checks(tmp_path):
    port, calls = verifier(tmp_path, changed=("index.html",), errors=("script error",))
    legacy = request("Return only the total.", "42")
    result = await port.verify(legacy)
    assert calls == [] and result.report.items[0].status == "not_run"
    fresh = await port.verify(legacy.model_copy(
        update={"contract": contract("Return only the total.")}
    ))
    assert fresh.candidate.environment_digest != result.candidate.environment_digest
    assert fresh.report.items[0].status == "failed"


@pytest.mark.asyncio
async def test_non_web_checkpoint_does_not_start_a_repair_loop(tmp_path):
    port, calls = verifier(tmp_path, changed=("app/events.py",))
    result = await port.verify(request("unused", "").model_copy(update={
        "contract": contract("Fix Python code."), "purpose": "checkpoint",
    }))
    assert calls == [] and result.report is None


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [True, False])
async def test_continued_nested_game_does_not_verify_old_root_page(tmp_path, observed):
    port, calls = verifier(tmp_path, changed=("mario/game.js",), observed=observed)
    (tmp_path / "mario").mkdir()
    (tmp_path / "mario/index.html").write_text('<script src="game.js"></script>')
    (tmp_path / "mario/game.js").write_text("// updated game")
    state = port._store.load(SCOPE, "run")
    state.last_browser_observation = (
        {"entry_path": "mario/index.html", "tool_call": 2} if observed else None
    )
    task = contract("Continue the existing Mario game in mario/.")
    result = await port.verify(request("unused", "Done.").model_copy(update={"contract": task}))
    assert [call["entry_path"] for call in calls] == ["mario/index.html"]
    assert result.report.items[0].status == ("not_run" if observed else "failed")
    if observed:
        assert not result.report.repairable
        assert reduce_completion(task, result.candidate, result.report).outcome == "unverified"


@pytest.mark.asyncio
async def test_unbound_web_project_blocks_without_spending_game_repair_budget(tmp_path):
    port, calls = verifier(tmp_path, changed=("one/index.html", "two/index.html"))
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "index.html").write_text("<p>different project</p>")
    task = contract("Update the requested web project.")
    for purpose in ("checkpoint", "final"):
        result = await port.verify(request("unused", "Done.").model_copy(
            update={"contract": task, "purpose": purpose}
        ))
        assert calls == []
        assert result.report.items[0].status == "blocked"
        assert "entry" in result.report.items[0].safe_summary
        assert not result.report.repairable
        decision = reduce_completion(task, result.candidate, result.report)
        assert decision.reason == "required_acceptance_blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["mario/game.js", "mario/missing.html", "../outside.js"])
async def test_missing_or_unsafe_binding_never_falls_back_to_old_root_page(tmp_path, changed):
    port, calls = verifier(tmp_path, changed=(changed,))
    task = contract("Continue the nested project.")
    result = await port.verify(request("unused", "Done.").model_copy(update={"contract": task}))
    assert calls == []
    assert result.report.items[0].status == "blocked"
    assert not result.report.repairable


@pytest.mark.asyncio
async def test_continued_custom_entry_preserves_host_observed_page(tmp_path):
    port, calls = verifier(tmp_path, changed=("mario/src/game.js",))
    (tmp_path / "mario/src").mkdir(parents=True)
    (tmp_path / "mario/play.html").write_text('<script src="src/game.js"></script>')
    state = port._store.load(SCOPE, "run")
    state.last_browser_observation = {"entry_path": "mario/play.html", "tool_call": 2}
    task = contract("Continue the nested game.")
    result = await port.verify(request("unused", "Done.").model_copy(update={"contract": task}))
    assert [call["entry_path"] for call in calls] == ["mario/play.html"]
    assert result.report.items[0].status == "not_run"


@pytest.mark.asyncio
@pytest.mark.parametrize("reference,expected", [
    ('<script src="src/main.js?v=2"></script>', "not_run"),
    ('<script src="https://example.invalid/src/main.js"></script>', "blocked"),
])
async def test_root_project_requires_local_reference_to_nested_asset(tmp_path, reference, expected):
    port, calls = verifier(tmp_path, changed=("src/main.js",))
    (tmp_path / "index.html").write_text(reference)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.js").write_text("// current asset")
    task = contract("Continue this web project.")
    result = await port.verify(request("unused", "Done.").model_copy(update={"contract": task}))
    assert result.report.items[0].status == expected
    assert bool(calls) == (expected == "not_run")


@pytest.mark.asyncio
async def test_entry_symlink_cannot_select_an_outside_page(tmp_path):
    port, calls = verifier(tmp_path, changed=("mario/index.html",))
    (tmp_path / "mario").mkdir()
    outside = tmp_path.parent / (tmp_path.name + "-outside.html")
    outside.write_text("<p>outside</p>")
    (tmp_path / "mario/index.html").symlink_to(outside)
    task = contract("Continue the nested game.")
    result = await port.verify(request("unused", "Done.").model_copy(update={"contract": task}))
    assert calls == [] and result.report.items[0].status == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "incomplete", "http", "duplicate"])
async def test_host_browser_blockers_do_not_request_game_repairs(tmp_path, failure):
    port, calls = verifier(tmp_path, changed=("index.html",), observed=False)

    class Browser:
        async def verify_page(self, **kwargs):
            calls.append(kwargs)
            if failure == "exception":
                raise RuntimeError("private host error must not be disclosed")
            return SimpleNamespace(
                status="incomplete" if failure == "incomplete" else "error",
                status_code=404 if failure == "http" else None,
                error_code="browser_launch_control_ambiguous" if failure == "duplicate"
                else "observation_capture_failed_after_action",
                controls=(), page_errors=(), console=(), network_failures=(), screenshot=None,
            )

    port._browser = Browser()
    task = contract("Complete the game and verify it in the browser.")
    for purpose in ("final", "checkpoint"):
        result = await port.verify(request("unused", "Everything completed.").model_copy(
            update={"contract": task, "purpose": purpose}
        ))
        item = result.report.items[0]
        blocked = failure in {"exception", "incomplete"}
        assert item.status == ("blocked" if blocked else "failed")
        assert result.report.repairable is not blocked
        assert "private host error" not in item.safe_summary
        decision = reduce_completion(task, result.candidate, result.report)
        assert decision.reason == (
            "required_acceptance_blocked" if blocked else "required_acceptance_failed"
        )
        assert decision.action == ("finalize" if blocked else "repair")


@pytest.mark.asyncio
async def test_legacy_pending_run_restores_unchanged_then_new_turn_uses_new_binding(
    tmp_path, monkeypatch,
):
    from ksadk.studio import native_product

    def legacy_plain_contract(facts):
        revision = hashlib.sha256(
            f"{facts.session_id}\0{facts.parent_run_id or ''}\0{facts.goal}".encode()
        ).hexdigest()[:24]
        return TaskContract(revision=f"studio-goal-{revision}", items=(AcceptanceItem(
            acceptance_id="current_user_goal",
            verifier_ref="studio.independent-evidence-unavailable",
            verifier_version="1", verification_method="human", description=facts.goal[:1024],
        ),))

    # Freeze the prior plain-goal contract before shutdown, not rewrite its
    # SQLite row after the new reader starts. Model transport is a fixture;
    # Build, DSH activation, SQLite, approval and file execution are real.
    with monkeypatch.context() as prior:
        prior.setattr(native_product, "_goal_contract", legacy_plain_contract)
        root, original, scope, approval = await _suspend_product(tmp_path, monkeypatch)
    native_id = original.runtime_handle["run_id"]
    client = RecordingClient("The approved write is complete.")
    second = StudioService(root, model_client=client)
    try:
        before = second._native_product_composer.store.load(scope, native_id).task_contract
        await second.start()
        await second.run_service.recover_interrupted(second.resolve_run_spec)
        await _wait_for(lambda: (
            second.run_service._waiting_modes.get(original.id) == "resume"
            and second.event_store.get(original.id).status == RunStatus.WAITING_INPUT
        ) or second.event_store.get(original.id).status == RunStatus.INTERRUPTED)
        assert second.event_store.get(original.id).status == RunStatus.WAITING_INPUT
        assert second.event_store.get(original.id).runtime_handle == original.runtime_handle
        assert client.requests == [] and not list(root.rglob("approved.txt"))
        await second.run_service.submit_interaction(
            original.id, approval.data["approvalId"], name="approve", data={"decision": "approve"},
            expected_revision=1, idempotency_key="legacy-routing-approval",
        )
        await _wait_for(lambda: second.event_store.get(original.id).status == RunStatus.COMPLETED)
        await _wait_for(lambda: not second.run_service._recovery_tasks)
        restored = second._native_product_composer.store.load(scope, native_id)
        assert restored.task_contract == before
        assert len(list(root.rglob("approved.txt"))) == 1
        assert list(root.rglob("approved.txt"))[0].read_text() == "written once"
        following = await second.run_build(
            original.build_id, "Explain the result.", original.session_id
        )
        assert following.status == RunStatus.COMPLETED, following.error
        head = second._native_product_composer.store.session_head(scope, original.session_id)
        assert head.parent_run_id == native_id
        assert head.task_contract["items"][0]["verifier_ref"] == EVIDENCE_REF
        assert head.outcome == "unverified"
    finally:
        await second.aclose()
