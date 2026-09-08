"""App-owned persistence and checkpoint capability coordination."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ksadk.sessions.persistence import gate_runtime_capabilities

logger = logging.getLogger(__name__)

StatusProvider = Callable[..., Awaitable[Mapping[str, Any]]]
SessionServiceProvider = Callable[[], Any]


@dataclass(frozen=True)
class PersistenceCapabilitySnapshot:
    """One internally consistent generation of persistence capability state."""

    state: str
    generation: int
    session_persistence: Mapping[str, Any]
    checkpoint_persistence: Mapping[str, Any]
    native_runtime_capabilities: Mapping[str, Any]
    runtime_capabilities: Mapping[str, Any]
    refreshed_at: float
    next_refresh_at: float

    @property
    def resume_supported(self) -> bool:
        resume = self.runtime_capabilities.get("ResumeRun") or {}
        return bool(isinstance(resume, Mapping) and resume.get("Supported") is True)

    @property
    def persistence_gate(self) -> Mapping[str, Any]:
        checkpoint = self.runtime_capabilities.get("Checkpoint") or {}
        if not isinstance(checkpoint, Mapping):
            return {}
        gate = checkpoint.get("PersistenceGate") or {}
        return gate if isinstance(gate, Mapping) else {}


class PersistenceCapabilityCoordinator:
    """Single-flight, bidirectional refresh for one runtime app."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        refresh_interval: float | None = None,
        initial_retry_delay: float = 5.0,
        background: bool = True,
    ) -> None:
        self._clock = clock
        self._refresh_interval = max(
            0.1,
            float(
                refresh_interval
                if refresh_interval is not None
                else os.getenv("KSADK_PERSISTENCE_PROBE_CACHE_TTL") or "30"
            ),
        )
        self._initial_retry_delay = max(0.1, float(initial_retry_delay))
        self._background_enabled = background
        self._snapshot: PersistenceCapabilitySnapshot | None = None
        self._runner: Any | None = None
        self._framework = ""
        self._status_provider: StatusProvider | None = None
        self._session_service_provider: SessionServiceProvider | None = None
        self._refresh_task: asyncio.Task[PersistenceCapabilitySnapshot] | None = None
        self._background_task: asyncio.Task[None] | None = None
        self._consecutive_failures = 0

    @property
    def snapshot(self) -> PersistenceCapabilitySnapshot | None:
        return self._snapshot

    def start(
        self,
        *,
        runner: Any | None,
        framework: str,
        status_provider: StatusProvider,
        session_service_provider: SessionServiceProvider | None = None,
    ) -> None:
        """Bind the app and eagerly begin its first capability refresh."""
        self._bind(
            runner=runner,
            framework=framework,
            status_provider=status_provider,
            session_service_provider=session_service_provider,
        )
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh())
        self._ensure_background()

    def invalidate(self, runner: Any | None = None) -> None:
        """Discard state when the app swaps its active runner."""
        for task in (self._background_task, self._refresh_task):
            if task is not None and not task.done():
                task.cancel()
        self._background_task = None
        self._refresh_task = None
        self._snapshot = None
        self._consecutive_failures = 0
        if runner is not None:
            self._runner = runner

    async def get_snapshot(
        self,
        *,
        runner: Any | None,
        framework: str,
        status_provider: StatusProvider,
        session_service_provider: SessionServiceProvider | None = None,
        force: bool = False,
        wait_timeout: float | None = None,
    ) -> PersistenceCapabilitySnapshot:
        self._bind(
            runner=runner,
            framework=framework,
            status_provider=status_provider,
            session_service_provider=session_service_provider,
        )
        now = self._clock()
        if not force and self._snapshot is not None and now < self._snapshot.next_refresh_at:
            self._ensure_background()
            return self._snapshot

        task = self._refresh_task
        if task is None or task.done():
            task = asyncio.create_task(self._refresh())
            self._refresh_task = task
        self._ensure_background()

        try:
            if wait_timeout is None:
                return await asyncio.shield(task)
            return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, wait_timeout))
        except asyncio.TimeoutError:
            if self._snapshot is not None and not self._snapshot.resume_supported:
                return self._snapshot
            return self._checking_snapshot(runner)

    def _bind(
        self,
        *,
        runner: Any | None,
        framework: str,
        status_provider: StatusProvider,
        session_service_provider: SessionServiceProvider | None,
    ) -> None:
        if self._runner is not None and self._runner is not runner:
            self.invalidate(runner)
        self._runner = runner
        self._framework = str(framework or "").strip().lower()
        self._status_provider = status_provider
        self._session_service_provider = session_service_provider

    async def _refresh(self) -> PersistenceCapabilitySnapshot:
        runner = self._runner
        provider = self._status_provider
        if provider is None:
            raise RuntimeError("Persistence capability coordinator is not bound")

        started = self._clock()
        refresh_runner = getattr(runner, "refresh_runtime_capabilities", None)
        if not callable(refresh_runner):
            refresh_runner = getattr(runner, "prepare_runtime_capabilities", None)
        runner_refresh_failed = False

        async def prepare_runner() -> None:
            nonlocal runner_refresh_failed
            if callable(refresh_runner):
                try:
                    result = refresh_runner()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    runner_refresh_failed = True

        async def probe() -> Mapping[str, Any]:
            try:
                try:
                    return await provider(framework=self._framework, use_cache=False)
                except TypeError as exc:
                    if "use_cache" not in str(exc):
                        raise
                    return await provider(framework=self._framework)
            except Exception:
                unavailable = self._refresh_failed_status()
                return {"Session": unavailable, "Checkpoint": dict(unavailable)}

        _, persistence_status = await asyncio.gather(prepare_runner(), probe())
        session, checkpoint = self._split_persistence_status(persistence_status)
        service_provider = self._session_service_provider
        service = service_provider() if service_provider is not None else None
        refresh_service = getattr(service, "refresh_persistence_capability", None)
        if getattr(service, "degraded", False) is True and callable(refresh_service):
            try:
                result = refresh_service()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
        if getattr(service, "degraded", False) is True:
            session.update(
                {
                    "Status": "degraded",
                    "Ready": False,
                    "SharedAcrossPods": False,
                    "ReasonCode": "SESSION_STORE_DEGRADED",
                    "Reason": "Session persistence is temporarily degraded",
                }
            )
        native = self._runner_capabilities(runner)
        if runner_refresh_failed:
            native = self._runtime_refresh_failed_capabilities(native)
        effective = self._gate(native, session, checkpoint)
        effective = self._gate_runner_failure(native, effective)
        state = "ready" if self._is_ready(native, session, checkpoint, effective) else "degraded"
        if state == "ready":
            self._consecutive_failures = 0
            delay = self._refresh_interval
        else:
            self._consecutive_failures += 1
            delay = min(
                self._refresh_interval,
                self._initial_retry_delay * (2 ** (self._consecutive_failures - 1)),
            )
        refreshed_at = self._clock()
        generation = (self._snapshot.generation if self._snapshot else 0) + 1
        snapshot = PersistenceCapabilitySnapshot(
            state=state,
            generation=generation,
            session_persistence=deepcopy(session),
            checkpoint_persistence=deepcopy(checkpoint),
            native_runtime_capabilities=deepcopy(native),
            runtime_capabilities=deepcopy(effective),
            refreshed_at=refreshed_at,
            next_refresh_at=refreshed_at + delay,
        )
        previous_state = self._snapshot.state if self._snapshot else "checking"
        self._snapshot = snapshot
        if previous_state != state:
            checkpoint_capability = effective.get("Checkpoint") or {}
            logger.info(
                "KSADK persistence capability state changed: %s -> %s",
                previous_state,
                state,
                extra={
                    "persistence_capability_state": state,
                    "persistence_capability_generation": generation,
                    "persistence_reason_code": str(
                        checkpoint_capability.get("ReasonCode") or "READY"
                    ),
                    "persistence_backend": str(
                        checkpoint_capability.get("Backend") or "none"
                    ),
                    "persistence_refresh_ms": int((refreshed_at - started) * 1000),
                },
            )
        return snapshot

    @staticmethod
    def _split_persistence_status(
        status: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if isinstance(status.get("Session"), Mapping):
            return dict(status["Session"]), dict(status.get("Checkpoint") or {})
        legacy = dict(status)
        return legacy, dict(legacy)

    @staticmethod
    def _runner_capabilities(runner: Any) -> dict[str, Any]:
        describe = getattr(runner, "get_runtime_capabilities", None)
        return dict(describe() if callable(describe) else {})

    @staticmethod
    def _requires_persistence(capabilities: Mapping[str, Any]) -> bool:
        checkpoint = capabilities.get("Checkpoint") or {}
        if not isinstance(checkpoint, Mapping):
            return False
        backend = str(checkpoint.get("Backend") or "").lower()
        return bool(
            checkpoint.get("Durable") is True
            and checkpoint.get("SharedAcrossPods") is True
        ) or "postgres" in backend

    @classmethod
    def _gate(
        cls,
        capabilities: Mapping[str, Any],
        session: Mapping[str, Any],
        checkpoint: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not cls._requires_persistence(capabilities):
            return deepcopy(dict(capabilities))
        return gate_runtime_capabilities(capabilities, session, checkpoint)

    @classmethod
    def _gate_runner_failure(
        cls,
        native: Mapping[str, Any],
        effective: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not cls._requires_persistence(native):
            return deepcopy(dict(effective))
        resume = effective.get("ResumeRun") or {}
        if isinstance(resume, Mapping) and resume.get("Supported") is True:
            return deepcopy(dict(effective))

        gated = deepcopy(dict(effective))
        checkpoint = dict(gated.get("Checkpoint") or {})
        existing_gate = checkpoint.get("PersistenceGate") or {}
        if isinstance(existing_gate, Mapping) and existing_gate:
            return gated
        native_checkpoint = native.get("Checkpoint") or {}
        reason_code = str(
            checkpoint.get("ReasonCode")
            or (resume.get("ReasonCode") if isinstance(resume, Mapping) else "")
            or "RUNTIME_CAPABILITY_UNAVAILABLE"
        )
        reason = str(
            checkpoint.get("Reason")
            or (resume.get("Reason") if isinstance(resume, Mapping) else "")
            or "Runtime checkpoint capability is unavailable"
        )
        native_backend = str(
            checkpoint.get("NativeBackend")
            or (
                native_checkpoint.get("Backend")
                if isinstance(native_checkpoint, Mapping)
                else ""
            )
            or checkpoint.get("Backend")
            or "none"
        )
        checkpoint.update(
            {
                "Supported": False,
                "Backend": "none",
                "NativeBackend": native_backend,
                "Durable": False,
                "SharedAcrossPods": False,
                "ResumeMode": "none",
                "ReasonCode": reason_code,
                "Reason": reason,
                "PersistenceGate": {
                    "BlockedStore": "checkpoint",
                    "Source": "runtime",
                    "ReasonCode": reason_code,
                    "Reason": reason,
                },
            }
        )
        gated["Checkpoint"] = checkpoint
        gated["ResumeRun"] = {
            "Supported": False,
            "ResumeMode": "none",
            "ReasonCode": reason_code,
            "Reason": reason,
        }
        return gated

    @staticmethod
    def _refresh_failed_status() -> dict[str, Any]:
        return {
            "Configured": True,
            "Status": "error",
            "Ready": False,
            "Backend": "postgres",
            "SharedAcrossPods": False,
            "EffectiveFor": "new_runs_only",
            "Source": "none",
            "ReasonCode": "PERSISTENCE_REFRESH_FAILED",
            "Reason": "Persistence capability refresh failed",
        }

    @staticmethod
    def _runtime_refresh_failed_capabilities(
        capabilities: Mapping[str, Any],
    ) -> dict[str, Any]:
        failed = deepcopy(dict(capabilities))
        checkpoint = dict(failed.get("Checkpoint") or {})
        checkpoint.update(
            {
                "Supported": False,
                "Durable": False,
                "ReasonCode": "RUNTIME_CAPABILITY_REFRESH_FAILED",
                "Reason": "Runtime capability refresh failed",
            }
        )
        failed["Checkpoint"] = checkpoint
        failed["ResumeRun"] = {
            "Supported": False,
            "ResumeMode": "none",
            "ReasonCode": "RUNTIME_CAPABILITY_REFRESH_FAILED",
            "Reason": "Runtime capability refresh failed",
        }
        return failed

    @classmethod
    def _is_ready(
        cls,
        native: Mapping[str, Any],
        session: Mapping[str, Any],
        checkpoint: Mapping[str, Any],
        effective: Mapping[str, Any],
    ) -> bool:
        if not cls._requires_persistence(native):
            return True
        resume = effective.get("ResumeRun") or {}
        return bool(
            session.get("Ready") is True
            and checkpoint.get("Ready") is True
            and isinstance(resume, Mapping)
            and resume.get("Supported") is True
        )

    def _checking_snapshot(self, runner: Any) -> PersistenceCapabilitySnapshot:
        now = self._clock()
        unavailable = {
            "Configured": True,
            "Status": "checking",
            "Ready": False,
            "Backend": "postgres",
            "SharedAcrossPods": False,
            "EffectiveFor": "new_runs_only",
            "Source": "none",
            "ReasonCode": "PERSISTENCE_CHECK_TIMEOUT",
            "Reason": "Persistence capability check is still in progress",
        }
        native = self._runner_capabilities(runner)
        effective = gate_runtime_capabilities(native, unavailable, unavailable)
        return PersistenceCapabilitySnapshot(
            state="checking",
            generation=0,
            session_persistence=dict(unavailable),
            checkpoint_persistence=dict(unavailable),
            native_runtime_capabilities=native,
            runtime_capabilities=effective,
            refreshed_at=now,
            next_refresh_at=now,
        )

    def _ensure_background(self) -> None:
        if not self._background_enabled:
            return
        if self._background_task is None or self._background_task.done():
            self._background_task = asyncio.create_task(self._background_loop())

    async def _background_loop(self) -> None:
        while True:
            refresh_task = self._refresh_task
            if refresh_task is not None and not refresh_task.done():
                try:
                    await asyncio.shield(refresh_task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "KSADK persistence capability refresh failed",
                        extra={
                            "persistence_capability_state": "degraded",
                            "persistence_reason_code": "CAPABILITY_REFRESH_FAILED",
                        },
                    )
            snapshot = self._snapshot
            delay = (
                max(0.0, snapshot.next_refresh_at - self._clock())
                if snapshot is not None
                else self._initial_retry_delay
            )
            await asyncio.sleep(delay)
            try:
                await self.get_snapshot(
                    runner=self._runner,
                    framework=self._framework,
                    status_provider=self._status_provider,
                    session_service_provider=self._session_service_provider,
                    force=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "KSADK persistence capability background refresh failed",
                    extra={
                        "persistence_capability_state": "degraded",
                        "persistence_reason_code": "CAPABILITY_REFRESH_FAILED",
                    },
                )

    async def aclose(self) -> None:
        tasks = [
            task
            for task in (self._background_task, self._refresh_task)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_task = None
        self._refresh_task = None


__all__ = ["PersistenceCapabilityCoordinator", "PersistenceCapabilitySnapshot"]
