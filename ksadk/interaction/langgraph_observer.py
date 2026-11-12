"""Observe addressed native task results without patching LangGraph internals."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from langgraph.callbacks import GraphCallbackHandler
from langgraph.errors import GraphInterrupt, is_invalid_resume

from ksadk.interaction.langgraph_interrupt import NormalizedInterrupt, normalize_interrupt
from ksadk.interaction.native_events import NativeSubmissionOutcome


@dataclass(frozen=True)
class NativeWait:
    interrupt: NormalizedInterrupt
    task_id: str
    task_namespace: str


def collect_native_waits(snapshot: Any, *, depth: int = 0) -> list[NativeWait]:
    """Prefer actual child tasks to the parent's bubbled interrupt copy."""
    if snapshot is None or depth > 4:
        return []
    config = getattr(snapshot, "config", {}) or {}
    namespace = str((config.get("configurable") or {}).get("checkpoint_ns") or "")
    waits: list[NativeWait] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        nested = getattr(task, "state", None)
        children = collect_native_waits(nested, depth=depth + 1) if hasattr(nested, "tasks") else []
        waits.extend(children)
        bubbled = {wait.interrupt.native_id for wait in children}
        for interrupt in getattr(task, "interrupts", ()) or ():
            normalized = normalize_interrupt(interrupt)
            if normalized.native_id in bubbled:
                continue
            if not normalized.native_id:
                raise ValueError("A native interrupt must have an addressable id")
            task_id = str(task.id)
            task_name = str(task.name)
            task_ns = (
                f"{namespace}|{task_name}:{task_id}" if namespace else f"{task_name}:{task_id}"
            )
            waits.append(NativeWait(normalized, task_id, task_ns))
    return waits


class NativeResumeObserver(GraphCallbackHandler):
    """Only completion/re-interruption of the addressed task proves consumption.

    Generic on_resume fires before validation. Inner runnable/model completion
    is also insufficient: it may execute before the node reaches interrupt().
    Track the outer task callback, and publish observations only after the
    caller has drained the graph and read its persisted snapshot.
    """

    def __init__(self, targets: dict[str, dict[str, Any]], attempt_key: str) -> None:
        self.targets = targets
        self.attempt_key = attempt_key
        self._lock = threading.Lock()
        self._namespaces: dict[str, str] = {}
        self._tasks: dict[str, tuple[str, ...]] = {}
        self._results: dict[str, str] = {}

    def on_chain_start(
        self, _serialized, _inputs, *, run_id, parent_run_id=None, metadata=None, **_kwargs
    ):
        namespace = str((metadata or {}).get("langgraph_checkpoint_ns") or "")
        with self._lock:
            parent_namespace = self._namespaces.get(str(parent_run_id))
            self._namespaces[str(run_id)] = namespace
            if not namespace or parent_namespace == namespace:
                return
            matches = tuple(
                public_id
                for public_id, target in self.targets.items()
                if target.get("task_id") and namespace.endswith(":" + str(target["task_id"]))
            )
            if matches:
                self._tasks[str(run_id)] = matches

    def on_chain_end(self, _outputs, *, run_id, **_kwargs):
        self._record(run_id, "consumed")

    def on_chain_error(self, error, *, run_id, **_kwargs):
        if is_invalid_resume(error):
            status = "rejected"
        elif isinstance(error, GraphInterrupt):
            # The correct answer was addressed to this pending task. Reaching
            # its next native interrupt means that answer passed validation.
            status = "consumed"
        else:
            # A business exception could precede or follow interrupt. Do not
            # guess or release a response whose consumption is uncertain.
            status = "unknown"
        self._record(run_id, status)

    def _record(self, run_id, status):
        with self._lock:
            for public_id in self._tasks.get(str(run_id), ()):
                self._results[public_id] = status

    def outcomes(self) -> tuple[NativeSubmissionOutcome, ...]:
        with self._lock:
            return tuple(
                NativeSubmissionOutcome(
                    public_id, self.attempt_key, self._results.get(public_id, "unknown")
                )
                for public_id in self.targets
            )


def with_resume_observer(config: dict, targets: dict, attempt_key: str):
    """Return a new callback config; preserve user callbacks and their manager."""
    from langchain_core.callbacks.manager import AsyncCallbackManager

    observer = NativeResumeObserver(targets, attempt_key)
    manager = AsyncCallbackManager.configure(inheritable_callbacks=config.get("callbacks"))
    manager.add_handler(observer, inherit=True)
    return {**config, "callbacks": manager}, observer


__all__ = ["NativeResumeObserver", "NativeWait", "collect_native_waits", "with_resume_observer"]
