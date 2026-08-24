# Agent Kernel v1 合同与稳定错误码的公开入口。
from ksadk.kernel.authorization import (
    AgentControlPermitVerifier,
    JwksSource,
    PermitExpiredError,
    VerifiedAdmission,
)
from ksadk.kernel.contract_fingerprints import (
    AGENT_KERNEL_V1_AGGREGATE_DIGEST,
    AGENT_KERNEL_V1_CONTRACT_SET,
    runtime_capability_matrix_digest,
    runtime_capability_matrix_wire_value,
)
from ksadk.kernel.contracts import (
    ActivationLease,
    ActivationWriteGuard,
    AdmissionWriteGuard,
    AgentControlCommand,
    AgentControlPermit,
    AgentControlReceipt,
    AgentStatusQuery,
    AgentStatusSnapshot,
    ControlError,
    ControlSource,
    EnqueuePayload,
    InjectPayload,
    InterruptPayload,
    JsonValue,
    PausePayload,
    ResumePayload,
    ResumeTarget,
    RuntimeCapability,
    RuntimeCapabilityMatrix,
    SessionEventEnvelope,
    SessionEventSubscription,
    SessionEventWriteGuard,
    SteerPayload,
    SubmitInteractionPayload,
    WireModel,
    WriteContext,
)
from ksadk.kernel.control import AgentKernel, default_capability_matrix
from ksadk.kernel.errors import (
    ERROR_CODES,
    AgentKernelError,
    ContractMismatchError,
    InvalidCommandError,
    InvalidPermitError,
    PersistenceUncertainError,
    QueueFullError,
    StaleFenceError,
    UnsupportedError,
)

# worker 依赖 ksadk.runtime.adapter；runtime.adapter 又经 ksadk.events 回指本包的
# contracts，急切导入会成环，故用 PEP 562 惰性导出。

_LAZY_EXPORTS = {"AgentKernelWorker": "ksadk.kernel.worker", "WorkResult": "ksadk.kernel.worker"}


def __getattr__(name: str):  # noqa: ANN001
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)

__all__ = [
    "AgentControlPermitVerifier",
    "AgentKernel",
    "AgentKernelWorker",
    "JwksSource",
    "PermitExpiredError",
    "VerifiedAdmission",
    "WorkResult",
    "default_capability_matrix",
    "AGENT_KERNEL_V1_AGGREGATE_DIGEST",
    "AGENT_KERNEL_V1_CONTRACT_SET",
    "runtime_capability_matrix_digest",
    "runtime_capability_matrix_wire_value",
    "ERROR_CODES",
    "AgentKernelError",
    "ContractMismatchError",
    "InvalidCommandError",
    "InvalidPermitError",
    "PersistenceUncertainError",
    "QueueFullError",
    "StaleFenceError",
    "UnsupportedError",
    "ActivationLease",
    "ActivationWriteGuard",
    "AdmissionWriteGuard",
    "AgentControlCommand",
    "AgentControlPermit",
    "AgentControlReceipt",
    "AgentStatusQuery",
    "AgentStatusSnapshot",
    "ControlError",
    "ControlSource",
    "EnqueuePayload",
    "InjectPayload",
    "InterruptPayload",
    "JsonValue",
    "PausePayload",
    "ResumePayload",
    "ResumeTarget",
    "RuntimeCapability",
    "RuntimeCapabilityMatrix",
    "SessionEventEnvelope",
    "SessionEventSubscription",
    "SessionEventWriteGuard",
    "SteerPayload",
    "SubmitInteractionPayload",
    "WireModel",
    "WriteContext",
]
