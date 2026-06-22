import logging

from ksadk.api.client import AgentEngineClient


def test_client_can_suppress_selected_http_error_logs(caplog):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")

    with caplog.at_level(logging.ERROR, logger="ksadk.api.client"):
        with client.suppress_http_error_logging(
            lambda *, method, full_url, status_code, resp_text, details: (
                method == "POST"
                and "Action=GetAgent" in full_url
                and status_code == 404
                and "未找到对应的 Agent" in str(details.get("remote_error_message") or "")
            )
        ):
            client._log_http_error(
                method="POST",
                full_url="http://example.com/?Action=GetAgent&Version=2024-06-12",
                status_code=404,
                details={"remote_error_message": "未找到对应的 Agent", "http_status": 404},
            )

    assert "Request failed" not in caplog.text


def test_client_error_log_redacts_url_query(caplog):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")

    with caplog.at_level(logging.ERROR, logger="ksadk.api.client"):
        client._log_http_error(
            method="POST",
            full_url="http://example.com/?Action=GetAgent&Password=secret",
            status_code=500,
            details={"http_status": 500},
        )

    assert "status=500" in caplog.text
    assert "example.com" not in caplog.text
    assert "Password=secret" not in caplog.text


def test_client_error_log_redacts_sensitive_response_body(caplog):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")

    with caplog.at_level(logging.ERROR, logger="ksadk.api.client"):
        client._log_http_error(
            method="POST",
            full_url="http://example.com/?Action=GetAgent",
            status_code=500,
            details={"http_status": 500},
        )

    assert "response body omitted" not in caplog.text
    assert "password" not in caplog.text
    assert "token" not in caplog.text
    assert "secret" not in caplog.text
    assert "abc" not in caplog.text
