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
                and "未找到对应的 Agent" in (
                    str(details.get("remote_error_message") or "") + resp_text
                )
            )
        ):
            client._log_http_error(
                method="POST",
                full_url="http://example.com/?Action=GetAgent&Version=2024-06-12",
                status_code=404,
                resp_text='{"Message":"未找到对应的 Agent"}',
                details={"remote_error_message": "未找到对应的 Agent", "http_status": 404},
            )

    assert "Request failed" not in caplog.text
