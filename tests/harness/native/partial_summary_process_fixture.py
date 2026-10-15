"""Deterministic port and exact SQLite commit seam for real SIGKILL."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.model import normalize_response
from ksadk.harness.stores.native_sqlite import SQLiteRunStore
from tests.harness.native.test_context_summary_recovery import prepared_summary
from tests.harness.native.test_kernel import SCOPE
from tests.studio.native_recovery_lease_fixture import lease_marker

EVENT = "context.compaction_partial_sealed"
SUMMARY = "Completed earlier observations; verification remains pending."


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def raw_messages():
    return (
        {"role": "user", "content": "Preserve all observations."},
        {"role": "assistant", "content": "Earlier observation " * 100},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call-{n}", "type": "function", "function": {
                "name": "write", "arguments": "{}",
            }} for n in range(2)
        ]},
        *({"role": "tool", "tool_call_id": f"call-{n}",
           "content": f"receipt-{n} " * 100 + f"TAIL-{n}"} for n in range(2)),
        {"role": "assistant", "content": "Later observation " * 100 + "FINAL-SOURCE-TAIL"},
    )


class RecordedModel:
    """No provider: two deterministic summaries, then an unverified delivery."""

    def __init__(self, directory, *, resumed=False):
        self.directory = directory
        self.resumed = resumed
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        n = len(self.requests)
        assert request.effective_max_output_tokens == 1024
        if self.resumed:
            assert request.purpose == "task", "completed summary must never be dispatched again"
        if request.purpose == "compaction":
            assert n <= 2 and not request.tools
            text, finish = ("", "length") if n == 1 else (SUMMARY, "stop")
        else:
            assert request.purpose == "task" and not request.tools
            assert request.turn_control.reason == "model_budget_reserved"
            raw = raw_messages()
            assert request.messages[-4:] == raw[2:]
            assert raw[1] not in request.messages and SUMMARY in str(request.messages)
            text, finish = "Delivery from retained sources remains unverified.", "stop"
        value = normalize_response(
            request_id=request.request_id, attempt_id=request.attempt_id,
            text=text, finish_reason=finish,
        )
        write(self.directory / f"model-{n:02d}.json", {
            "request": request.model_dump(mode="json"), "response": value.model_dump(mode="json"),
        })
        return value


class ForbiddenTools:
    async def execute(self, _request):
        raise AssertionError("historical tool receipts must not execute again")


def make_kernel(store, model, *, create=False, attach=True):
    driver, initial, cfg = prepared_summary(calls=3, messages=raw_messages())
    assert cfg.limits.max_model_calls == 3 and cfg.limits.finalization_reserve_model_calls == 1
    assert cfg.profile.context_window == 8000 and cfg.profile.max_output_tokens == 1024
    kernel = NativeKernel(
        store=store, scope=SCOPE, configuration=cfg, model=model, tools=ForbiddenTools(),
        context_planner=driver.planner, workspace_revision="fixture", lease_ttl_seconds=30,
    )
    if create:
        store.create(initial.model_copy(update={
            "revision": 0, "event_seq": 0, "configuration_digest": cfg.digest,
            "profile_digest": cfg.profile.digest, "context_binding": kernel.context.binding,
        }))
    if attach:
        kernel.attach(initial.run_id)
    return kernel, initial.run_id


def export(store, run_id, path):
    events = store.events(SCOPE, run_id)
    keys = tuple(dict.fromkeys(e.payload["operation_key"] for e in events
                              if e.kind in {"effect.prepared", "effect.received"}))
    value = {
        "snapshot": store.load(SCOPE, run_id).model_dump(mode="json"),
        "events": [e.model_dump(mode="json") for e in events],
        "intents": {key: store.get_intent(SCOPE, run_id, key).model_dump(mode="json")
                    for key in keys},
        "receipts": {key: receipt.model_dump(mode="json") if (receipt := store.get_receipt(
            SCOPE, run_id, key)) else None for key in keys},
    }
    write(path, value)
    return value


class BoundaryStore(SQLiteRunStore):
    def __init__(self, directory, boundary, *, pause=None):
        super().__init__(directory / "partial.sqlite")
        self.directory, self.boundary = directory, boundary
        self.pause = pause or threading.Event().wait
        self.reached = False

    def commit(self, scope, batch, *, expected_revision, fencing_token):
        target = any(e.kind == EVENT for e in batch.events)
        if target:
            assert not self.reached and not batch.intents and not batch.receipts
            self.reached = True
            run_id = batch.snapshot.run_id
            assert batch.snapshot.compaction_history["count"] == 1
            assert batch.snapshot.model_calls == 2
            export(self, run_id, self.directory / "precommit-logical.json")
            write(self.directory / "proposal.json", batch.model_dump(mode="json"))
            if self.boundary == "before_commit":
                self.ready(scope, run_id, expected_revision, fencing_token)
        saved = super().commit(scope, batch, expected_revision=expected_revision,
                               fencing_token=fencing_token)
        if target and self.boundary == "after_commit":
            self.ready(scope, run_id, expected_revision, fencing_token)
        return saved

    def ready(self, scope, run_id, expected_revision, fencing_token):
        facts = export(self, run_id, self.directory / "boundary-logical.json")
        write(self.directory / "ready.json", {
            "scope": scope.model_dump(mode="json"), "handle": {"run_id": run_id},
            "boundary": self.boundary, "pid": os.getpid(), "ready_at": time.time(),
            "expected_revision": expected_revision, "fencing_token": fencing_token,
            "boundary_snapshot_sha256": hashlib.sha256(json.dumps(
                facts["snapshot"], sort_keys=True).encode()).hexdigest(),
            **lease_marker(self, scope, run_id, mode="production"),
        })
        self.pause()  # Parent SIGKILL; no release, close, lease change or in-process recovery.


async def main(directory, boundary):
    assert boundary in {"before_commit", "after_commit"}
    directory.mkdir(parents=True, exist_ok=False)
    store = BoundaryStore(directory, boundary)
    model = RecordedModel(directory)
    kernel, run_id = make_kernel(store, model, create=True)
    for _ in range(20):
        await kernel.advance(run_id)
    raise AssertionError("partial commit boundary was never reached")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
