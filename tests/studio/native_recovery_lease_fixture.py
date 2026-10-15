"""Explicit test-only lease modes and passive evidence from the real SQLite lease.

The product default, task contract and budgets are never changed. Short mode
retains the existing fault fixture; production mode passes the Composer TTL
through unchanged. SQL below is read-only, scoped and never advances a clock.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from ksadk.harness.native import effect_control, effect_runtime
from ksadk.harness.native.state import RunScope
from ksadk.harness.stores.native_contract import LeaseBusy, encode
from ksadk.harness.native_facade import NativeHarnessEngine

MODE_VARIABLE = "KSADK_NATIVE_RECOVERY_LEASE_MODE"


def recovery_lease_mode() -> str:
    mode = os.environ.get(MODE_VARIABLE, "short")
    if mode not in {"short", "production"}:
        raise ValueError(f"{MODE_VARIABLE} must be short or production")
    return mode


def fault_ttl() -> float:
    return 30.0 if recovery_lease_mode() == "production" else 0.6


class RecoveryLeaseEngine(NativeHarnessEngine):
    def __init__(self, **kwargs):
        assert kwargs["lease_ttl_seconds"] == 30, "product lease default changed"
        if recovery_lease_mode() == "short":
            kwargs = {**kwargs, "lease_ttl_seconds": 0.6}
        super().__init__(**kwargs)


def _lease_row(store, scope, run_id):
    with store._transaction() as connection:
        row = connection.execute(
            "SELECT r.fence, l.owner_id, l.expires_at FROM native_runs AS r "
            "LEFT JOIN native_run_leases AS l ON l.scope=r.scope AND l.run_id=r.run_id "
            "WHERE r.scope=? AND r.run_id=?",
            (encode(scope), run_id),
        ).fetchone()
    assert row is not None, "fault target run is absent"
    return {"fence": row[0], "owner_id": row[1], "expires_at": row[2]}


def lease_marker(store, scope, run_id, *, mode=None):
    mode = recovery_lease_mode() if mode is None else mode
    assert mode in {"short", "production"}
    observed_at = time.time()
    row = _lease_row(store, scope, run_id)
    if mode == "production":
        assert row["expires_at"] is not None and row["expires_at"] > observed_at, (
            "production fault must capture a still-owned lease"
        )
    return {
        "lease_mode": mode,
        "lease_ttl_seconds": 30.0 if mode == "production" else 0.6,
        "original_lease": {**row, "observed_at": observed_at},
    }


def _write(path, payload):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        json.dump(payload, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


class RecoveryLeaseAudit:
    """Observe acquisition, including rejected attempts, without changing it.

    Record initial facts before starting recovery and update after each attempt,
    so a failed or interrupted recovery leaves evidence. This does not claim
    to observe every downstream network transport or every child lease.
    """

    def __init__(self, store, marker, directory):
        self.store = store
        self.scope = RunScope.model_validate(marker["scope"])
        self.run_id = marker["handle"]["run_id"]
        self.original = marker["original_lease"]
        self.mode = marker["lease_mode"]
        self.path = Path(directory) / "recovery-lease.json"
        if self.path.exists():
            raise ValueError("recovery lease evidence already exists")
        self.rows = []
        self.dispatches = []
        self.dispatch_observer_installed = False
        self.started_at = time.time()
        self.initial_digest = self._state_digest()
        self.initial_row = _lease_row(store, self.scope, self.run_id)
        assert self.initial_row == {k: self.original[k] for k in self.initial_row}
        self.validated = False
        self.capture_errors = []
        self.no_dispatch_reason = None
        self._save()
        original_acquire = store.acquire_lease

        def observe(scope, run_id, **kwargs):
            if scope != self.scope or run_id != self.run_id:
                return original_acquire(scope, run_id, **kwargs)
            before = self._observe(
                "lease_before", lambda: _lease_row(store, scope, run_id)
            )
            began = time.time()
            row = {
                "started_at": began,
                "previous": before,
                "requested_ttl_seconds": kwargs["ttl_seconds"],
            }
            try:
                token = original_acquire(scope, run_id, **kwargs)
            except LeaseBusy:
                row.update(
                    status="busy",
                    observed_at=time.time(),
                    state_unchanged=(
                        self._observe("busy_state", self._state_digest) == self.initial_digest
                    ),
                )
                self.rows.append(row)
                self._save_observation()
                raise
            except BaseException as error:
                row.update(status="error", error_type=type(error).__name__)
                self.rows.append(row)
                self._save_observation()
                raise
            row.update(
                status="acquired",
                observed_at=time.time(),
                fencing_token=token,
                current=self._observe("lease_after", lambda: _lease_row(store, scope, run_id)),
            )
            self.rows.append(row)
            self._save_observation()
            return token

        store.acquire_lease = observe

    def observe_dispatch(self, monkeypatch):
        """Observe the real dispatcher entry and recursive parallel effects.

        Both module bindings are required: effect_control imports the function,
        while parallel tool calls recurse via effect_runtime's global binding.
        Always delegate unchanged; validation rejects early calls after recording
        the product's actual behavior instead of masking it with an interception.
        """
        assert not self.dispatch_observer_installed

        def wrap(original, entrypoint):
            async def observe(snapshot, intent, *args, **kwargs):
                if snapshot.scope != self.scope or snapshot.run_id != self.run_id:
                    return await original(snapshot, intent, *args, **kwargs)
                began = time.time()
                row = {
                    "scope": snapshot.scope.model_dump(mode="json"),
                    "run_id": snapshot.run_id,
                    "operation_key": intent.operation_key,
                    "kind": intent.kind,
                    "entrypoint": entrypoint,
                    "started_at": began,
                    "before_original_expiry": self.original["expires_at"] is not None
                    and began < self.original["expires_at"],
                    "status": "started",
                }
                self.dispatches.append(row)
                self._save_observation()
                try:
                    result = await original(snapshot, intent, *args, **kwargs)
                except BaseException as error:
                    row.update(status="raised", error_type=type(error).__name__)
                    raise
                else:
                    row["status"] = "returned"
                    return result
                finally:
                    row["finished_at"] = time.time()
                    self._save_observation()
            return observe

        for module in (effect_control, effect_runtime):
            monkeypatch.setattr(
                module, "dispatch_effect", wrap(module.dispatch_effect, module.__name__)
            )
        self.dispatch_observer_installed = True
        self._save_observation()

    def _observe(self, operation, callback):
        try:
            return callback()
        except Exception as error:
            self.validated = False
            self.capture_errors.append({"operation": operation,
                                        "error_type": type(error).__name__})
            return None

    def _save_observation(self):
        self._observe("save", self._save)

    def _state_digest(self):
        payload = {
            "snapshot": self.store.load(self.scope, self.run_id).model_dump(mode="json"),
            "events": [
                e.model_dump(mode="json") for e in self.store.events(self.scope, self.run_id)
            ],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _save(self):
        _write(self.path, self.summary())

    def summary(self):
        return {
            "schema_version": "native-product-recovery-lease/v1",
            "lease_mode": self.mode,
            "target_scope": self.scope.model_dump(mode="json"),
            "target_run_id": self.run_id,
            "original_lease": self.original,
            "recovery_started_at": self.started_at,
            "acquisitions": self.rows,
            "dispatches": self.dispatches,
            "dispatch_observer_installed": self.dispatch_observer_installed,
            "no_dispatch_reason": self.no_dispatch_reason,
            "production_window_observed": self.mode == "production"
            and self.original["expires_at"] is not None
            and self.started_at < self.original["expires_at"],
            "validated": self.validated,
            "capture_errors": self.capture_errors,
            "scope": ("parent SQLite lease and Native dispatcher; "
                      "not downstream transport or child leases"),
        }

    def validate(self, *, no_dispatch_reason=None):
        if no_dispatch_reason not in {None, "mcp_outcome_unknown"}:
            raise ValueError("unsupported no-dispatch recovery reason")
        self.validated = False
        self.no_dispatch_reason = no_dispatch_reason
        self._save_observation()
        assert not self.capture_errors, "recovery evidence capture is incomplete"
        if self.mode == "production":
            assert not any(row["before_original_expiry"] for row in self.dispatches), (
                "Native effect dispatched before original lease expiry"
            )
            expiry = self.original["expires_at"]
            assert expiry is not None and self.started_at < expiry, (
                "original production lease expired before recovery observation"
            )
            busy = [r for r in self.rows if r["status"] == "busy"]
            acquired = [r for r in self.rows if r["status"] == "acquired"]
            assert busy and all(r["state_unchanged"] for r in busy)
            assert acquired, "no actual lease acquisition was observed"
            first = acquired[0]
            assert (first["current"]["expires_at"] - first["requested_ttl_seconds"]) >= expiry, (
                "lease takeover began before original expiry"
            )
            assert first["requested_ttl_seconds"] == 30
            assert first["fencing_token"] > self.original["fence"]
            assert first["current"]["fence"] == first["fencing_token"]
            assert first["current"]["owner_id"] != self.original["owner_id"]
            assert self.dispatch_observer_installed, "Native dispatch observer was not installed"
            if no_dispatch_reason is None:
                assert self.dispatches, "no actual Native effect dispatch was observed"
        self.validated = True
        self._save_observation()
        assert not self.capture_errors, "recovery evidence capture is incomplete"
