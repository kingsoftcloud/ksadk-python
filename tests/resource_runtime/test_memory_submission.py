import pytest

from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend


@pytest.mark.parametrize(
    "response",
    [
        {},
        [],
        "not-json",
        {"RequestId": "r", "Error": {"Code": "Denied"}},
        {"RequestId": "r", "Code": True},
        {"RequestId": "r", "Success": False},
        {"RequestId": "r", "Code": 500},
        {"ResponseMetadata": {"RequestId": "r", "Error": {"Code": "Denied"}}},
    ],
)
def test_unconfirmed_submission_is_not_accepted(response):
    class Client:
        def call(self, *args, **kwargs):
            return response

    backend = SdkLTMBackend(index="fixture", access_key="fake-ak", secret_key="fake-sk")
    backend._aicp_client = Client()
    backend.last_create_response = {"RequestId": "previous-success"}
    assert not backend.save_memory("user", ["test content"], strict=True)
    assert backend.last_error in {"MEMORY_WRITE_RESPONSE_UNKNOWN", "MEMORY_WRITE_UNKNOWN"}
    assert backend.last_create_response != {"RequestId": "previous-success"}


@pytest.mark.parametrize(
    "response",
    [
        {"RequestId": "ack"},
        {"ResponseMetadata": {"RequestId": "ack"}},
        {"Code": 200, "Message": "success", "Data": {"TaskId": "task"}, "RequestId": "ack"},
    ],
)
def test_acknowledgment_is_accepted_without_claiming_searchability(response):
    class Client:
        def call(self, *args, **kwargs):
            return response

    backend = SdkLTMBackend(index="fixture", access_key="fake-ak", secret_key="fake-sk")
    backend._aicp_client = Client()
    assert backend.save_memory("user", ["test content"], strict=True)
    assert backend.last_error == ""
