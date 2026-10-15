"""Reattach an opt-in SQLite Native Harness run after a local Studio restart."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone

from ksadk.harness.native_runtime import NativeRuntimeAdapter
from ksadk.harness.stores.native_contract import LeaseBusy
from ksadk.runtime import RunHandle
from ksadk.runtime.executor import RuntimeStartPreparation
from ksadk.studio.contracts import RunStatus


async def _prepare_adapter(service, record, spec, handle):
    if spec.plugin_bundle_root is not None:
        from ksadk.studio.plugin_kernel_adapter import StudioPluginKernelAdapter

        if service.plugin_runtime is None:
            raise ValueError("native_studio_plugin_runtime_unavailable")
        adapter = service.plugin_runtime.kernel_adapter_provider(spec)()
        if not isinstance(adapter, StudioPluginKernelAdapter):
            raise ValueError("native_studio_restore_adapter_changed")
        await adapter.prepare_native_restore(handle, expected_goal=record.input)
        return adapter
    adapter = service.executor.create_adapter(spec.launch_context)
    if not isinstance(adapter, NativeRuntimeAdapter):
        raise ValueError("native runtime adapter changed during recovery")
    matrix = adapter.capabilities()
    if not (matrix.attach.supported and matrix.durable_restore.supported):
        raise ValueError("native durable restore unavailable")
    adapter.admit_studio_build(
        build_id=spec.build_id,
        manifest_sha256=spec.manifest_sha256,
        model=spec.model,
        request_config=spec.request_config,
    )
    snapshot = adapter.snapshot(handle)
    if not snapshot.matches_initial_goal(record.input) or adapter.session_parent_run_id(
        record.session_id
    ) != handle.run_id:
        raise ValueError("native run binding changed during recovery")
    return adapter


def _track_recovery(service, record, coroutine) -> None:
    key = (record.agent_id, record.session_id)
    service._active_sessions.add(key)
    ready = asyncio.Event()
    service._native_recovery_ready[record.id] = ready
    task = asyncio.create_task(coroutine)
    service._recovery_tasks.add(task)

    def finished(done):
        service._recovery_tasks.discard(done)
        service._active_sessions.discard(key)
        if service._native_recovery_ready.get(record.id) is ready:
            service._native_recovery_ready.pop(record.id)
        # Completion can mean failure or host shutdown. Wake pending HTTP
        # submissions to revalidate the durable interaction, not to accept it.
        ready.set()
        if not done.cancelled():
            done.exception()

    task.add_done_callback(finished)


async def _retry_after_lease(service, record, spec, handle):
    """A crashed owner can retain its lease briefly; never mark that run final."""
    try:
        while True:
            await asyncio.sleep(0.1)
            current = service.event_store.get(record.id)
            if current.status not in {RunStatus.RUNNING, RunStatus.WAITING_INPUT, RunStatus.PAUSED}:
                return
            adapter = await _prepare_adapter(service, current, spec, handle)
            try:
                restored = await service.executor.attach(
                    spec.launch_context,
                    handle,
                    preparation=RuntimeStartPreparation(
                        context=spec.launch_context, adapter=adapter
                    ),
                )
            except LeaseBusy:
                continue
            await service._execute_runtime_record(
                spec, current.input, record=current, restored_handle=restored,
                provider_runtime_adapter=spec.plugin_bundle_root is not None,
            )
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.getLogger(__name__).warning(
            "Native Studio run %s failed during lease recovery", record.id
        )
        current = service.event_store.get(record.id)
        if current.status in {RunStatus.RUNNING, RunStatus.WAITING_INPUT, RunStatus.PAUSED}:
            current.status = RunStatus.INTERRUPTED
            current.completed_at = datetime.now(timezone.utc)
            current.error = {
                "code": "LOCAL_STUDIO_RESTARTED",
                "message": "本地 Studio 重启，运行未在本地恢复。",
            }
            service.event_store.save(current)
            service.event_store.append(
                current.id,
                "run.interrupted",
                {"reason": "local_studio_restarted", "recoverable": False},
            )
            service._expire_unresolved_interactions(current)


async def restore_native_studio_run(service, record, resolve_spec) -> bool:
    native_ref = record.runtime_handle.get("native_ref") if record.runtime_handle else None
    if not isinstance(native_ref, dict) or native_ref.get("engine_kind") != "native":
        return False
    if (record.agent_id, record.session_id) in service._active_sessions:
        return True
    try:
        options = record.native_runtime_options
        selections = {"approval_mode": options.tool_approval_mode} if options is not None else {}
        spec = resolve_spec(record.build_id, model=record.model or None, **selections)
        request_config = dict(spec.request_config)
        if options is not None and options.reasoning_effort:
            request_config["effort"] = options.reasoning_effort
        if record.collaboration_mode:
            request_config["collaboration_mode"] = record.collaboration_mode
        if record.goal_objective:
            request_config["goal_objective"] = record.goal_objective
            request_config["ephemeral"] = False
        spec = replace(spec, request_config=request_config)
        if (
            spec.build_id != record.build_id
            or spec.agent_id != record.agent_id
            or spec.manifest_sha256 != record.manifest_sha256
            or spec.launch_context.runtime_type != record.runtime_type
        ):
            return False
        handle = RunHandle.model_validate(record.runtime_handle)
        if handle.session_id != record.session_id:
            return False
        adapter = await _prepare_adapter(service, record, spec, handle)
        try:
            handle = await service.executor.attach(
                spec.launch_context,
                handle,
                preparation=RuntimeStartPreparation(context=spec.launch_context, adapter=adapter),
            )
        except LeaseBusy:
            _track_recovery(service, record, _retry_after_lease(service, record, spec, handle))
            return True
    except Exception:
        logging.getLogger(__name__).warning(
            "Native Studio run %s could not reattach", record.id
        )
        return False

    _track_recovery(
        service,
        record,
        service._execute_runtime_record(
            spec, record.input, record=record, restored_handle=handle,
            provider_runtime_adapter=spec.plugin_bundle_root is not None,
        ),
    )
    await asyncio.sleep(0)
    return True
