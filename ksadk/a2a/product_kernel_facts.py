"""Route persisted callee lifecycle batches through the shared Task outbox."""

SCHEMA = "a2a-product-kernel-fact/v1"
STATES = frozenset({"running", "waiting", "completed", "failed", "interrupted", "cancelled"})


class ProductKernelFactSink:
    def __init__(self, control_plane):
        self._control = control_plane

    async def append_task_events(self, *, platform_task_id, events):
        if not events or events[0].get("schema") != SCHEMA:
            return await self._control.append_task_events(
                platform_task_id=platform_task_id,
                events=events,
            )
        binding = events[0]["binding"]
        if any(item.get("schema") != SCHEMA or item.get("binding") != binding for item in events):
            raise ValueError("inconsistent persisted Kernel fact batch")
        return await self._control.append_product_kernel_facts(
            platform_task_id=platform_task_id,
            **binding,
            events=[item["fact"] for item in events],
        )


async def enqueue_product_kernel_fact(dispatcher, handle, fact):
    binding = handle.native_ref.get("product_kernel_binding")
    if binding is None:
        return
    if (
        fact.family != "control"
        or fact.event_type != "control.run_transition"
        or fact.payload.get("state") not in STATES
    ):
        return
    await dispatcher.enqueue(
        platform_task_id=binding["platform_task_id"],
        events=[
            {
                "schema": SCHEMA,
                "binding": {
                    name: value for name, value in binding.items() if name != "platform_task_id"
                },
                "fact": fact.model_dump(mode="json"),
            }
        ],
    )


class ProductKernelReconciler:
    """Re-read unreported canonical facts; the Server owns the recovery cursor."""

    def __init__(self, control_plane, session_events, *, interval=5.0, scan_timeout=0.25):
        if interval <= 0 or scan_timeout <= 0:
            raise ValueError("positive reconciliation intervals required")
        self._control, self._events = control_plane, session_events
        self._interval, self._scan_timeout = interval, scan_timeout
        self._scan_cursors = {}
        self._sweep_tasks = set()
        self._cursor = ""
        self._task = None

    async def start(self):
        import asyncio

        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="a2a-product-kernel-reconciler")

    async def stop(self):
        import asyncio

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self):
        import asyncio
        import logging

        while True:
            try:
                await self.reconcile_once()
            except Exception:  # noqa: BLE001 -- durable cursor remains on Server for retry
                logging.getLogger(__name__).warning("A2A Kernel fact reconciliation deferred")
            await asyncio.sleep(self._interval)

    async def reconcile_once(self):
        import asyncio

        page = await self._control.list_product_kernel_tasks(after_task_id=self._cursor, limit=25)
        items = page.get("Items")
        cursor = page.get("NextCursor")
        if not isinstance(items, list) or len(items) > 25 or not isinstance(cursor, str):
            raise ValueError("invalid Kernel recovery page")
        first_error = None
        for item in items:
            try:
                if (
                    not isinstance(item, dict)
                    or type(item.get("AfterSeq")) is not int
                    or item["AfterSeq"] < 0
                    or any(
                        not isinstance(item.get(name), str) or not 0 < len(item[name]) <= 256
                        for name in (
                            "A2ATaskId",
                            "TargetVersionId",
                            "RemoteTaskId",
                            "RemoteContextId",
                            "SessionId",
                            "InitialCommandId",
                        )
                    )
                    or not item["SessionId"].startswith("a2a-kernel-")
                ):
                    raise ValueError("invalid Kernel recovery binding")
                facts = []
                task_id = item["A2ATaskId"]
                self._sweep_tasks.add(task_id)
                start_seq = max(item["AfterSeq"], self._scan_cursors.get(task_id, 0))
                observed_seq = start_seq
                stream = self._events.subscribe(item["SessionId"], start_seq)
                try:
                    try:
                        async with asyncio.timeout(self._scan_timeout):
                            count = 0
                            async for fact in stream:
                                count += 1
                                if type(fact.seq) is not int or fact.seq <= observed_seq:
                                    raise ValueError("Kernel recovery sequence must increase")
                                observed_seq = fact.seq
                                if fact.session_id != item["SessionId"]:
                                    raise PermissionError("Kernel recovery session mismatch")
                                if (
                                    fact.seq > item["AfterSeq"]
                                    and fact.family == "control"
                                    and fact.event_type == "control.run_transition"
                                    and fact.causation_id == item["InitialCommandId"]
                                    and fact.payload.get("state") in STATES
                                ):
                                    facts.append(fact.model_dump(mode="json"))
                                if len(facts) >= 100 or count >= 1000:
                                    break
                    except TimeoutError:
                        pass
                finally:
                    await stream.aclose()
                if facts:
                    await self._control.append_product_kernel_facts(
                        platform_task_id=item["A2ATaskId"],
                        target_version_id=item["TargetVersionId"],
                        remote_task_id=item["RemoteTaskId"],
                        remote_context_id=item["RemoteContextId"],
                        events=facts,
                    )
                # Advance only after eligible facts have been acknowledged. Losing this
                # optimization on restart causes replay, never lost facts.
                self._scan_cursors[task_id] = observed_seq
            except Exception as exc:  # noqa: BLE001 -- one broken Task must not starve the page
                if first_error is None:
                    first_error = exc
        self._cursor = cursor
        if not cursor:
            # Retain progress for every active Task. Capacity eviction can starve
            # all long streams when a sweep contains more Tasks than the cache.
            self._scan_cursors = {
                task_id: seq
                for task_id, seq in self._scan_cursors.items()
                if task_id in self._sweep_tasks
            }
            self._sweep_tasks.clear()
        if first_error is not None:
            raise first_error
