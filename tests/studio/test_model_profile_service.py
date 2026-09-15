from ksadk.studio.model_profile_service import _recommended_probe


def test_probe_prefers_responses_when_both_protocols_are_available():
    attempts = [
        {"protocol": "chat", "endpointUrl": "https://example.test/v1/chat/completions", "status": "ok"},
        {"protocol": "responses", "endpointUrl": "https://example.test/v1/responses", "status": "ok"},
    ]

    recommendation = _recommended_probe(attempts)

    assert recommendation == {
        "protocol": "responses",
        "wireApi": "responses",
        "endpointUrl": "https://example.test/v1/responses",
        "status": "ok",
    }


def test_probe_keeps_chat_when_responses_is_unavailable():
    attempts = [
        {"protocol": "responses", "endpointUrl": "https://example.test/v1/responses", "status": "unavailable"},
        {"protocol": "chat", "endpointUrl": "https://example.test/v1/chat/completions", "status": "ok"},
    ]

    recommendation = _recommended_probe(attempts)

    assert recommendation is not None
    assert recommendation["wireApi"] == "chat"
