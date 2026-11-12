"""Live interaction waiters must never become native ADK session history."""

import copy
import time
from types import SimpleNamespace

import pytest
from google.adk.sessions import InMemorySessionService

from ksadk.a2a.adk import _project, canonical_event_from_adk
from ksadk.a2a.runtime_approval import ManagedA2AApprovalWaiter
from ksadk.a2a.runtime_input import ManagedA2AInputWaiter
from ksadk.events.canonical import InteractionRequested, SourceRef, StructuredInputRequest
from tests.a2a.test_invocation_service_r85 import request


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["approval", "structured_input"])
async def test_native_session_remains_readable_after_live_interaction(kind):
    req = request()
    if kind == "approval":
        waiter = ManagedA2AApprovalWaiter(req)
        canonical = waiter.event(timestamp=time.time())
    else:
        canonical = InteractionRequested(
            schema_version=2, seq=0, timestamp=time.time(), event_id="input-event",
            run_id=req.handle.run_id, scope_id=req.handle.scope_id,
            source=SourceRef(framework="a2a"), interaction_id="input-1",
            interaction_kind="structured_input",
            request=StructuredInputRequest(schema={"type": "string"}),
        )
        waiter = ManagedA2AInputWaiter(
            canonical, SimpleNamespace(handle=req.handle, continuation_id="continuation-1")
        )
        canonical = waiter.event
    try:
        service = InMemorySessionService()
        session = await service.create_session(app_name="test", user_id="user", session_id="s")
        native = _project(canonical, "remote", SimpleNamespace(invocation_id="native", branch=None))
        assert canonical_event_from_adk(native) is canonical
        await service.append_event(session, native)
        # Both during the wait and after resolution, a second native run must
        # be able to read/copy history without copying a Future or a live waiter.
        for close in (False, True):
            if close:
                waiter.close()
            restored = await service.get_session(app_name="test", user_id="user", session_id="s")
            copy.deepcopy(restored)
            assert restored.events == []
    finally:
        waiter.close()
