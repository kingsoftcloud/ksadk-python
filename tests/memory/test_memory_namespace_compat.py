"""Keep persisted local namespaces while encoding only AICP request identities."""

import hashlib
import json
import re
from dataclasses import replace

import pytest

from ksadk.memory.coordinator import MemoryCoordinator, agent_user_scope_id, build_search_request
from ksadk.memory.models import MemoryRecord
from ksadk.memory.providers.local_sqlite import SqliteMemoryProvider


@pytest.mark.parametrize("logical_id", ["", "   ", "agent:example-agent:user:", "agent::user:example-user"])
def test_missing_wire_identity_is_rejected_before_cloud_call(logical_id):
    from ksadk.memory.scope_ids import aicp_user_id

    with pytest.raises(ValueError, match="MEMORY_USER_ID_REQUIRED"):
        aicp_user_id(logical_id)


def test_sqlite_legacy_agent_memory_survives_reopen_and_update(tmp_path):
    # This is the literal namespace written by releases before 0.8.8; do not
    # generate it with the candidate helper or the test could mask a migration.
    legacy = "agent:example-agent:user:example-user"
    record = MemoryRecord(
        memory_id="legacy-record", tenant_id="example-tenant", workspace_id="example-workspace",
        scope="user", scope_id=legacy, memory_type="fact", content="legacy_fact_088 preserved",
        summary="legacy fact", status="active", confidence=.9, importance=.8,
        valid_from="", valid_to="", expires_at="", source_session_id="legacy-session",
        source_event_ids=["legacy-event"], source_seq_range=(1, 2), content_hash="legacy-hash",
        version=1, metadata={"legacy": True},
    )
    options = dict(db_path=tmp_path / "memory.sqlite", tenant_id="example-tenant", workspace_id="example-workspace")
    before = SqliteMemoryProvider(**options)
    saved = before.upsert(record, expected_version=None)
    before.close()
    after = SqliteMemoryProvider(**options)
    try:
        scope = agent_user_scope_id(agent_id="example-agent", user_id="example-user")
        found = MemoryCoordinator(after).recall(build_search_request(query="legacy_fact_088", user_id=scope, min_score=0))
        assert found.records == [saved]
        assert after.get(saved.memory_id) == saved
        updated = after.upsert(replace(saved, content="legacy_fact_088 updated"), expected_version=1)
        assert updated.scope_id == legacy and updated.version == 2
        for agent, user in (("other-agent", "example-user"), ("example-agent", "other-user")):
            other_scope = agent_user_scope_id(agent_id=agent, user_id=user)
            assert not after.search(build_search_request(query="legacy_fact_088", user_id=other_scope, min_score=0)).records
    finally:
        after.close()


@pytest.mark.parametrize("logical_id, wire_id", [
    ("agent:example-agent:user:example-user", "example-agent_example-user"),
    ("agent:a_b:user:c", hashlib.sha256(b"agent-user-scope/v1|a_b|c").hexdigest()[:32]),
    ("local-user", "local-user"),
    ("mp1-existing-partition", "mp1-existing-partition"),
])
def test_sdk_roundtrip_uses_one_safe_wire_identity_without_changing_logical_id(logical_id, wire_id):
    from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend

    class MemoryApi:
        calls = None

        def __init__(self):
            self.calls = []
            self.content = "namespace_fact_088"

        def call(self, action, params, options=None):
            return json.dumps(self.response(action, params))

        def response(self, action, params):
            assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", params["AgentUserId"])
            assert params["AgentUserId"] == wire_id
            self.calls.append(action)
            item = {"MemoryId": "fixture-memory", "Memory": self.content, "AgentUserId": wire_id, "Score": .9}
            if action == "QueryMemorySdk":
                return {"Code": 0, "Data": [{"Memories": [item]}]}
            if action == "ListMemories":
                return {"Code": 0, "MemoryList": [item]}
            if action == "ListSessions":
                return {"Code": 0, "Data": {"Items": [{"SessionId": "fixture-session", "AgentUserId": wire_id, "State": 100}], "TotalCount": 1}}
            if action == "UpdateMemory":
                self.content = params["Content"]
                return {"Code": 0, "RequestId": "fixture-update", "new_memory_id": "fixture-memory"}
            assert action in ("CreateMemorySdk", "DeleteMemory")
            return {"Code": 0, "RequestId": "fixture-request"}

    client = MemoryApi()
    backend = SdkLTMBackend(index="fixture-collection", access_key="fixture-ak", secret_key="fixture-sk")
    backend._aicp_client = client
    assert backend.save_memory(logical_id, [json.dumps({"parts": [{"text": client.content}]})], session_id="fixture-session", strict=True)
    assert backend.search_memory(logical_id, "namespace_fact_088")
    records = backend.search_records(logical_id, "namespace_fact_088", strict=True)
    assert records[0].user_id == logical_id
    assert backend.list_memory_records(user_id=logical_id)[0].user_id == logical_id
    assert backend.get_session_status(user_id=logical_id, session_id="fixture-session")["State"] == 100
    assert backend.update_memory(user_id=logical_id, memory_id="fixture-memory", content="updated", strict=True).ok
    # This only calls the in-process fixture, never an external delete API.
    assert backend.delete_memory(user_id=logical_id, memory_id="fixture-memory", strict=True).ok
    assert set(client.calls) == {"CreateMemorySdk", "QueryMemorySdk", "ListMemories", "ListSessions", "UpdateMemory", "DeleteMemory"}
