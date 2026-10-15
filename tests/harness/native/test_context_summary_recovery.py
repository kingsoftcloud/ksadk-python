"""A failed summary can shrink unconsumed input without discarding its source."""

import json

import pytest

from ksadk.harness.native.context_driver import ContextDriver
from ksadk.harness.native.context_partition import projection_digest
from ksadk.harness.native.context_runtime import (
    CompactionController,
    CompactionPolicy,
    CompactionRequest,
    ContextPlanner,
)
from ksadk.harness.native.context_summary_recovery import repartition_failed_summary
from ksadk.harness.native.model import normalize_response
from ksadk.harness.native.ports import digest
from ksadk.harness.native.state import RunPhase, RunSnapshot
from tests.harness.native.test_context_runtime import count_tokens, typed_count
from tests.harness.native.test_kernel import SCOPE, config


def prepared_summary(*, max_retries=1, calls=12, messages=None, media=False):
    cfg = config(max_model_calls=calls, finalization_reserve_model_calls=1)
    cfg = cfg.model_copy(update={"profile": cfg.profile.model_copy(update={
        "context_window": 8000, "max_output_tokens": 1024, "max_retries": max_retries,
    })})
    planner = ContextPlanner(
        count_tokens=count_tokens, tokenizer_id="summary-recovery-counterexample/v1",
        count_message_tokens=typed_count if media else None,
        policy=CompactionPolicy(
            summary_max_tokens=1024, recent_tool_batch_count=0, read_working_set_count=0,
            trigger_ratio=0.4, target_ratio=0.2,
        ),
    )
    driver = ContextDriver(planner, "fixture")
    raw = messages or (
        {"role": "user", "content": "Investigate and preserve all pending work."},
        *({"role": "assistant", "content": f"Observation {i}: " + "source evidence " * 200}
          for i in range(4)),
    )
    snapshot = RunSnapshot(
        scope=SCOPE, run_id="minimal-summary", session_id="s", turn_id="t",
        goal=raw[0]["content"], contract_digest=digest(cfg.contract),
        task_contract=cfg.contract.model_dump(mode="json"),
        phase=RunPhase.PREPARING_CONTEXT, messages=raw, force_compaction=True,
    )
    prepared = driver.prepare(snapshot, cfg).snapshot
    assert prepared.phase == RunPhase.COMPACTING
    return driver, prepared, cfg


def result(*, finish="length", text="", n=1):
    return normalize_response(
        request_id=f"effect-{n}", attempt_id=f"effect-{n}:a", text=text, finish_reason=finish,
    )


@pytest.mark.parametrize("finish", ["length", "context_overflow"])
def test_failed_summary_repartitions_unconsumed_source_with_original_budget(finish):
    driver, before, cfg = prepared_summary()
    previous = before.compaction_request
    after = driver.seal(before, result(finish=finish), cfg)
    assert after.phase == RunPhase.COMPACTING
    current = after.compaction_request
    assert current["summary_messages"] != previous["summary_messages"]
    assert current["summary_counted_input_tokens"] < previous["summary_counted_input_tokens"]
    assert current["summary_output_tokens"] == previous["summary_output_tokens"] == 1024
    assert current["profile"] == previous["profile"]
    assert after.model_calls == 1 and after.model_errors == 1
    assert after.messages == before.messages
    assert after.compaction_history == before.compaction_history
    assert current["summary_partition"]["accepted"] == previous["summary_partition"]["accepted"]
    assert current["summary_partition"]["covered_count"] == 0
    body = json.loads(current["summary_messages"][0]["content"])
    refs = tuple(row["source_message_ref"] for row in body["transcript"])
    refs += tuple(row["ref"] for row in current["remaining_history_entries"])
    old_body = json.loads(previous["summary_messages"][0]["content"])
    assert refs == tuple(row["source_message_ref"] for row in old_body["transcript"])
    exhausted = driver.seal(after, result(finish=finish, n=2), cfg)
    assert exhausted.reason == "compaction_summary_retry_exhausted"
    assert exhausted.model_calls == 2 and exhausted.model_errors == 2


