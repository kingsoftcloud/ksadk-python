"""AG-UI Agent duck seam backed exclusively by a KSADK RuntimeAdapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ag_ui.core import (
    Interrupt,
)

from ksadk.runtime.adapter import (
    RunHandle,
    RuntimeLaunchContext,
)
from ksadk.runtime.executor import RuntimeExecutor

EventStoreFactory = Callable[[], Any]
SessionServiceFactory = Callable[[], Any]


@dataclass
class _PendingInterrupt:
    interrupt: Interrupt
    checkpoint_id: str
    kind: str = "approval"


@dataclass
class _ThreadRun:
    handle: RunHandle
    active: bool = False
    interrupted: bool = False
    cancel_failed: bool = False
    pending: dict[str, _PendingInterrupt] = field(default_factory=dict)
    native_submissions: dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False


@dataclass
class _SharedRuntime:
    executor: RuntimeExecutor
    launch_context: RuntimeLaunchContext
    event_store_factory: Optional[EventStoreFactory]
    session_service_factory: Optional[SessionServiceFactory]
    threads: dict[str, _ThreadRun] = field(default_factory=dict)
    consumed_resumes: dict[tuple[str, str], Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    local_kernel_provider: Any = None


@dataclass
class _WireState:
    thread_id: str
    run_id: str
    message_id: str
    reasoning_id: str
    text_open: bool = False
    text_started: bool = False
    reasoning_open: bool = False
    terminal: bool = False
    text_content: str = ""
    text_items: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    reasoning_content: str = ""
    tool_calls_started: set[str] = field(default_factory=set)
    tool_calls_completed: set[str] = field(default_factory=set)
