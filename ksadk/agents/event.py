"""编排事件模型。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """编排层事件类型。"""

    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    TEXT_OUTPUT = "text_output"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STATE_CHANGE = "state_change"
    ESCALATE = "escalate"
    ERROR = "error"


@dataclass(slots=True)
class AgentEvent:
    """编排事件。"""

    agent_name: str
    event_type: EventType
    data: Any = None
    branch: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    state_delta: dict[str, Any] = field(default_factory=dict)
    escalate: bool = False