@pytest.mark.parametrize("finish", ["length", "context_overflow"])
def test_repartitioned_chunks_cover_original_source_and_retain_prior_summary(finish):
    driver, before, cfg = prepared_summary()
    smaller = driver.seal(before, result(finish=finish), cfg)
    first = driver.seal(
        smaller, result(finish="stop", text="First sources remain pending.", n=2), cfg
    )
    assert first.phase == RunPhase.COMPACTING and first.model_errors == 0
    accepted = first.compaction_request["summary_partition"]["accepted"]
    assert len(accepted) == 1 and accepted[0]["start"] == 0 and accepted[0]["stop"] == 2
    assert first.compaction_request["summary_output_tokens"] == 1024
    previous_summary = json.loads(first.compaction_request["summary_messages"][0]["content"])[
        "previous_summary"
    ]
    second_smaller = driver.seal(first, result(finish=finish, n=3), cfg)
    current = second_smaller.compaction_request
    assert current["summary_partition"]["accepted"] == accepted
    assert current["summary_partition"]["covered_count"] == 2
    body = json.loads(current["summary_messages"][0]["content"])
    assert body["previous_summary"] == previous_summary
    for n in range(4, 7):
        second_smaller = driver.seal(
            second_smaller,
            result(finish="stop", text="All original observations remain pending.", n=n), cfg,
        )
        if second_smaller.phase != RunPhase.COMPACTING:
            break
        assert second_smaller.compaction_request["summary_output_tokens"] == 1024
    assert second_smaller.phase == RunPhase.PREPARING_CONTEXT
    assert second_smaller.compaction_history["count"] == 1
    assert second_smaller.messages == before.messages
    assert second_smaller.compaction_request is None
    assert second_smaller.model_calls == 5


def test_quote_packing_keeps_full_output_and_continuation_reserve():
    driver, before, cfg = prepared_summary()
    smaller = driver.seal(before, result(), cfg)
    quote = smaller.compaction_request["remaining_history_entries"][0]
    for continuations in (0, 1):
        profile = cfg.profile.model_copy(update={"max_continuations": continuations})
        assert not driver.planner._summary_unit_fits(7999, profile, (quote,))
        assert driver.planner._summary_unit_fits(4000, profile, (quote,))


def test_repartition_does_not_borrow_last_delivery_call():
    driver, before, cfg = prepared_summary(calls=2)
    smaller = driver.seal(before, result(), cfg)
    assert smaller.phase == RunPhase.COMPACTING and smaller.model_calls == 1
    delivery = driver.begin_summary(smaller, cfg).snapshot
    assert delivery.phase == RunPhase.PREPARING_CONTEXT
    assert delivery.compaction_request is None and not delivery.compaction_history
    assert delivery.model_calls == 1 and delivery.messages == before.messages


@pytest.mark.parametrize("finish", ["length", "context_overflow"])
def test_zero_retry_budget_never_repartitions(finish):
    driver, before, cfg = prepared_summary(max_retries=0)
    failed = driver.seal(before, result(finish=finish), cfg)
    assert failed.reason == "compaction_summary_retry_exhausted"
    assert failed.compaction_request == before.compaction_request
    assert failed.model_calls == 1


def test_partial_summary_continuation_cannot_be_repartitioned():
    driver, before, cfg = prepared_summary()
    request = CompactionRequest.model_validate(before.compaction_request)
    request = request.model_copy(update={"profile": request.profile.model_copy(update={
        "max_continuations": 1,
    })})
    kwargs = dict(current_revision=before.revision,
                  current_continuity=driver.continuity(before, cfg),
                  current_messages=before.messages)
    continued = CompactionController(driver.planner).continue_summary(request, "Partial ", **kwargs)
    assert repartition_failed_summary(driver.planner, continued, **kwargs) is None


@pytest.mark.parametrize("mutation", ["source", "coverage", "quote"])
def test_repartition_rejects_changed_source_or_coverage(mutation):
    driver, before, cfg = prepared_summary()
    broken = before.model_copy(deep=True)
    if mutation == "source":
        broken.messages[1]["content"] = "Changed source"
    elif mutation == "coverage":
        broken.compaction_request["summary_partition"]["covered_count"] = 1
    else:
        broken.compaction_request["summary_messages"][0]["content"] += " "
    failed = driver.seal(broken, result(), cfg)
    assert failed.phase == RunPhase.FINALIZING and failed.outcome == "blocked"
    assert failed.compaction_request == broken.compaction_request


@pytest.mark.parametrize("receipts", [1, 3])
def test_single_atomic_tool_batch_is_not_split_or_labeled_overflow(receipts):
    raw = (
        {"role": "user", "content": "Investigate and preserve all pending work."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call-{n}", "type": "function", "function": {
                "name": "write", "arguments": "{}",
            }} for n in range(receipts)
        ]},
        *({"role": "tool", "tool_call_id": f"call-{n}",
           "content": "complete receipt " * 100} for n in range(receipts)),
    )
    driver, before, cfg = prepared_summary(messages=raw)
    empty = driver.seal(before, result(), cfg)
    assert empty.phase == RunPhase.COMPACTING
    assert empty.compaction_request == before.compaction_request
    assert empty.model_errors == 1 and empty.reason is None
    overflow = driver.seal(before, result(finish="context_overflow"), cfg)
    assert overflow.reason == "compaction_summary_incomplete"
    assert overflow.compaction_request == before.compaction_request


