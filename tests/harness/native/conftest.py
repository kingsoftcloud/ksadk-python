"""Keep failed live-test diagnostics without changing test results or inputs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest


def _redact(text: str) -> str:
    for key, value in os.environ.items():
        if len(value) < 4:
            continue
        if any(part in key for part in ("KEY", "SECRET", "TOKEN", "BASE_URL")):
            text = text.replace(value, "<redacted>")
            if "BASE_URL" in key:
                try:
                    host = urlsplit(value).hostname
                except ValueError:
                    host = None
                if host:
                    text = text.replace(host, "<private-provider-host>")
    return text


def _runtime_diagnostics(call) -> list[dict]:
    """Read bounded counters from failed fixture frames, never model content."""
    if call.excinfo is None:
        return []
    from ksadk.harness.native.engine import NativeKernel

    rows = []
    for entry in call.excinfo.traceback:
        frame = entry.frame
        kernel = frame.f_locals.get("kernel")
        case = frame.f_globals.get("_CASE")
        run_id = getattr(case, "id", None) or frame.f_globals.get("_CASE_ID")
        if not isinstance(kernel, NativeKernel) or not isinstance(run_id, str):
            continue
        try:
            snapshot = kernel.snapshot(run_id)
        except (KeyError, ValueError, RuntimeError):
            continue
        rows.append(
            {
                "phase": snapshot.phase.value,
                "outcome": snapshot.outcome,
                "reason": snapshot.reason,
                "model_calls": snapshot.model_calls,
                "descendant_model_calls": snapshot.descendant_model_calls,
                "tool_calls": snapshot.tool_calls,
                "event_seq": snapshot.event_seq,
            }
        )
        if len(rows) >= 4:
            break
    return rows


def _durable_diagnostics(item) -> list[dict]:
    """Retain safe SQLite counters even when a fixture caught its inner error."""
    root = item.funcargs.get("tmp_path")
    if not isinstance(root, Path):
        return []
    paths = sorted(
        {*root.glob("run.sqlite"), *root.glob("*/run.sqlite"), *root.glob("*/*/run.sqlite")}
    )[:4]
    records = []
    for path in paths:
        try:
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
                states = []
                for (raw,) in database.execute("SELECT snapshot FROM native_runs LIMIT 4"):
                    value = json.loads(raw)
                    states.append(
                        {
                            key: value.get(key)
                            for key in (
                                "phase",
                                "outcome",
                                "reason",
                                "model_calls",
                                "tool_calls",
                                "descendant_model_calls",
                                "event_seq",
                            )
                        }
                    )
                failures = []
                for (raw,) in database.execute("SELECT data FROM native_receipts"):
                    value = json.loads(raw).get("result", {})
                    if value.get("response_kind") in {
                        "provider_retryable_error",
                        "provider_fatal_error",
                        "context_overflow",
                    }:
                        failures.append(
                            {
                                key: value.get(key)
                                for key in (
                                    "response_kind",
                                    "failure_kind",
                                    "retry_after_seconds",
                                )
                            }
                        )
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                protected = Path(tempfile.gettempdir()) / "ksadk-native-failure-receipts" / digest
                protected.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Keep the original database outside public artifacts; messages
                # and provider details never enter the safe diagnostic report.
                shutil.copy2(path, protected / "run.sqlite")
                records.append(
                    {
                        "database_sha256": digest,
                        "protected_relative_path": digest + "/run.sqlite",
                        "snapshots": states,
                        "provider_failures": failures[:16],
                        "provider_failure_count": len(failures),
                    }
                )
        except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
            continue
    return records


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not report.failed:
        return
    targets = {
        Path(value)
        for key, value in os.environ.items()
        if key.startswith("KSADK_NATIVE_") and key.endswith("_REPORT") and value.endswith(".json")
    }
    if not targets:
        return
    record = {
        "schema_version": "native-live-test-failure-diagnostic/v1",
        "test_id": _redact(item.nodeid),
        "stage": report.when,
        "pytest_outcome": report.outcome,
        "exception_type": call.excinfo.typename if call.excinfo else None,
        "duration_seconds": report.duration,
        "diagnostic": _redact(report.longreprtext),
        "runtime_diagnostics": _runtime_diagnostics(call),
        "durable_diagnostics": _durable_diagnostics(item),
        "observer_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "verdict_unchanged": True,
    }
    for target in targets:
        path = target.with_name(f"{target.stem}.{report.when}.failure.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        except OSError:
            # A diagnostic write must not hide or replace the actual failure.
            print("native_live_failure_diagnostic_write_failed", flush=True)
