from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_session_failopen_e2e.py"
SPEC = importlib.util.spec_from_file_location("validate_session_failopen_e2e", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.asyncio
async def test_failopen_framework_matrix_runs_without_postgres():
    report = await MODULE.run_validation(
        dsn="",
        unavailable_dsn="postgresql://ksadk@127.0.0.1:1/ksadk_failopen",
    )

    assert report["overall_status"] == "pass"
    assert [item["framework"] for item in report["frameworks"]] == [
        "langgraph",
        "langchain",
        "adk",
    ]
    assert all(item["status"] == "pass" for item in report["frameworks"])
    assert all(item["degraded"] is True for item in report["frameworks"])
    assert report["postgres_readability"] == {"status": "skipped"}
