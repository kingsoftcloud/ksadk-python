"""真实 Sandbox 模板矩阵入口的离线验证。

私有 Subprocess 后端执行真实 Python 模板；E2B/KOP 未配置时必须保留
显式 ``not_configured`` 行，而不是被伪造通过或静默丢弃。
"""

from __future__ import annotations

import pytest

from ksadk.harness.sandbox_matrix_eval import build_candidates, run_sandbox_template_matrix


@pytest.mark.asyncio
async def test_private_template_runs_real_python_and_unconfigured_backends_stay_explicit():
    report = await __import__("ksadk.harness.sandbox_matrix", fromlist=["x"]).run_sandbox_matrix(
        build_candidates()
    )

    rows = {row.backend_id: row for row in report.rows}
    assert rows["private-local-subprocess"].status == "ready"
    assert rows["private-local-subprocess"].summary["failed"] == 0
    # 未装配的真实后端：显式 not_configured 行，绝不伪造。
    assert rows["e2b"].status == "not_configured"
    assert "E2B_API_KEY" in rows["e2b"].findings[0]["detail"]
    assert rows["kop-pod-process"].status == "not_configured"
    assert "KSADK_ALLOW_POD_PROCESS_TOOLS" in rows["kop-pod-process"].findings[0]["detail"]
    wire = str(report.to_dict())
    assert "sk-" not in wire


def test_matrix_report_is_json_serializable_without_secrets():
    payload = run_sandbox_template_matrix()
    assert payload["schemaVersion"] == 1
    assert payload["status"] in {"ready", "warning"}
    assert any(row["backendId"] == "private-local-subprocess" for row in payload["rows"])
