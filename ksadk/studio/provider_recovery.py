"""Reattach approval-paused, Build-pinned Provider runs without replaying input."""

from __future__ import annotations

import asyncio
import logging

from ksadk.runtime import RunHandle
from ksadk.runtime.executor import RuntimeStartPreparation


async def restore_waiting_provider_run(service, record, resolve_spec) -> bool:
    if not record.runtime_handle or service.plugin_runtime is None:
        return False
    try:
        spec = resolve_spec(record.build_id, model=record.model or None)
        if (spec.plugin_bundle_root is None or spec.agent_id != record.agent_id
                or spec.manifest_sha256 != record.manifest_sha256):
            return False
        adapter = service.plugin_runtime.kernel_adapter_provider(spec)()
        matrix = adapter.capabilities()
        if not (matrix.attach.supported and matrix.durable_restore.supported):
            return False
        handle = RunHandle.model_validate(record.runtime_handle)
        if handle.run_id != record.id or handle.session_id != record.session_id:
            return False
        handle = await service.executor.attach(
            spec.launch_context, handle,
            preparation=RuntimeStartPreparation(context=spec.launch_context, adapter=adapter),
        )
    except Exception:
        logging.getLogger(__name__).warning("Provider run %s could not reattach", record.id)
        return False
    key = (record.agent_id, record.session_id)
    service._active_sessions.add(key)
    task = asyncio.create_task(service._execute_runtime_record(
        spec, record.input, record=record, restored_handle=handle,
        provider_runtime_adapter=True,
    ))
    service._recovery_tasks.add(task)

    def finished(done):
        service._recovery_tasks.discard(done)
        service._active_sessions.discard(key)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(finished)
    await asyncio.sleep(0)
    return not task.done()


async def detach_recovered_runs(service) -> None:
    service._detaching_recoveries = True
    tasks = tuple(service._recovery_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    service._detaching_recoveries = False
