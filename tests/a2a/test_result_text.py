from types import SimpleNamespace

from ksadk.a2a.result_text import select_terminal_reply


def ref(scope: str, item: str, part: str | None = None):
    return SimpleNamespace(scope_id=scope, item_id=item, part_id=part)


def test_legacy_reply_prefers_last_artifact_over_stale_status_message() -> None:
    snapshots = {
        ("scope", "answer"): {"text": "R85 input continued with value: Shanghai"},
        ("scope", "stale-status"): {"text": "input_required"},
    }
    kinds = {
        ("scope", "answer"): "artifact",
        ("scope", "stale-status"): "message",
    }

    assert (
        select_terminal_reply(
            [ref("scope", "answer", "text"), ref("scope", "stale-status", "text")],
            snapshots,
            kinds,
        )
        == "R85 input continued with value: Shanghai"
    )


def test_legacy_reply_selects_last_artifact_instead_of_concatenating_turns() -> None:
    snapshots = {
        ("scope", "first"): {"text": "first answer"},
        ("scope", "final"): {"part-1": "final ", "part-2": "answer"},
    }
    kinds = {key: "artifact" for key in snapshots}

    assert (
        select_terminal_reply([ref("scope", "first"), ref("scope", "final")], snapshots, kinds)
        == "final answer"
    )


def test_legacy_reply_falls_back_to_last_message_for_message_only_peer() -> None:
    snapshots = {
        ("scope", "first"): {"text": "first"},
        ("scope", "final"): {"text": "final"},
    }
    kinds = {key: "message" for key in snapshots}

    assert (
        select_terminal_reply([ref("scope", "first"), ref("scope", "final")], snapshots, kinds)
        == "final"
    )
