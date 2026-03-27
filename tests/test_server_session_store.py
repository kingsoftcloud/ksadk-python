from __future__ import annotations

import warnings

from ksadk.server.session_store import get_session_store


def test_session_store_compatibility_layer_delegates_to_new_service():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        store = get_session_store()

    assert any(item.category is DeprecationWarning for item in caught)

    session = store.create_session("demo-agent", "user-1")
    assert session.app_name == "demo-agent"
    assert session.user_id == "user-1"

    assert store.add_event(
        session.id,
        {
            "id": "evt-1",
            "author": "user",
            "content": {"role": "user", "parts": [{"text": "hello"}]},
        },
    )
    assert store.update_state(session.id, {"topic": "billing"})

    fetched = store.get_session(session.id)
    assert fetched is not None
    assert fetched.events[0]["id"] == "evt-1"
    assert fetched.state == {"topic": "billing"}

    listed = store.list_sessions("demo-agent", "user-1")
    assert [item.id for item in listed] == [session.id]

    assert store.delete_session(session.id) is True
