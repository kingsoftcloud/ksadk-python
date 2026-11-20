"""Keep persisted local namespaces while encoding only AICP request identities."""

import hashlib
import json
import re
from dataclasses import replace

import pytest

from ksadk.memory.coordinator import MemoryCoordinator, agent_user_scope_id, build_search_request
from ksadk.memory.models import MemoryRecord
from ksadk.memory.providers.local_sqlite import SqliteMemoryProvider


@pytest.mark.parametrize("length", [1, 63, 64, 65, 68, 127, 128, 129, 255, 256, 257, 10000])
@pytest.mark.parametrize("character", ["a", ":", "界"])
def test_memory_wire_user_id_boundaries_preserve_only_valid_existing_ids(length, character):
    from ksadk.memory.scope_ids import aicp_user_id

    logical = character * length
    encoded = aicp_user_id(logical)
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", encoded)
    assert aicp_user_id(logical) == encoded
    if character == "a" and length <= 64:
        assert encoded == logical


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
    ("aeu_" + "a" * 64, hashlib.sha256(("agent-user-scope/v1||aeu_" + "a" * 64).encode()).hexdigest()[:32]),
    ("a2a-user-v1:" + "b" * 64, hashlib.sha256(("agent-user-scope/v1||a2a-user-v1:" + "b" * 64).encode()).hexdigest()[:32]),
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


def test_trusted_adk_identity_memory_tools_share_safe_cloud_partition(monkeypatch):
    from ksadk.memory import tool
    from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend
    from ksadk.memory.service import LongTermMemoryService
    from ksadk.runners._session_identity import ADKSessionIdentityMixin
    from ksadk.runtime_context import PlatformIdentityContext, PlatformInvocationContext, platform_invocation_scope
    from ksadk.sessions.invocation_identity import identity_native_user_id

    class MemoryApi:
        def __init__(self):
            self.saved = {}
            self.identities = []

        def call(self, action, params, options=None):
            user = params["AgentUserId"]
            assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user)
            self.identities.append(user)
            if action == "CreateMemorySdk":
                self.saved[user] = params["Data"]["Conversation"][0]["Content"][0]["Text"]
                return json.dumps({"Code": 0, "RequestId": "fixture-save"})
            assert action == "QueryMemorySdk"
            memories = [{"MemoryId": "fixture-memory", "Memory": self.saved[user]}] if user in self.saved else []
            return json.dumps({"Data": [{"Memories": memories}]})

    api = MemoryApi()
    backend = SdkLTMBackend(index="fixture-collection", access_key="fixture-ak", secret_key="fixture-sk")
    backend._aicp_client = api
    monkeypatch.setattr(tool, "_service", LongTermMemoryService(backend=backend))

    def context(subject):
        identity = PlatformIdentityContext(identity_namespace="fixture-identity", tenant_id="fixture-tenant", subject_type="user", subject_id=subject)
        return PlatformInvocationContext(
            agent_id="fixture-agent", user_id=identity_native_user_id(identity), session_id="fixture-session",
            history=[], input_content=[], input_messages=[], input_parts=[], attachments=[], attachment_results=[],
            current_attachments=[], current_attachment_results=[], has_current_files=False, runner_type="adk", identity=identity,
        )

    owner = context("owner")
    assert len(owner.user_id) == 68
    with platform_invocation_scope(owner):
        assert ADKSessionIdentityMixin()._invocation_session_owner()[0] == owner.user_id
        assert tool.save_memory("fixture user prefers Friday exhibitions")["status"] == "persisted"
        assert "Friday exhibitions" in tool.load_memory("exhibitions")
        assert owner.user_id == identity_native_user_id(owner.identity)
    assert len(set(api.identities)) == 1 and len(api.identities[0]) == 32
    with platform_invocation_scope(context("different-owner")):
        assert "Friday exhibitions" not in tool.load_memory("exhibitions")
