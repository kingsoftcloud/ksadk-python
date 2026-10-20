"""Run-scoped access to the Kernel-owned, fenced canonical event store."""

from contextlib import contextmanager
from contextvars import ContextVar

from ksadk.events.canonical_store import RuntimeEventStore


class RunEventAuthority:
    """An ephemeral capability, never serialized into request/session state."""

    def __init__(
        self,
        event_store,
        *,
        session_id,
        run_id,
        guard,
        interaction_ledger=None,
        tenant_id=None,
        agent_instance_id=None,
        managed_a2a_bootstraps=None,
    ):
        self.session_id, self.run_id = session_id, run_id
        self._store = RuntimeEventStore(event_store, session_id=session_id)
        self._guard = guard
        self._active = False
        self._used = False
        self._events = event_store
        self._ledger = interaction_ledger
        self._tenant, self._agent = tenant_id, agent_instance_id
        self._managed_a2a_bootstraps = (
            managed_a2a_bootstraps if managed_a2a_bootstraps is not None else {}
        )

    def _require(self, session_id, run_id):
        if not self._active:
            raise RuntimeError("canonical run authority is no longer active")
        if session_id != self.session_id or run_id != self.run_id:
            raise ValueError("canonical run authority identity mismatch")

    def require_session(self, session_id):
        self._require(session_id, self.run_id)

    @property
    def publication_epoch(self):
        """Identify the actual Kernel owner, not a fabricated framework run."""
        from ksadk.events.identity import stable_item_id

        self.require_session(self.session_id)
        return stable_item_id(
            "kernel", self._guard.activation_id, str(self._guard.fencing_token)
        )

    def managed_invocation_policy(self):
        """Compose approval and audited authorization within this run's fence."""
        from datetime import datetime, timezone
        from uuid import uuid4

        from ksadk.a2a.invocation_policy import runtime_a2a_invocation_policy
        from ksadk.a2a.runtime_approval import KernelA2AApprovalResolver, approval_target
        from ksadk.kernel.contracts import SessionEventEnvelope

        self.require_session(self.session_id)
        if self._ledger is None or not self._tenant or not self._agent:
            raise RuntimeError("A2A_KERNEL_POLICY_DEPENDENCIES_REQUIRED")

        async def audit(request, decision, route):
            self._require(request.handle.session_id, request.handle.run_id)
            await self._events.append(
                SessionEventEnvelope(
                    event_id=uuid4(),
                    session_id=self.session_id,
                    seq=0,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    family="control",
                    family_version=1,
                    event_type="control.a2a_policy_evaluated",
                    run_id=self.run_id,
                    actor_ref="agent-kernel",
                    payload={
                        "schema": "a2a.policy-audit/v1",
                        "invocation_sha256": approval_target(request)["invocation_sha256"],
                        "action": decision.action,
                        "reason_code": decision.reason_code,
                        "route_kind": route,
                    },
                ),
                guard=self._guard,
            )

        return runtime_a2a_invocation_policy(
            audit=audit,
            approval_resolver=KernelA2AApprovalResolver(
                self._ledger, self._events, tenant_id=self._tenant, agent_instance_id=self._agent
            ),
        )

    def register_managed_a2a(self, bootstrap):
        self._require(self.session_id, self.run_id)
        if getattr(bootstrap, "scoped_cancel_available", False):
            self._managed_a2a_bootstraps[bootstrap.runtime_metadata.runtime_id] = bootstrap

    async def cancel_a2a_scope(self, scope_id):
        """Resolve a caller-owned child, then reuse the durable cancel protocol."""
        self._require(self.session_id, self.run_id)
        for bootstrap in tuple(self._managed_a2a_bootstraps.values()):
            saved = await bootstrap.invocation_checkpoint_store.read(scope_id)
            if saved is None:
                continue
            if (saved.handle.session_id != self.session_id
                    or saved.handle.run_id != self.run_id
                    or saved.request.context.caller_agent_id
                    != bootstrap.runtime_metadata.agent_id):
                raise ValueError("A2A_SCOPE_OWNER_CONFLICT")
            service = bootstrap.invocation_service_for_space(
                saved.request.spec.space_id, event_store=self,
                policy=self.managed_invocation_policy(),
            )
            # cancel() re-reads under a lease, or atomically records intent when
            # the active invocation owns it; this unlocked read grants no write.
            return await service.cancel(saved.handle)
        raise KeyError("A2A_INVOCATION_NOT_FOUND")

    async def persist_one(self, session_id, event):
        self._require(session_id, event.run_id)
        existing = await self._store.resolve_existing(session_id, event)
        # Always use the guarded append, even on a duplicate. An expired owner
        # must not regain publication authority merely because a fact exists.
        persisted = await self._store.append_typed(event, guard=self._guard)
        return persisted, existing is None

    async def save_runtime_checkpoint(self, checkpoint):
        """Persist private adapter state under this exact live Kernel owner.

        Checkpoint contents may include native conversation records; they must
        never be published as SessionEvents or returned as control receipts.
        """
        self._require(self.session_id, self.run_id)
        save = getattr(self._ledger, "save_runtime_checkpoint", None)
        if not callable(save) or not self._agent:
            raise RuntimeError("runtime checkpoint store is unavailable")
        await save(self.run_id, checkpoint, agent_instance_id=self._agent,
                   session_id=self.session_id, guard=self._guard)

    def runtime_checkpoint_identity(self):
        self._require(self.session_id, self.run_id)
        if not self._agent or not self._tenant:
            raise RuntimeError("runtime checkpoint identity is unavailable")
        return {"agent_instance_id": self._agent, "tenant_id": self._tenant}

    async def list(self, session_id, *, run_id=None, **kwargs):
        run_id = self.run_id if run_id is None else run_id
        self._require(session_id, run_id)
        return await self._store.list(session_id, run_id=run_id, **kwargs)

    async def latest(self, session_id, **kwargs):
        self._require(session_id, self.run_id)
        return await self._store.latest(session_id, run_id=self.run_id, **kwargs)

    async def first(self, session_id, **kwargs):
        self._require(session_id, self.run_id)
        return await self._store.first(session_id, run_id=self.run_id, **kwargs)

    async def event_by_id(self, session_id, event_id):
        self._require(session_id, self.run_id)
        event = await self._store.event_by_id(session_id, event_id)
        if event is not None:
            self._require(session_id, event.run_id)
        return event


_current: ContextVar[RunEventAuthority | None] = ContextVar("canonical_run_authority", default=None)


def current_run_event_authority():
    return _current.get()


@contextmanager
def bind_run_event_authority(authority):
    if authority._used:
        raise RuntimeError("canonical run authority cannot be rebound")
    authority._used = True
    authority._active = True
    token = _current.set(authority)
    try:
        yield authority
    finally:
        authority._active = False
        _current.reset(token)
