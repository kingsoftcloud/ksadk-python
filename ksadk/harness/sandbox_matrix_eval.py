"""真实 Sandbox 模板就绪矩阵入口（Local Deployment / 发布前验证）。

装配宿主环境可用的真实后端并运行 Conformance：

- ``private-local-subprocess``：进程隔离私有 Sandbox（每次会话一次性
  临时工作区 + 输出上限 + 超时），始终执行——它是本地发布的最低门槛；
- ``kop-pod-process``：KOP Pod 进程后端，需 ``KSADK_ALLOW_POD_PROCESS_TOOLS``
  显式开启，否则诚实报告 ``not_configured``；
- ``e2b``：真实 E2B 模板，需 ``KSADK_SANDBOX_TEMPLATE_ID`` 与
  ``E2B_API_KEY``；缺少配置时报告 ``not_configured`` 而非伪造通过。

用法::

    python -m ksadk.harness.sandbox_matrix_eval --out sandbox-matrix.json

命令是显式网络/进程 E2E，不进默认测试套件；凭证只留在进程内，
报告不含 Secret。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from ksadk.harness.sandbox_backend import SandboxSpec
from ksadk.harness.sandbox_conformance import SandboxConformanceCase
from ksadk.harness.sandbox_matrix import (
    SandboxMatrixCandidate,
    run_sandbox_matrix,
)


def _smoke_command() -> str:
    return f'"{sys.executable}" -c "print(\'SANDBOX_MATRIX_OK\')"'


def _case() -> SandboxConformanceCase:
    return SandboxConformanceCase(
        smoke_command=_smoke_command(),
        expected_output="SANDBOX_MATRIX_OK",
        timeout_command=f'"{sys.executable}" -c "import time; time.sleep(30)"',
        artifact_command=f'"{sys.executable}" -c "open(\'result.txt\',\'w\').write(\'ok\')"',
        expected_artifact="result.txt",
    )


def _private_subprocess_backend():  # type: ignore[no-untyped-def]
    from ksadk.harness.sandbox_backend import SubprocessSandboxBackend

    return SubprocessSandboxBackend()


def _pod_process_backend():  # type: ignore[no-untyped-def]
    from ksadk.sandbox.backends.local_process import LocalProcessSandboxBackend
    from ksadk.sandbox.factory import resolve_local_session_dir

    return LocalProcessSandboxBackend(
        workspace_root=resolve_local_session_dir() / "workspace",
        backend_name="pod_process",
    )


def _e2b_backend():  # type: ignore[no-untyped-def]
    from ksadk.harness.sandbox_adapters import adapt_e2b_backend
    from ksadk.sandbox.backends.e2b import E2BSandboxBackend
    from ksadk.sandbox.factory import sandbox_spec_from_env

    return adapt_e2b_backend(E2BSandboxBackend(spec=sandbox_spec_from_env()))


def build_candidates() -> tuple[SandboxMatrixCandidate, ...]:
    spec = SandboxSpec(workspace_root="", read_only=False)
    case = _case()
    candidates: list[SandboxMatrixCandidate] = [
        SandboxMatrixCandidate(
            backend_id="private-local-subprocess",
            backend=_private_subprocess_backend(),
            spec=spec,
            case=case,
            required=True,
        )
    ]
    if os.getenv("KSADK_ALLOW_POD_PROCESS_TOOLS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        try:
            kop_backend = _pod_process_backend()
        except Exception as exc:  # noqa: BLE001 - 装配失败必须是可审计行
            candidates.append(
                SandboxMatrixCandidate(
                    backend_id="kop-pod-process",
                    backend=None,
                    spec=spec,
                    case=case,
                    unavailable_reason=f"kop pod backend assembly failed: {exc}",
                )
            )
        else:
            from ksadk.harness.sandbox_adapters import adapt_local_process_backend

            candidates.append(
                SandboxMatrixCandidate(
                    backend_id="kop-pod-process",
                    backend=adapt_local_process_backend(kop_backend),
                    spec=spec,
                    case=case,
                )
            )
    else:
        candidates.append(
            SandboxMatrixCandidate(
                backend_id="kop-pod-process",
                backend=None,
                spec=spec,
                case=case,
                unavailable_reason="KSADK_ALLOW_POD_PROCESS_TOOLS not enabled",
            )
        )

    template_id = (
        os.getenv("KSADK_SANDBOX_TEMPLATE_ID")
        or os.getenv("KSADK_SKILL_RUNTIME_TEMPLATE_ID")
        or ""
    ).strip()
    api_key = os.getenv("E2B_API_KEY", "").strip()
    if template_id and api_key:
        try:
            e2b_backend = _e2b_backend()
        except Exception as exc:  # noqa: BLE001 - 装配失败必须是可审计行
            candidates.append(
                SandboxMatrixCandidate(
                    backend_id="e2b",
                    backend=None,
                    spec=spec,
                    case=case,
                    required=bool(os.getenv("KSADK_SANDBOX_MATRIX_REQUIRE_E2B")),
                    unavailable_reason=f"e2b backend assembly failed: {exc}",
                )
            )
        else:
            candidates.append(
                SandboxMatrixCandidate(
                    backend_id="e2b",
                    backend=e2b_backend,
                    spec=spec,
                    case=case,
                    required=bool(os.getenv("KSADK_SANDBOX_MATRIX_REQUIRE_E2B")),
                )
            )
    else:
        missing = []
        if not template_id:
            missing.append("KSADK_SANDBOX_TEMPLATE_ID")
        if not api_key:
            missing.append("E2B_API_KEY")
        candidates.append(
            SandboxMatrixCandidate(
                backend_id="e2b",
                backend=None,
                spec=spec,
                case=case,
                required=bool(os.getenv("KSADK_SANDBOX_MATRIX_REQUIRE_E2B")),
                unavailable_reason=f"missing: {', '.join(missing)}",
            )
        )
    return tuple(candidates)


def run_sandbox_template_matrix() -> dict[str, Any]:
    report = asyncio.run(run_sandbox_matrix(build_candidates()))
    return report.to_dict()


def main() -> None:
    parser = argparse.ArgumentParser(prog="ksadk.harness.sandbox_matrix_eval")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    report = run_sandbox_template_matrix()
    safe = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(safe + "\n")
    print(safe)
    raise SystemExit(0 if report["status"] in {"ready", "warning"} else 1)


if __name__ == "__main__":
    main()


__all__ = ["build_candidates", "run_sandbox_template_matrix"]
