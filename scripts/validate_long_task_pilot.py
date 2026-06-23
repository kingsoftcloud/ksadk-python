#!/usr/bin/env python3
"""Run the long-task pilot acceptance validation and emit a JSON report.

This wraps the lower-level checkpoint resume e2e script with the W3 acceptance
shape expected by the delivery plan. It intentionally avoids printing the DSN.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_checkpoint_resume_e2e import (
    run_cancel_validation,
    run_cancel_then_resume_validation,
    run_validation,
)


def _pass_fail(condition: bool) -> str:
    return "pass" if condition else "fail"


def _rate(condition: bool) -> float:
    return 1.0 if condition else 0.0


def _checkpoint_resume_passed(result: dict[str, Any]) -> bool:
    return (
        result.get("output_text") == "a,b,c"
        and int(result.get("checkpoint_count") or 0) >= 1
        and int(result.get("run_checkpoint_event_count") or 0) >= 2
        and int(result.get("run_resume_event_count") or 0) >= 1
        and result.get("resume_did_not_rerun_prior_nodes") is True
    )


def _runtime_cancel_passed(result: dict[str, Any] | None) -> bool:
    if not result:
        return False
    return (
        result.get("cancel_found") is True
        and str(result.get("cancel_status") or "") in {"cancelling", "cancelled"}
        and int(result.get("cancelled_event_count") or 0) >= 1
        and int(result.get("post_cancel_extra_event_count") or 0) == 0
    )


def _cancel_then_resume_passed(result: dict[str, Any] | None) -> bool:
    if not result:
        return False
    return (
        result.get("cancel_found") is True
        and str(result.get("cancel_status") or "") in {"cancelling", "cancelled"}
        and int(result.get("cancelled_event_count") or 0) >= 1
        and int(result.get("post_cancel_extra_event_count") or 0) == 0
        and result.get("output_text_after_resume") == "a,b,c"
        and result.get("resume_after_cancel_did_not_rerun_prior_nodes") is True
    )


async def _build_single_pilot_report(
    *,
    dsn: str,
    keep_session: bool,
    include_cancel: bool,
) -> dict[str, Any]:
    generated_at = datetime.now(UTC).isoformat()
    try:
        checkpoint_result = await run_validation(dsn=dsn, keep_session=keep_session)
    except Exception as exc:
        return {
            "report_type": "long_task_pilot_validation",
            "generated_at": generated_at,
            "overall_status": "fail",
            "cases": {
                "checkpoint_resume": {
                    "status": "fail",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                "runtime_cancel": {
                    "status": "skipped",
                    "reason": "checkpoint_resume failed before cancel validation",
                },
                "cancel_then_resume": {
                    "status": "skipped",
                    "reason": "checkpoint_resume failed before cancel-then-resume validation",
                },
            },
            "metrics": {
                "checkpoint_resume_success_rate": 0.0,
                "runtime_cancel_success_rate": None,
                "cancel_then_resume_success_rate": None,
                "checkpoint_event_count": None,
                "resume_event_count": None,
                "post_cancel_extra_event_count": None,
            },
            "acceptance": {
                "same_run_id_resume": "fail",
                "checkpoint_list_visible": "fail",
                "resume_does_not_restart": "fail",
                "runtime_cancel_terminal": "skipped",
                "no_events_after_cancel": "skipped",
                "cancel_then_resume_after_cancelled": "skipped",
                "resume_after_cancel_does_not_restart": "skipped",
            },
            "notes": [
                "DSN is intentionally omitted from this report.",
                "Provider/tool deep cancellation support must be reported as accepted or unsupported per runner/tool.",
            ],
        }

    cancel_result = None
    cancel_then_resume_result = None
    if include_cancel:
        try:
            cancel_result = await run_cancel_validation(dsn=dsn, keep_session=keep_session)
        except Exception as exc:
            cancel_result = {
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        try:
            cancel_then_resume_result = await run_cancel_then_resume_validation(
                dsn=dsn,
                keep_session=keep_session,
            )
        except Exception as exc:
            cancel_then_resume_result = {
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    checkpoint_ok = _checkpoint_resume_passed(checkpoint_result)
    cancel_ok = _runtime_cancel_passed(cancel_result) if include_cancel else None
    cancel_then_resume_ok = (
        _cancel_then_resume_passed(cancel_then_resume_result) if include_cancel else None
    )
    overall_ok = checkpoint_ok and (cancel_ok is not False) and (cancel_then_resume_ok is not False)

    cases: dict[str, Any] = {
        "checkpoint_resume": {
            "status": _pass_fail(checkpoint_ok),
            "session_id": checkpoint_result.get("session_id"),
            "run_id": checkpoint_result.get("run_id"),
            "checkpoint_id": checkpoint_result.get("checkpoint_id"),
            "output_text": checkpoint_result.get("output_text"),
            "checkpoint_count": checkpoint_result.get("checkpoint_count"),
            "run_checkpoint_event_count": checkpoint_result.get("run_checkpoint_event_count"),
            "run_resume_event_count": checkpoint_result.get("run_resume_event_count"),
            "checkpoint_log_before_resume": checkpoint_result.get("checkpoint_log_before_resume"),
            "node_counts_after_resume": checkpoint_result.get("node_counts_after_resume"),
            "resume_did_not_rerun_prior_nodes": checkpoint_result.get("resume_did_not_rerun_prior_nodes"),
        }
    }
    if include_cancel:
        cases["runtime_cancel"] = {
            "status": _pass_fail(bool(cancel_ok)),
            "error_type": cancel_result.get("error_type") if cancel_result else None,
            "error": cancel_result.get("error") if cancel_result else None,
            "session_id": cancel_result.get("session_id") if cancel_result else None,
            "invocation_id": cancel_result.get("invocation_id") if cancel_result else None,
            "cancel_found": cancel_result.get("cancel_found") if cancel_result else None,
            "cancel_status": cancel_result.get("cancel_status") if cancel_result else None,
            "cancelled_event_count": cancel_result.get("cancelled_event_count") if cancel_result else None,
            "post_cancel_extra_event_count": (
                cancel_result.get("post_cancel_extra_event_count") if cancel_result else None
            ),
        }
        cases["cancel_then_resume"] = {
            "status": _pass_fail(bool(cancel_then_resume_ok)),
            "error_type": (
                cancel_then_resume_result.get("error_type") if cancel_then_resume_result else None
            ),
            "error": cancel_then_resume_result.get("error") if cancel_then_resume_result else None,
            "session_id": (
                cancel_then_resume_result.get("session_id") if cancel_then_resume_result else None
            ),
            "run_id": cancel_then_resume_result.get("run_id") if cancel_then_resume_result else None,
            "invocation_id": (
                cancel_then_resume_result.get("invocation_id") if cancel_then_resume_result else None
            ),
            "checkpoint_id": (
                cancel_then_resume_result.get("checkpoint_id") if cancel_then_resume_result else None
            ),
            "cancel_found": (
                cancel_then_resume_result.get("cancel_found") if cancel_then_resume_result else None
            ),
            "cancel_status": (
                cancel_then_resume_result.get("cancel_status") if cancel_then_resume_result else None
            ),
            "cancelled_event_count": (
                cancel_then_resume_result.get("cancelled_event_count")
                if cancel_then_resume_result
                else None
            ),
            "post_cancel_extra_event_count": (
                cancel_then_resume_result.get("post_cancel_extra_event_count")
                if cancel_then_resume_result
                else None
            ),
            "output_text_after_resume": (
                cancel_then_resume_result.get("output_text_after_resume")
                if cancel_then_resume_result
                else None
            ),
            "checkpoint_log_before_cancel": (
                cancel_then_resume_result.get("checkpoint_log_before_cancel")
                if cancel_then_resume_result
                else None
            ),
            "node_counts_after_resume": (
                cancel_then_resume_result.get("node_counts_after_resume")
                if cancel_then_resume_result
                else None
            ),
            "resume_after_cancel_did_not_rerun_prior_nodes": (
                cancel_then_resume_result.get("resume_after_cancel_did_not_rerun_prior_nodes")
                if cancel_then_resume_result
                else None
            ),
        }
    else:
        cases["runtime_cancel"] = {
            "status": "skipped",
            "reason": "cancel validation disabled by --skip-cancel",
        }
        cases["cancel_then_resume"] = {
            "status": "skipped",
            "reason": "cancel validation disabled by --skip-cancel",
        }

    no_events_after_cancel = (
        cancel_result is not None and int(cancel_result.get("post_cancel_extra_event_count") or 0) == 0
    )
    cancel_then_resume_after_cancel = bool(cancel_then_resume_ok)
    return {
        "report_type": "long_task_pilot_validation",
        "generated_at": generated_at,
        "overall_status": _pass_fail(overall_ok),
        "cases": cases,
        "metrics": {
            "checkpoint_resume_success_rate": _rate(checkpoint_ok),
            "runtime_cancel_success_rate": _rate(bool(cancel_ok)) if include_cancel else None,
            "cancel_then_resume_success_rate": (
                _rate(bool(cancel_then_resume_ok)) if include_cancel else None
            ),
            "checkpoint_event_count": checkpoint_result.get("run_checkpoint_event_count"),
            "resume_event_count": checkpoint_result.get("run_resume_event_count"),
            "node_counts_after_resume": checkpoint_result.get("node_counts_after_resume"),
            "post_cancel_extra_event_count": (
                cancel_result.get("post_cancel_extra_event_count") if cancel_result else None
            ),
        },
        "acceptance": {
            "same_run_id_resume": _pass_fail(bool(checkpoint_result.get("run_id")) and checkpoint_ok),
            "checkpoint_list_visible": _pass_fail(int(checkpoint_result.get("checkpoint_count") or 0) >= 1),
            "resume_does_not_restart": _pass_fail(
                checkpoint_result.get("resume_did_not_rerun_prior_nodes") is True
            ),
            "runtime_cancel_terminal": (
                _pass_fail(bool(cancel_ok)) if include_cancel else "skipped"
            ),
            "no_events_after_cancel": (
                _pass_fail(no_events_after_cancel) if include_cancel else "skipped"
            ),
            "cancel_then_resume_after_cancelled": (
                _pass_fail(cancel_then_resume_after_cancel) if include_cancel else "skipped"
            ),
            "resume_after_cancel_does_not_restart": (
                _pass_fail(
                    cancel_then_resume_result is not None
                    and cancel_then_resume_result.get("resume_after_cancel_did_not_rerun_prior_nodes")
                    is True
                )
                if include_cancel
                else "skipped"
            ),
        },
        "notes": [
            "DSN is intentionally omitted from this report.",
            "Provider/tool deep cancellation support must be reported as accepted or unsupported per runner/tool.",
        ],
    }


def _case_passed(report: dict[str, Any], case_name: str) -> bool:
    cases = report.get("cases") if isinstance(report.get("cases"), dict) else {}
    case = cases.get(case_name) if isinstance(cases, dict) else None
    return isinstance(case, dict) and case.get("status") == "pass"


def _first_nonpassing_case(reports: list[dict[str, Any]], case_name: str) -> dict[str, Any]:
    for report in reports:
        cases = report.get("cases") if isinstance(report.get("cases"), dict) else {}
        case = cases.get(case_name) if isinstance(cases, dict) else None
        if isinstance(case, dict) and case.get("status") != "pass":
            return dict(case)
    cases = reports[-1].get("cases") if reports else {}
    case = cases.get(case_name) if isinstance(cases, dict) else {}
    return dict(case) if isinstance(case, dict) else {}


def _max_metric(reports: list[dict[str, Any]], metric_name: str) -> int | None:
    values: list[int] = []
    for report in reports:
        metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
        value = metrics.get(metric_name) if isinstance(metrics, dict) else None
        if value is not None:
            values.append(int(value or 0))
    return max(values) if values else None


async def build_pilot_report(
    *,
    dsn: str,
    keep_session: bool,
    include_cancel: bool,
    iterations: int = 1,
) -> dict[str, Any]:
    total_iterations = max(1, int(iterations or 1))
    if total_iterations == 1:
        return await _build_single_pilot_report(
            dsn=dsn,
            keep_session=keep_session,
            include_cancel=include_cancel,
        )

    generated_at = datetime.now(UTC).isoformat()
    iteration_reports: list[dict[str, Any]] = []
    for index in range(1, total_iterations + 1):
        report = await _build_single_pilot_report(
            dsn=dsn,
            keep_session=keep_session,
            include_cancel=include_cancel,
        )
        report["iteration"] = index
        iteration_reports.append(report)

    checkpoint_passed = sum(
        1 for report in iteration_reports if _case_passed(report, "checkpoint_resume")
    )
    cancel_passed = (
        sum(1 for report in iteration_reports if _case_passed(report, "runtime_cancel"))
        if include_cancel
        else None
    )
    cancel_then_resume_passed = (
        sum(1 for report in iteration_reports if _case_passed(report, "cancel_then_resume"))
        if include_cancel
        else None
    )
    checkpoint_ok = checkpoint_passed == total_iterations
    cancel_ok = (cancel_passed == total_iterations) if include_cancel else True
    cancel_then_resume_ok = (
        cancel_then_resume_passed == total_iterations
        if include_cancel
        else True
    )

    checkpoint_case = _first_nonpassing_case(iteration_reports, "checkpoint_resume")
    checkpoint_case["status"] = _pass_fail(checkpoint_ok)
    cases: dict[str, Any] = {"checkpoint_resume": checkpoint_case}
    if include_cancel:
        cancel_case = _first_nonpassing_case(iteration_reports, "runtime_cancel")
        cancel_case["status"] = _pass_fail(cancel_ok)
        cases["runtime_cancel"] = cancel_case
    else:
        cases["runtime_cancel"] = {
            "status": "skipped",
            "reason": "cancel validation disabled by --skip-cancel",
        }
        cases["cancel_then_resume"] = {
            "status": "skipped",
            "reason": "cancel validation disabled by --skip-cancel",
        }
    if include_cancel:
        cancel_then_resume_case = _first_nonpassing_case(
            iteration_reports,
            "cancel_then_resume",
        )
        cancel_then_resume_case["status"] = _pass_fail(cancel_then_resume_ok)
        cases["cancel_then_resume"] = cancel_then_resume_case

    return {
        "report_type": "long_task_pilot_validation",
        "generated_at": generated_at,
        "overall_status": _pass_fail(checkpoint_ok and cancel_ok and cancel_then_resume_ok),
        "cases": cases,
        "iterations": iteration_reports,
        "metrics": {
            "total_iterations": total_iterations,
            "checkpoint_resume_passed": checkpoint_passed,
            "checkpoint_resume_failed": total_iterations - checkpoint_passed,
            "runtime_cancel_passed": cancel_passed if include_cancel else None,
            "runtime_cancel_failed": (
                total_iterations - int(cancel_passed or 0) if include_cancel else None
            ),
            "cancel_then_resume_passed": (
                cancel_then_resume_passed if include_cancel else None
            ),
            "cancel_then_resume_failed": (
                total_iterations - int(cancel_then_resume_passed or 0)
                if include_cancel
                else None
            ),
            "checkpoint_resume_success_rate": checkpoint_passed / total_iterations,
            "runtime_cancel_success_rate": (
                int(cancel_passed or 0) / total_iterations if include_cancel else None
            ),
            "cancel_then_resume_success_rate": (
                int(cancel_then_resume_passed or 0) / total_iterations
                if include_cancel
                else None
            ),
            "max_post_cancel_extra_event_count": _max_metric(
                iteration_reports,
                "post_cancel_extra_event_count",
            ),
        },
        "acceptance": {
            "same_run_id_resume": _pass_fail(checkpoint_ok),
            "checkpoint_list_visible": _pass_fail(checkpoint_ok),
            "resume_does_not_restart": _pass_fail(checkpoint_ok),
            "runtime_cancel_terminal": _pass_fail(cancel_ok) if include_cancel else "skipped",
            "no_events_after_cancel": _pass_fail(cancel_ok) if include_cancel else "skipped",
            "cancel_then_resume_after_cancelled": (
                _pass_fail(cancel_then_resume_ok) if include_cancel else "skipped"
            ),
            "resume_after_cancel_does_not_restart": (
                _pass_fail(cancel_then_resume_ok) if include_cancel else "skipped"
            ),
        },
        "notes": [
            "DSN is intentionally omitted from this report.",
            "Provider/tool deep cancellation support must be reported as accepted or unsupported per runner/tool.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dsn",
        default=os.environ.get("KSADK_SESSION_DSN", ""),
        help="PostgreSQL DSN. Defaults to KSADK_SESSION_DSN. The value is not printed.",
    )
    parser.add_argument(
        "--keep-session",
        action="store_true",
        help="Keep generated session rows for debugging.",
    )
    parser.add_argument(
        "--skip-cancel",
        action="store_true",
        help="Skip W2.5 runtime cancel validation.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Number of independent validation iterations to run. Use 100 for W3 95%% acceptance.",
    )
    args = parser.parse_args()
    dsn = args.dsn.strip()
    if not dsn:
        raise SystemExit("--dsn or KSADK_SESSION_DSN is required")

    report = asyncio.run(
        build_pilot_report(
            dsn=dsn,
            keep_session=args.keep_session,
            include_cancel=not args.skip_cancel,
            iterations=args.iterations,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["overall_status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
