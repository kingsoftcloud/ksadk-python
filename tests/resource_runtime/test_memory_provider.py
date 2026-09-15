from dataclasses import replace
from types import SimpleNamespace

import pytest

from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend
from ksadk.memory.coordinator import MemoryCoordinator, recall_to_context_item
from ksadk.memory.models import MemorySearchRequest, UnsupportedMemoryOperation
from ksadk.resource_runtime.contracts import InvocationIdentity, ResourceConfig
from ksadk.resource_runtime.memory_provider import AicpMemoryProvider


def provider(*, user="user-a", session="session-a", response=None):
    identity = InvocationIdentity(
        tenant_ref="tenant-a",
        resource_principal_ref="account-a",
        actor_ref=user,
        memory_subject_ref=user,
        agent_id="agent-a",
        session_ref=session,
    )
    config = ResourceConfig.model_validate(
        {
            "binding": {
                "id": "memory-binding",
                "connectionRef": "connection-a",
                "resource": {"kind": "memory-instance", "id": "collection-a", "region": "region-a"},
            }
        }
    )
    sdk = SdkLTMBackend(
        index="fixture",
        memory_collection_id="collection-a",
        region="region-a",
        access_key="fake-ak",
        secret_key="fake-sk",
    )
    calls = []

    def call(action, params, **kwargs):
        calls.append(params)
        return (
            response
            if response is not None
            else {
                "Data": [
                    {"Memories": [{"MemoryId": "real-upstream-id", "Memory": "a remembered fact"}]}
                ]
            }
        )

    sdk._aicp_client = SimpleNamespace(call=call)
    return AicpMemoryProvider(config, identity, sdk), calls


def request(value, **changes):
    return replace(
        MemorySearchRequest(
            query="recall", scopes=[("user", value.partition)], memory_types=[], min_score=0
        ),
        **changes,
    )


def test_real_sdk_parser_preserves_id_and_unknown_fields_in_coordinator():
    value, calls = provider()
    result = MemoryCoordinator(value).recall(request(value))
    assert result.status == "ok"
    record = result.records[0]
    assert record.memory_id == "real-upstream-id"
    assert record.version is None and record.confidence is None and record.importance is None
    assert record.memory_type == "unknown"
    assert record.metadata["score"] is None
    assert calls[0]["AgentUserId"] == value.partition
    assert recall_to_context_item(result) is not None


def test_partition_is_stable_across_sessions_and_separate_for_users():
    a, _ = provider()
    resumed, _ = provider(session="new-session")
    b, calls = provider(user="user-b")
    assert a.partition == resumed.partition and a.partition != b.partition
    result = b.search(request(a))
    assert result.status == "unauthorized"
    assert calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"min_score": 0.45},
        {"filters": {"type": "fact"}},
        {"memory_types": ["fact"]},
        {"as_of": "2026-01-01"},
    ],
)
def test_unsupported_search_policy_is_not_silently_ignored(change):
    value, calls = provider()
    assert value.search(request(value, **change)).error_code == "MEMORY_SEARCH_POLICY_UNSUPPORTED"
    assert calls == []


@pytest.mark.parametrize(
    "response",
    [
        {"Error": "upstream-failure", "Data": []},
        {"Data": {}},
        {"Data": [{"Memories": [{"Memory": "missing id"}]}]},
    ],
)
def test_invalid_response_is_not_empty_success(response):
    value, _ = provider(response=response)
    result = value.search(request(value))
    assert result.status == "failed"
    assert not result.records


def test_zero_budget_makes_no_call_and_preserves_empty_result():
    value, calls = provider()
    result = value.search(request(value, max_tokens=0))
    assert result.status == "ok" and result.truncated_by_budget
    assert calls == []


def test_provider_does_not_fabricate_sync_write_or_cas():
    value, calls = provider()
    caps = value.capabilities()
    assert not caps.versioned_update and not caps.ttl and not caps.hard_delete
    record = value.search(request(value)).records[0]
    with pytest.raises(UnsupportedMemoryOperation):
        value.upsert(record, expected_version=None)
    assert len(calls) == 1
