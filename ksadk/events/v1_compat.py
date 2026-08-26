"""Read-only RuntimeEvent v1 wire compatibility.

This module is the only owner of the legacy v1 envelope, parser, and the
canonical-v2-to-v1 projection.  It is deliberately not a persistence or
source-adapter boundary.

Implementation lives in the :mod:`ksadk.events._v1_compat` subpackage
(models / parser / projection); this module remains the stable import path.
"""

from __future__ import annotations

from ksadk.events._v1_compat.models import (
    ALL_V1_EVENT_TYPES,
    V1_EVENT_PAYLOAD_REQUIRED_KEYS,
    A2ATaskProjectionRef,
    A2UIInteractionProjectionRef,
    A2UISurfaceProjectionRef,
    EventTypeV1,
    RuntimeEventV1,
    RuntimeEventV1ProjectionContext,
    RuntimeEventV1ProjectionMode,
    V1ProjectionContextRequiredError,
)
from ksadk.events._v1_compat.parser import RuntimeEventV1Parser
from ksadk.events._v1_compat.projection import project_to_v1

__all__ = [
    "ALL_V1_EVENT_TYPES",
    "A2ATaskProjectionRef",
    "A2UIInteractionProjectionRef",
    "A2UISurfaceProjectionRef",
    "EventTypeV1",
    "RuntimeEventV1",
    "RuntimeEventV1Parser",
    "RuntimeEventV1ProjectionContext",
    "RuntimeEventV1ProjectionMode",
    "V1ProjectionContextRequiredError",
    "V1_EVENT_PAYLOAD_REQUIRED_KEYS",
    "project_to_v1",
]
