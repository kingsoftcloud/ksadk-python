from types import SimpleNamespace

import pytest

from tests.resource_runtime.test_memory_provider import provider, request


def observed(response):
    value, _ = provider()
    assert value.search(request(value)).status == "ok"
    calls = []

    def call(action, params, **kwargs):
        calls.append((action, params))
        return response

    value._backend._aicp_client = SimpleNamespace(call=call)
    return value, calls


def test_update_tracks_only_real_returned_id_and_delete_is_scoped():
    value, calls = observed({"RequestId": "ack", "Data": {"NewMemoryId": "new-real-id"}})
    result = value.mutate(
        "update_memory",
        {
            "memoryId": "real-upstream-id",
            "content": "new preference",
        },
        operation_id="a" * 64,
    )
    assert result["status"] == "succeeded"
    assert result["newMemoryId"] == "new-real-id"
    assert calls[0] == (
        "UpdateMemory",
        {
            "MemoryCollectionId": "collection-a",
            "MemoryId": "real-upstream-id",
            "Content": "new preference",
            "AgentUserId": value.partition,
        },
    )
    stale = value.mutate("delete_memory", {"memoryId": "real-upstream-id"}, operation_id="b" * 64)
    assert stale["status"] == "failed"
    assert len(calls) == 1
    deleted = value.mutate("delete_memory", {"memoryId": "new-real-id"}, operation_id="c" * 64)
    assert deleted["status"] == "succeeded"
    assert calls[-1][0] == "DeleteMemory"
    assert calls[-1][1]["AgentUserId"] == value.partition
    assert not value.capabilities().hard_delete
    assert not value.capabilities().versioned_update


@pytest.mark.parametrize("operation", ["update_memory", "delete_memory"])
@pytest.mark.parametrize(
    "response",
    [
        {},
        "invalid-json",
        {"RequestId": "r", "Success": False},
        {"RequestId": "r", "Error": {"Code": "Denied"}},
    ],
)
def test_unconfirmed_response_is_unknown_and_cannot_reuse_observed_id(operation, response):
    value, calls = observed(response)
    arguments = {"memoryId": "real-upstream-id"}
    if operation == "update_memory":
        arguments["content"] = "replacement"
    result = value.mutate(operation, arguments, operation_id="a" * 64)
    assert result["status"] == "unknown"
    assert result["newMemoryId"] == ""
    assert value.mutate(operation, arguments, operation_id="b" * 64)["status"] == "failed"
    assert len(calls) == 1


def test_update_without_new_id_does_not_guess_from_matching_text():
    value, calls = observed({"RequestId": "ack"})
    result = value.mutate(
        "update_memory",
        {
            "memoryId": "real-upstream-id",
            "content": "replacement",
        },
        operation_id="a" * 64,
    )
    assert result["status"] == "succeeded"
    assert result["newMemoryId"] == ""
    assert [action for action, _ in calls] == ["UpdateMemory"]


def test_legacy_sdk_mutation_response_compatibility_remains_available(monkeypatch):
    value, calls = observed({})
    monkeypatch.setattr(
        type(value._backend),
        "list_memory_records",
        lambda self, **kwargs: [
            SimpleNamespace(memory_id="legacy-reconciled-id", content="replacement")
        ],
    )
    updated = value._backend.update_memory(
        user_id=value.partition,
        memory_id="real-upstream-id",
        content="replacement",
    )
    assert updated.ok and updated.new_memory_id == "legacy-reconciled-id"
    deleted = value._backend.delete_memory(
        user_id=value.partition, memory_id="legacy-reconciled-id"
    )
    assert deleted.ok and deleted.status == "deleted"
    assert [action for action, _ in calls] == ["UpdateMemory", "DeleteMemory"]


def test_another_users_observed_id_does_not_authorize_mutation():
    other, calls = provider(user="different-user")
    result = other.mutate("delete_memory", {"memoryId": "real-upstream-id"}, operation_id="a" * 64)
    assert result["errorCode"] == "MEMORY_RECORD_NOT_OBSERVED"
    assert calls == []


@pytest.mark.parametrize("extra", [{"userId": "other"}, {"expectedVersion": 1}, {"hard": True}])
def test_mutation_rejects_scope_cas_and_hard_delete_arguments(extra):
    value, calls = observed({"RequestId": "ack"})
    with pytest.raises(ValueError):
        value.mutate(
            "delete_memory", {"memoryId": "real-upstream-id", **extra}, operation_id="a" * 64
        )
    assert calls == []
