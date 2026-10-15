"""Provider-neutral model gateway boundary.

The gateway package is intentionally independent from an agent loop.  Studio,
SDK runtimes and direct Harness execution share the same transport and stream
event vocabulary while retaining their own orchestration policies.
"""

from ksadk.model_gateway.client import ModelGatewayClient
from ksadk.model_gateway.events import (
    ModelStreamEvent,
    ModelStreamEventType,
    classify_gateway_error,
    safe_gateway_error_code,
)
from ksadk.studio.contracts import ModelParameters, NetworkPolicy, ResolvedModel, Usage
from ksadk.studio.model_client import CredentialResolver

__all__ = [
    "ModelGatewayClient",
    "ModelStreamEvent",
    "ModelStreamEventType",
    "classify_gateway_error",
    "safe_gateway_error_code",
    "CredentialResolver",
    "ModelParameters",
    "NetworkPolicy",
    "ResolvedModel",
    "Usage",
]
