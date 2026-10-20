"""Admit background notification turns through their owning Native runtime."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ksadk.harness.stores.native_contract import StoreConflict

if TYPE_CHECKING:
    from ksadk.studio.run_service import StudioRunService

logger = logging.getLogger("ksadk.studio.run_service")


class _RunServiceWakeDispatcher:
    """Bind the wakeup coordinator to per-watch NativeRuntimeAdapter admissions.

    Each pending command carries its source Build's ``bundle_digest``. The
    dispatcher looks up the Native activation by ``(bundle_digest, session_id)``
    — the registry ``plugin_native_activation`` already maintains — so a watch
    never admits through a foreign Build's adapter. A different Build or agent
    for the same session cannot read or notify another source's commands. The
    dispatcher only delegates to the adapter's existing
    ``start_notification_turn`` / ``notified_task_ids`` / ``is_run_canceled`` /
    ``task_chain_budget_status``; it does not duplicate admission logic.
    """

    def __init__(self, run_service: StudioRunService) -> None:
        self._run_service = run_service
        self._source_record_ids: dict[str, str] = {}

    def _source_studio_record(self, native_run_id: str):
        """Find the Studio RunRecord whose Native run_id matches the source."""
        cached = self._source_record_ids.get(native_run_id)
        if cached:
            try:
                return self._run_service.event_store.get(cached)
            except KeyError:
                self._source_record_ids.pop(native_run_id, None)
        for record in self._run_service.event_store.list_runs():
            handle = record.runtime_handle or {}
            rid = handle.get("run_id") or (handle.get("native_ref", {}) or {}).get("run_id")
            if rid == native_run_id:
                self._source_record_ids[native_run_id] = record.id
                return record
        return None

    def _adapter_for(self, bundle_digest: str, session_id: str):
        pr = self._run_service.plugin_runtime
        if pr is None:
            return None
        activations = getattr(pr, "_native_activations", None)
        if not isinstance(activations, dict):
            return None
        activation = activations.get((bundle_digest, session_id))
        if activation is None:
            return None
        return getattr(activation, "adapter", None)

    async def _reopen_adapter(
        self, *, build_id: str, session_id: str, model: str, approval_mode: str,
        bundle_digest: str,
    ):
        # Cold-restart path: the in-memory activation cache is empty because no
        # run has started for this session in this process. Reopen the source
        # Build's Native activation by resolving the sealed Build and calling the
        # existing ``kernel_adapter`` entry — the same path a run start uses — so
        # claims, chain budget, CAS/fencing and the sealed Build identity are all
        # preserved (no engine recovery logic is duplicated here). The activation
        # is cached in ``_native_activations`` by ``kernel_adapter`` itself, so a
        # repeat wake reuses it. Returns the NativeRuntimeAdapter, or None if the
        # Build/host cannot be resolved right now (transient — coordinator retries).
        if not build_id:
            return None
        studio = getattr(self._run_service, "_studio", None)
        if studio is None:
            return None
        pr = self._run_service.plugin_runtime
        if pr is None or not hasattr(pr, "kernel_adapter"):
            return None
        try:
            spec = studio.resolve_run_spec(
                build_id, model=(model or None), approval_mode=(approval_mode or None),
            )
        except Exception as exc:
            logger.debug(
                "background_wake.reopen_resolve_failed build=%s err=%s",
                build_id, type(exc).__name__,
            )
            return None
        # Refuse to admit through a Build whose sealed digest differs from the
        # watch's source: a different Build cannot own the source's commands.
        if str(spec.request_config.get("plugin_bundle_digest") or "") != bundle_digest:
            logger.warning(
                "background_wake.reopen_bundle_mismatch build=%s expected=%s",
                build_id, bundle_digest,
            )
            return None
        try:
            adapter = await pr.kernel_adapter(spec, session_id=session_id)
        except Exception as exc:
            logger.debug(
                "background_wake.reopen_kernel_adapter_failed build=%s err=%s",
                build_id, type(exc).__name__,
            )
            return None
        from ksadk.harness.native_runtime import NativeRuntimeAdapter
        if not isinstance(adapter, NativeRuntimeAdapter):
            return None
        # The reopened activation's adapter is admitted (Build/session bound at
        # construction) but not yet compiled — ``start_notification_turn`` requires
        # a compiled engine. Compile it through the standard lazy compile path
        # (idempotent under the adapter's compile lock). This does not duplicate
        # engine recovery logic: it is the same compile a run start performs.
        try:
            await adapter._ensure_compiled()
        except Exception as exc:
            logger.debug(
                "background_wake.reopen_compile_failed build=%s err=%s",
                build_id, type(exc).__name__,
            )
            return None
        return adapter

    async def admit(
        self, *, scope_id: str, source_run_id: str, task_id: str,
        bundle_digest: str, session_id: str,
        build_id: str = "", model: str = "", approval_mode: str = "",
    ) -> str:
        adapter = self._adapter_for(bundle_digest, session_id)
        if adapter is None:
            # Cold restart: the activation cache is empty. Reopen the source
            # Build's activation so the notification can be admitted without a
            # new user run. A failure to reopen is transient (pending).
            adapter = await self._reopen_adapter(
                build_id=build_id, session_id=session_id, model=model,
                approval_mode=approval_mode, bundle_digest=bundle_digest,
            )
            if adapter is None:
                return "pending"
        start_public = getattr(adapter, "start_notification_turn_public", None)
        if start_public is None:
            return "pending"  # adapter is not a NativeRuntimeAdapter — transient, retries
        # Budget check: a confirmed exhausted MODEL or WALL-TIME budget is a
        # permanent discard (no model answer is possible / time elapsed). The
        # TOOL dimension is NOT a discard here: a notification turn delivers a
        # background-command observation at a model boundary — it does not
        # dispatch a tool call — so a chain with max_tool_calls=0 (a text-only
        # allowance) must still admit the notification if it has model budget.
        # Discarding on tool_calls_remaining<=0 would wrongly swallow a pure-text
        # notification for a zero-tool chain. An unknown read error is NOT
        # confirmed exhaustion — it remains pending (retain for retry).
        budget_status = getattr(adapter, "task_chain_budget_status", None)
        if budget_status is not None:
            try:
                status = budget_status(source_run_id)
            except Exception:
                return "pending"  # unknown read error — not confirmed exhaustion
            model_remaining = status.get("model_calls_remaining")
            if model_remaining is not None and model_remaining <= 0:
                return "discarded"  # no model answer possible — permanent
            wall_remaining = status.get("wall_seconds_remaining")
            if wall_remaining is not None and wall_remaining <= 0:
                return "discarded"  # wall budget elapsed — permanent
        try:
            result, handle = start_public(source_run_id=source_run_id, task_id=task_id)
            # Project the notification turn as a public Studio RunRecord so the
            # ordinary runs API and session trajectory see in-progress events and
            # a terminal record. Reuses the ordinary registration/event path.
            run_service = self._run_service
            studio = getattr(run_service, "_studio", None)
            if studio is not None and build_id:
                try:
                    spec = studio.resolve_run_spec(
                        build_id, model=(model or None), approval_mode=(approval_mode or None),
                    )
                    # Resolve the source Studio RunRecord by the native run_id.
                    source_record = self._source_studio_record(source_run_id)
                    if source_record is None:
                        return "pending"
                    await run_service._project_notification_run(
                        adapter=adapter, spec=spec,
                        source_record=source_record,
                        source_run_id=source_run_id, task_id=task_id,
                        handle=handle,
                    )
                except Exception as exc:
                    logger.warning(
                        "background_wake.project_notification_failed task_id=%s err=%s",
                        task_id, type(exc).__name__,
                    )
                    # Native admission is durable, but the public observer is
                    # still missing. Keep the watch so an idempotent reattach
                    # retries projection instead of losing the public run.
                    return "pending"
            # Idempotent creation: created=True (new turn) or created=False (repeat).
            # Both are admitted — the turn exists and the kernel will drive it.
            return "admitted"
        except StoreConflict as exc:
            reason = str(exc)
            # Only permanent conditions that prove the notification cannot ever
            # succeed are discarded. CAS / transient conflicts remain pending.
            # Do NOT match on the substring "failed" — StoreConflict messages
            # like "session head compare-and-swap failed" are transient, not a
            # confirmed source-run failure. Match specific permanent reasons.
            if "already_notified" in reason:
                return "discarded"
            if "canceled" in reason and "compare-and-swap" not in reason:
                return "discarded"
            if "background_command_task_not_found" in reason:
                return "discarded"
            return "pending"
        except Exception:
            return "pending"

    def notified_task_ids(
        self, source_run_id: str, bundle_digest: str, session_id: str,
    ) -> set[str]:
        adapter = self._adapter_for(bundle_digest, session_id)
        query = getattr(adapter, "notified_task_ids", None) if adapter is not None else None
        if query is None:
            return set()
        return query(source_run_id)

    def is_source_canceled(self, source_run_id: str, bundle_digest: str, session_id: str) -> bool:
        adapter = self._adapter_for(bundle_digest, session_id)
        query = getattr(adapter, "is_run_canceled", None) if adapter is not None else None
        if query is None:
            return False
        return query(source_run_id)

    def release_notification_lease(
        self, source_run_id: str, task_id: str, bundle_digest: str, session_id: str,
    ) -> None:
        adapter = self._adapter_for(bundle_digest, session_id)
        release = (
            getattr(adapter, "release_notification_lease", None)
            if adapter is not None else None
        )
        if release is None:
            return
        try:
            release(source_run_id, task_id)
        except Exception:
            pass