class SummaryRecoveryModel:
    def __init__(self, *, failed=False):
        self.requests = []
        self.failed = failed

    async def complete(self, request):
        self.requests.append(request)
        identity = dict(request_id=request.request_id, attempt_id=request.attempt_id)
        if request.purpose == "compaction":
            if not self.failed:
                self.failed = True
                return normalize_response(**identity, text="", finish_reason="length")
            return normalize_response(
                **identity, text="All observations still require verification."
            )
        return normalize_response(**identity, text="Delivery remains unverified.")


def recovery_kernel(store, model, *, create=False, lease=None, attach=True):
    from ksadk.harness.native.engine import NativeKernel

    driver, prepared, cfg = prepared_summary()
    kernel = NativeKernel(
        store=store, scope=SCOPE, configuration=cfg, model=model,
        context_planner=driver.planner, workspace_revision="fixture", lease_ttl_seconds=lease,
    )
    if create:
        store.create(prepared.model_copy(update={
            "revision": 0, "event_seq": 0, "configuration_digest": cfg.digest,
            "profile_digest": cfg.profile.digest, "context_binding": kernel.context.binding,
        }))
    if attach:
        kernel.attach(prepared.run_id)
    return kernel, prepared.run_id


@pytest.mark.asyncio
async def test_saved_failed_receipt_repartitions_once_after_sqlite_reopen(tmp_path):
    from ksadk.harness.native.state import EffectReceipt
    from ksadk.harness.native.transitions import commit_transition
    from ksadk.harness.stores.native_sqlite import SQLiteRunStore

    path = tmp_path / "receipt.sqlite"
    store = SQLiteRunStore(path)
    initial_model = SummaryRecoveryModel()
    kernel, run_id = recovery_kernel(store, initial_model, create=True)
    prepared = await kernel.advance(run_id)
    key = prepared.pending_intent
    intent = store.get_intent(SCOPE, run_id, key)
    assert intent.status == "prepared"
    store.claim_intent(SCOPE, run_id, key, fencing_token=kernel.fencing_token(run_id))
    response = normalize_response(request_id=key, attempt_id=f"{key}:attempt:1",
                                  text="", finish_reason="length")
    receipt = EffectReceipt(operation_key=key, status="committed",
                            result=response.model_dump(mode="json"))
    batch = commit_transition(prepared, prepared, "fixture.receipt_saved", receipts=(receipt,))
    saved = store.commit(SCOPE, batch, expected_revision=prepared.revision,
                         fencing_token=kernel.fencing_token(run_id))
    kernel.release(run_id)
    store.close()
    with SQLiteRunStore(path) as restored:
        model = SummaryRecoveryModel(failed=True)
        kernel, run_id = recovery_kernel(restored, model)
        reduced = await kernel.advance(run_id)
        assert not model.requests and reduced.model_calls == 1 and reduced.model_errors == 1
        assert reduced.compaction_request != saved.compaction_request
        assert restored.get_receipt(SCOPE, run_id, key) == receipt
        result_snapshot = await kernel.run_until_idle(run_id)
        assert result_snapshot.compaction_history["count"] == 1
        assert result_snapshot.model_calls == len(model.requests) + 1 == 4
        assert result_snapshot.messages[:len(saved.messages)] == saved.messages
        assert sum(e.kind == "context.compaction_retry_scheduled"
                   for e in kernel.events(run_id)) == 1
        assert sum(e.kind == "usage.unavailable" and e.payload["request_id"] == key
                   for e in kernel.events(run_id)) == 1
        assert result_snapshot.outcome != "verified"
        kernel.release(run_id)


@pytest.mark.parametrize("missing", ["planner", "request"])
def test_missing_summary_state_blocks_without_an_unhandled_exception(missing):
    driver, before, cfg = prepared_summary()
    if missing == "planner":
        driver.planner = None
    else:
        before = before.model_copy(update={"compaction_request": None})
    failed = driver.seal(before, result(), cfg)
    assert failed.phase == RunPhase.FINALIZING and failed.outcome == "blocked"
    assert failed.model_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("repartitioned", [False, True])
