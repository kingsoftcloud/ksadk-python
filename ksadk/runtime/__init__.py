"""RuntimeAdapter 平台接口包 (goal-03 冻结签名 + goal-07 框架实现)。"""

from ksadk.runtime.adapter import (
    CONVERSATION_PREPROCESSING_METADATA_KEY,
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    ConversationPreprocessingRequest,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.runtime.framework_adapters import (
    ADKRuntimeAdapter,
    LangGraphRuntimeAdapter,
    build_default_registry,
)
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter

__all__ = [
    "ADKRuntimeAdapter",
    "BaseRuntime",
    "CancelResult",
    "CheckpointCapability",
    "CheckpointDescriptor",
    "ConversationPreprocessingRequest",
    "CONVERSATION_PREPROCESSING_METADATA_KEY",
    "LangGraphRuntimeAdapter",
    "ResumePayload",
    "ResumeTarget",
    "RunHandle",
    "RunnerRuntimeAdapter",
    "RuntimeAdapter",
    "RuntimeRegistry",
    "StartRequest",
    "build_default_registry",
]
