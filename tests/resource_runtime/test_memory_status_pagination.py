from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend


def backend(handler):
    value = SdkLTMBackend(
        index="fixture", access_key="fake-ak", secret_key="fake-sk", namespace="fixture-collection"
    )
    value._aicp_client = SimpleNamespace(call=handler)
    return value


def page(start, count, *, total=None, total_field="TotalCount", code=None):
    payload = {
        "Items": [{"SessionId": f"session-{i}", "State": 100} for i in range(start, start + count)]
    }
    if total is not None:
        payload[total_field] = total
    response = {"Data": payload}
    if code is not None:
        response["Code"] = code
    return response


def test_memory_status_accepts_real_service_success_envelope():
    response = page(0, 1, total=1, total_field="Total", code=200)
    result = backend(lambda *args, **kwargs: response).get_extraction_status(
        user_id="user-a", session_id="session-0"
    )
    assert result.status == "extracted"
    assert result.error_code == ""


def test_memory_status_can_find_session_21_without_claiming_searchable():
    requests = []

    def call(action, params, **kwargs):
        assert action == "ListSessions"
        requests.append(dict(params))
        return page(0, 20, total=21) if params["Page"] == 1 else page(20, 1, total=21)

    result = backend(call).get_extraction_status(user_id="user-a", session_id="session-20")
    assert result.status == "extracted"
    assert result.error_code == ""
    assert not result.searchable
    assert [item["Page"] for item in requests] == [1, 2]
    assert all(item["AgentUserId"] == "user-a" for item in requests)


def test_query_failure_is_unknown_not_failed_write_and_does_not_leak(caplog):
    def fail(*args, **kwargs):
        raise RuntimeError("fixture-secret-in-upstream-url")

    result = backend(fail).get_extraction_status(user_id="user-a", session_id="session-a")
    assert result.status == "unknown"
    assert result.error_code == "MEMORY_STATUS_QUERY_FAILED"
    assert "fixture-secret" not in caplog.text
    assert "fixture-secret" not in str(result)


def test_missing_session_is_distinct_from_query_failure():
    result = backend(lambda *args, **kwargs: page(0, 0)).get_extraction_status(
        user_id="user-a", session_id="absent"
    )
    assert result.status == "unknown"
    assert result.error_code == ""


@pytest.mark.parametrize(
    "change",
    [
        {"ResponseMetadata": {"Error": {"Code": "Denied"}}},
        {"Code": False},
    ],
)
def test_error_envelope_cannot_be_treated_as_extracted(change):
    response = {**page(0, 1), **change}
    result = backend(lambda *args, **kwargs: response).get_extraction_status(
        user_id="user-a", session_id="session-0"
    )
    assert result.status == "unknown"
    assert result.error_code == "MEMORY_STATUS_QUERY_FAILED"


@pytest.mark.parametrize("field", ["AgentUserId", "MemoryCollectionId"])
def test_mismatched_returned_scope_is_not_an_extraction_confirmation(field):
    response = page(0, 1)
    response["Data"]["Items"][0][field] = "another-scope"
    result = backend(lambda *args, **kwargs: response).get_extraction_status(
        user_id="user-a", session_id="session-0"
    )
    assert result.status == "unknown"
    assert result.error_code == "MEMORY_STATUS_SCOPE_MISMATCH"


@pytest.mark.parametrize("case", ["repeated", "budget", "malformed", "incomplete"])
def test_incomplete_queries_have_diagnostics(case):
    response = {
        "repeated": page(0, 20),
        "budget": page(0, 20),
        "malformed": {"Data": {"Items": {}}},
        "incomplete": page(0, 1, total=5),
    }[case]
    value = backend(lambda *args, **kwargs: response)
    item = value.get_session_status(
        user_id="user-a", session_id="absent", max_pages=1 if case == "budget" else 3
    )
    assert item is None
    assert value.last_session_status == {}
    assert (
        value.last_error
        == {
            "repeated": "MEMORY_STATUS_PAGINATION_REPEATED",
            "budget": "MEMORY_STATUS_PAGE_BUDGET_EXHAUSTED",
            "malformed": "MEMORY_STATUS_RESPONSE_INVALID",
            "incomplete": "MEMORY_STATUS_DIRECTORY_INCOMPLETE",
        }[case]
    )


def test_legacy_status_does_not_keep_previous_error_or_session():
    value = backend(lambda *args, **kwargs: page(0, 1))
    value.last_error = "stale"
    value.last_session_status = {"SessionId": "old"}
    assert value.get_session_status(user_id="user-a", session_id="session-0")["State"] == 100
    assert value.last_error == ""
    value.get_session_status(user_id="user-a", session_id="absent")
    assert value.last_session_status == {}


def test_structured_concurrent_results_do_not_read_shared_last_error():
    barrier = Barrier(2)

    def call(action, params, **kwargs):
        barrier.wait(timeout=5)
        if params["AgentUserId"] == "failed-user":
            raise RuntimeError("fixture failure")
        return page(0, 1)

    value = backend(call)
    with ThreadPoolExecutor(max_workers=2) as pool:
        failed = pool.submit(
            value.get_extraction_status, user_id="failed-user", session_id="session-0"
        )
        successful = pool.submit(
            value.get_extraction_status, user_id="successful-user", session_id="session-0"
        )
        assert successful.result().status == "extracted"
        assert failed.result().error_code == "MEMORY_STATUS_QUERY_FAILED"
