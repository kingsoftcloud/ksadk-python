from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _reset_harness_session_service_cache():
    from ksadk.sessions import reset_session_service

    await reset_session_service()
    yield
    await reset_session_service()
