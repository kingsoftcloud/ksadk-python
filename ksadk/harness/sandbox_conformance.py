"""Sandbox Backend Conformance。

这套校验针对 Harness 的异步 Sandbox 合同，而不是某个厂商 SDK。它验证
后端声明与实际行为是否一致，并把无法验证或未声明的能力明确记为 skip，
不把本地进程包装器包装成远程隔离环境。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ksadk.harness.sandbox_backend import (
    ExecuteRequest,
    FilesystemIsolation,
    NetworkControl,
    SandboxBackend,
    SandboxClosedError,
    SandboxPolicyViolation,
    SandboxSpec,
)


@dataclass(frozen=True)
class SandboxConformanceCase:
    """后端无关的行为探针。

    命令由调用方提供，是因为只读命令面、POSIX Shell、容器命令面不一定
    相同。Conformance 验证语义，不强迫所有后端支持同一种 Shell。
    """

    smoke_command: str
    expected_output: str = ""
    timeout_command: str | None = None
    artifact_command: str | None = None
    expected_artifact: str | None = None


@dataclass(frozen=True)
class SandboxConformanceFinding:
    rule: str
    status: str
    detail: str = ""


@dataclass
class SandboxConformanceReport:
    backend_id: str
    findings: list[SandboxConformanceFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(item.status == "failed" for item in self.findings)

    def pass_rule(self, rule: str, detail: str = "") -> None:
        self.findings.append(SandboxConformanceFinding(rule, "passed", detail))

    def fail_rule(self, rule: str, detail: str) -> None:
        self.findings.append(SandboxConformanceFinding(rule, "failed", detail))

    def skip_rule(self, rule: str, detail: str) -> None:
        self.findings.append(SandboxConformanceFinding(rule, "skipped", detail))


async def run_sandbox_backend_conformance(
    backend: SandboxBackend,
    *,
    spec: SandboxSpec,
    case: SandboxConformanceCase,
) -> SandboxConformanceReport:
    """运行一组无网络、可重复的 Sandbox 行为校验。"""

    capabilities = backend.capabilities
    report = SandboxConformanceReport(backend_id=capabilities.backend_id)
    _verify_declaration(report, capabilities)

    handle = await backend.create(spec)
    try:
        result = await backend.execute(
            handle,
            ExecuteRequest(command=case.smoke_command, run_id="sandbox-conformance-smoke"),
        )
        if result.ok and case.expected_output in result.output:
            report.pass_rule("execute.result_contract")
        else:
            report.fail_rule(
                "execute.result_contract",
                f"ok={result.ok}, exit_code={result.exit_code}, error={result.error!r}",
            )

        await _verify_timeout(backend, handle, case, report)
        await _verify_artifacts(backend, handle, case, report)
    finally:
        await backend.close(handle)

    if not handle.closed:
        report.fail_rule("lifecycle.close_marks_handle", "close 后 handle.closed 仍为 false")
    else:
        report.pass_rule("lifecycle.close_marks_handle")

    if capabilities.deterministic_cleanup:
        try:
            await backend.execute(handle, ExecuteRequest(command=case.smoke_command))
        except SandboxClosedError:
            report.pass_rule("lifecycle.closed_handle_rejected")
        except Exception as exc:  # noqa: BLE001 - 报告非标准失败语义
            report.fail_rule(
                "lifecycle.closed_handle_rejected",
                f"关闭后返回了非标准异常 {type(exc).__name__}: {exc}",
            )
        else:
            report.fail_rule("lifecycle.closed_handle_rejected", "关闭后仍可执行命令")
    else:
        report.skip_rule("lifecycle.closed_handle_rejected", "后端未声明确定性清理")

    await _verify_network_admission(backend, spec, report)
    return report


def _verify_declaration(report: SandboxConformanceReport, capabilities) -> None:
    if not capabilities.backend_id.strip():
        report.fail_rule("capabilities.backend_id", "backend_id 不能为空")
    else:
        report.pass_rule("capabilities.backend_id")

    if (
        capabilities.filesystem_isolation is FilesystemIsolation.NONE
        and capabilities.artifact_collection
    ):
        report.fail_rule(
            "capabilities.artifact_boundary",
            "没有文件系统边界时不能声明受控 Artifact 收集",
        )
    else:
        report.pass_rule("capabilities.artifact_boundary")

    if capabilities.reconnect and not capabilities.deterministic_cleanup:
        report.fail_rule(
            "capabilities.reconnect_lifecycle",
            "支持 reconnect 的后端必须定义确定性清理语义",
        )
    else:
        report.pass_rule("capabilities.reconnect_lifecycle")

    if capabilities.ownership_fencing and not capabilities.reconnect:
        report.fail_rule(
            "capabilities.ownership_fencing",
            "ownership_fencing 依赖 reconnect 能力",
        )
    else:
        report.pass_rule("capabilities.ownership_fencing")

    if capabilities.command_reconnect and not (
        capabilities.reconnect
        and capabilities.ownership_fencing
        and capabilities.cooperative_cancellation
    ):
        report.fail_rule(
            "capabilities.command_reconnect",
            "command_reconnect 依赖 reconnect、ownership_fencing 和 cooperative_cancellation",
        )
    else:
        report.pass_rule("capabilities.command_reconnect")


async def _verify_timeout(backend, handle, case, report) -> None:
    capabilities = backend.capabilities
    if not capabilities.request_timeout:
        report.skip_rule("execute.request_timeout", "后端未声明按请求超时")
        return
    if not case.timeout_command:
        report.fail_rule("execute.request_timeout", "声明支持超时但未提供超时探针")
        return
    result = await backend.execute(
        handle,
        ExecuteRequest(
            command=case.timeout_command,
            timeout_seconds=0.05,
            run_id="sandbox-conformance-timeout",
        ),
    )
    if not result.ok and result.exit_code != 0 and "超时" in result.error:
        report.pass_rule("execute.request_timeout")
    else:
        report.fail_rule(
            "execute.request_timeout",
            f"超时未形成明确失败: ok={result.ok}, code={result.exit_code}, error={result.error!r}",
        )


async def _verify_artifacts(backend, handle, case, report) -> None:
    capabilities = backend.capabilities
    if not capabilities.artifact_collection:
        report.skip_rule("artifact.collection", "后端未声明 Artifact 收集")
        return
    if not case.artifact_command or not case.expected_artifact:
        report.fail_rule("artifact.collection", "声明支持 Artifact 但未提供 Artifact 探针")
        return
    result = await backend.execute(
        handle,
        ExecuteRequest(
            command=case.artifact_command,
            run_id="sandbox-conformance-artifact",
        ),
    )
    artifacts = await backend.collect_artifacts(handle)
    if result.ok and case.expected_artifact in artifacts:
        report.pass_rule("artifact.collection")
    else:
        report.fail_rule(
            "artifact.collection",
            f"command_ok={result.ok}, artifacts={artifacts!r}",
        )


async def _verify_network_admission(backend, spec, report) -> None:
    capabilities = backend.capabilities
    if capabilities.network_control not in {
        NetworkControl.COMMAND_SURFACE,
        NetworkControl.ADMISSION_ONLY,
        NetworkControl.ENFORCED,
    }:
        report.skip_rule("policy.network_admission", "后端未声明网络控制")
        return
    probe_spec = SandboxSpec(
        workspace_root=spec.workspace_root,
        read_only=spec.read_only,
        network_egress=("sandbox-conformance.invalid",),
        env=dict(spec.env),
    )
    try:
        handle = await backend.create(probe_spec)
    except SandboxPolicyViolation:
        report.pass_rule("policy.network_admission")
        return
    except Exception as exc:  # noqa: BLE001 - 报告非标准失败语义
        report.fail_rule(
            "policy.network_admission",
            f"网络策略返回非标准异常 {type(exc).__name__}: {exc}",
        )
        return
    await backend.close(handle)
    report.fail_rule("policy.network_admission", "声明网络控制但接受了未授权出网 spec")


async def verify_cooperative_cancellation(
    backend: SandboxBackend,
    *,
    spec: SandboxSpec,
    command: str,
) -> SandboxConformanceReport:
    """单独验证取消窗口，避免主套件依赖长运行命令。"""

    report = SandboxConformanceReport(backend_id=backend.capabilities.backend_id)
    if not backend.capabilities.cooperative_cancellation:
        report.skip_rule("execute.cooperative_cancellation", "后端未声明协作取消")
        return report
    handle = await backend.create(spec)
    try:
        task = asyncio.create_task(
            backend.execute(
                handle,
                ExecuteRequest(command=command, run_id="sandbox-conformance-cancel"),
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            report.pass_rule("execute.cooperative_cancellation")
        else:
            report.fail_rule(
                "execute.cooperative_cancellation",
                "取消后任务没有抛出 CancelledError",
            )
    finally:
        await backend.close(handle)
    return report


__all__ = [
    "SandboxConformanceCase",
    "SandboxConformanceFinding",
    "SandboxConformanceReport",
    "run_sandbox_backend_conformance",
    "verify_cooperative_cancellation",
]