async def test_lost_running_summary_remains_unknown_without_dispatch(tmp_path, repartitioned):
    from ksadk.harness.stores.native_sqlite import SQLiteRunStore

    path = tmp_path / "running.sqlite"
    store = SQLiteRunStore(path)
    first_model = SummaryRecoveryModel()
    kernel, run_id = recovery_kernel(store, first_model, create=True)
    await kernel.advance(run_id)
    if repartitioned:
        await kernel.advance(run_id)
        await kernel.advance(run_id)
    before = kernel.snapshot(run_id)
    store.claim_intent(SCOPE, run_id, before.pending_intent,
                       fencing_token=kernel.fencing_token(run_id))
    kernel.release(run_id)
    store.close()
    with SQLiteRunStore(path) as restored:
        model = SummaryRecoveryModel(failed=True)
        kernel, run_id = recovery_kernel(restored, model)
        final = await kernel.run_until_idle(run_id)
        assert not model.requests
        assert final.reason == "effect_outcome_unknown" and final.outcome != "verified"
        assert final.model_calls == before.model_calls + 1
        assert final.messages == before.messages
        assert final.compaction_request == before.compaction_request
        receipt = restored.get_receipt(SCOPE, run_id, before.pending_intent)
        assert receipt.status == "uncertain"
        kernel.release(run_id)


def test_whole_batch_suffix_remains_intact_after_summary_repartition():
    raw = (
        {"role": "user", "content": "Preserve all observations."},
        {"role": "assistant", "content": "Earlier observation " * 100},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call-{n}", "type": "function", "function": {
                "name": "write", "arguments": "{}",
            }} for n in range(2)
        ]},
        *({"role": "tool", "tool_call_id": f"call-{n}",
           "content": f"receipt-{n} " * 100} for n in range(2)),
        {"role": "assistant", "content": "Later observation " * 100},
    )
    driver, before, cfg = prepared_summary(messages=raw)
    smaller = driver.seal(before, result(), cfg)
    queued = smaller.compaction_request["remaining_history_entries"]
    assert len({entry["unit"] for entry in queued[:3]}) == 1
    assert queued[0]["message"]["tool_calls"] == before.messages[2]["tool_calls"]
    assert [entry["message"]["tool_call_id"] for entry in queued[1:3]] == ["call-0", "call-1"]
    assert all(entry["summary_retry_quote"] == "complete-quoted-summary-retry/v1"
               for entry in queued)
    following = driver.seal(
        smaller, result(finish="stop", text="Earlier observation retained."), cfg
    )
    body = json.loads(following.compaction_request["summary_messages"][0]["content"])
    assert [row["tool_call_id"] for row in body["transcript"] if row["role"] == "tool"] == [
        "call-0", "call-1",
    ]


@pytest.mark.parametrize("tampered", [False, True])
def test_repartition_keeps_source_owned_media_with_its_quoted_unit(tampered):
    image = {"type": "image_url", "image_url": {
        "url": "data:image/png;base64,ZmFrZQ==", "detail": "auto",
    }}
    raw = (
        {"role": "user", "content": "Preserve the image observations."},
        *({"role": "assistant", "content": [
            {"type": "text", "text": f"Observation {n}: " + "image evidence " * 100}, image,
        ]} for n in range(4)),
    )
    driver, before, cfg = prepared_summary(messages=raw, media=True)
    if tampered:
        # A matching envelope digest does not turn replacement image bytes
        # into original source. Repartition must decline that forged view.
        request = before.compaction_request
        request["summary_messages"][0]["content"][2]["image_url"]["url"] = (
            "data:image/png;base64,b3RoZXI="
        )
        request["summary_partition"]["pending"]["projection_digest"] = projection_digest(
            request["summary_messages"]
        )
        unchanged = driver.seal(before, result(), cfg)
        assert unchanged.compaction_request == before.compaction_request
        assert unchanged.phase == RunPhase.COMPACTING and unchanged.model_errors == 1
        return
    smaller = driver.seal(before, result(), cfg)
    assert smaller.phase == RunPhase.COMPACTING
    content = smaller.compaction_request["summary_messages"][0]["content"]
    assert isinstance(content, list) and content[2] == image and content[4] == image
    queued = smaller.compaction_request["remaining_history_entries"]
    assert len(queued) == 2 and all(entry["images"] == [image] for entry in queued)
    following = driver.seal(smaller, result(finish="stop", text="Earlier images preserved."), cfg)
    next_content = following.compaction_request["summary_messages"][0]["content"]
    assert next_content[2] == image and next_content[4] == image
    assert before.messages == raw
