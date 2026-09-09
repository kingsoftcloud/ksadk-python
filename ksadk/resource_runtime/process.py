"""Async owner of one resource worker subprocess and its private pipes."""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import time

from ksadk.resource_runtime.ipc import MAX_FRAME_BYTES, ResourceRequest, decode_body, encode_frame
from ksadk.resource_runtime.leases import ResourceScope
from ksadk.resource_runtime.worker import WorkerInitialization


class ResourceWorkerError(RuntimeError):
    def __init__(self):
        super().__init__("RESOURCE_WORKER_UNAVAILABLE")


class ResourceWorkerProcess:
    def __init__(self, process: asyncio.subprocess.Process):
        self._process = process
        self._lock = asyncio.Lock()

    @classmethod
    async def start(cls, initialization: WorkerInitialization) -> ResourceWorkerProcess:
        if os.name != "posix":
            raise RuntimeError("RESOURCE_IPC_UNSUPPORTED")
        # No inherited credential, proxy, Python path or runtime selection vars.
        env = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
            if key in os.environ
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "ksadk.resource_runtime.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        worker = cls(process)
        try:
            response = await asyncio.wait_for(worker._exchange(initialization.pipe_payload()), 30)
            if response != {"status": "ready"}:
                raise ResourceWorkerError()
            return worker
        except BaseException:
            await worker.aclose()
            raise

    async def invoke(self, request: ResourceRequest, scope: ResourceScope) -> dict:
        async with self._lock:
            remaining = request.deadline / 1000 - time.time()
            if remaining <= 0:
                raise ResourceWorkerError()
            try:
                response = await asyncio.wait_for(
                    self._exchange(
                        {
                            "request": request.model_dump(by_alias=True, mode="json"),
                            "scope": scope.model_dump(by_alias=True, mode="json"),
                        }
                    ),
                    remaining,
                )
                if response.get("requestId") != request.request_id or "result" not in response:
                    raise ResourceWorkerError()
                return response["result"]
            except BaseException:
                # A lost response cannot be correlated with a subsequent request.
                # Terminate instead of reusing or silently restarting the worker.
                await self.aclose()
                raise

    async def _exchange(self, payload: dict) -> dict:
        if self._process.returncode is not None:
            raise ResourceWorkerError()
        source, target = self._process.stdout, self._process.stdin
        assert source is not None and target is not None
        target.write(encode_frame(payload))
        await target.drain()
        prefix = await source.readexactly(4)
        (size,) = struct.unpack("!I", prefix)
        if not 0 < size <= MAX_FRAME_BYTES:
            raise ResourceWorkerError()
        return decode_body(await source.readexactly(size))

    async def aclose(self) -> None:
        if self._process.returncode is None:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
        await self._process.wait()

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def is_running(self) -> bool:
        return self._process.returncode is None

    async def wait_stopped(self) -> int:
        return await self._process.wait()
