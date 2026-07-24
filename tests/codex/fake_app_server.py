"""Small JSON-RPC app-server used to exercise the real openai-codex transport."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

THREAD_ID = "019f0000-0000-7000-8000-000000000001"
TURN_ID = "019f0000-0000-7000-8000-000000000002"
COMMENTARY_ID = "msg_commentary"
FINAL_ID = "msg_final"
REVIEW_ID = "review_1"


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _thread() -> dict[str, Any]:
    now = int(time.time())
    return {
        "agentNickname": None,
        "agentRole": None,
        "cliVersion": "0.144.4-test",
        "createdAt": now,
        "cwd": os.getcwd(),
        "ephemeral": True,
        "forkedFromId": None,
        "gitInfo": None,
        "id": THREAD_ID,
        "modelProvider": "test",
        "name": None,
        "parentThreadId": None,
        "path": None,
        "preview": "",
        "recencyAt": now,
        "sessionId": THREAD_ID,
        "source": "vscode",
        "status": {"type": "idle"},
        "threadSource": None,
        "turns": [],
        "updatedAt": now,
    }


def _thread_response() -> dict[str, Any]:
    return {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "cwd": os.getcwd(),
        "instructionSources": [],
        "model": "test-model",
        "modelProvider": "test",
        "reasoningEffort": "low",
        "sandbox": {"networkAccess": False, "type": "readOnly"},
        "serviceTier": None,
        "thread": _thread(),
    }


def _turn(status: str = "inProgress") -> dict[str, Any]:
    return {
        "completedAt": None,
        "durationMs": None,
        "error": None,
        "id": TURN_ID,
        "items": [],
        "itemsView": "full",
        "startedAt": int(time.time()),
        "status": status,
    }


def _notify(method: str, params: dict[str, Any]) -> None:
    _write({"method": method, "params": params})


def _item_notification(item: dict[str, Any], *, completed: bool) -> None:
    timestamp_key = "completedAtMs" if completed else "startedAtMs"
    _notify(
        "item/completed" if completed else "item/started",
        {
            timestamp_key: int(time.time() * 1000),
            "item": item,
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
        },
    )


def _start_turn(block: bool) -> None:
    time.sleep(0.05)
    _notify("turn/started", {"threadId": THREAD_ID, "turn": _turn()})

    user_item = {
        "clientId": None,
        "content": [{"text": "input", "textElements": [], "type": "text"}],
        "id": "user_1",
        "type": "userMessage",
    }
    _item_notification(user_item, completed=False)
    _item_notification(user_item, completed=True)

    commentary = {
        "id": COMMENTARY_ID,
        "memoryCitation": None,
        "phase": "commentary",
        "text": "",
        "type": "agentMessage",
    }
    _item_notification(commentary, completed=False)
    _notify(
        "item/agentMessage/delta",
        {
            "delta": "thinking",
            "itemId": COMMENTARY_ID,
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
        },
    )
    commentary["text"] = "thinking"
    _item_notification(commentary, completed=True)

    # Deliberately incomplete payload: the SDK routes it as UnknownNotification,
    # which verifies that KSADK preserves method + params without fake SDK types.
    _notify(
        "item/autoApprovalReview/started",
        {
            "reviewId": REVIEW_ID,
            "targetItemId": "tool_1",
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
        },
    )
    if block:
        return
    _notify(
        "item/autoApprovalReview/completed",
        {
            "reviewId": REVIEW_ID,
            "targetItemId": "tool_1",
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "decision": "accept",
        },
    )
    _finish_turn("completed")


def _finish_turn(status: str) -> None:
    final = {
        "id": FINAL_ID,
        "memoryCitation": None,
        "phase": "final_answer",
        "text": "",
        "type": "agentMessage",
    }
    _item_notification(final, completed=False)
    _notify(
        "item/agentMessage/delta",
        {
            "delta": "done",
            "itemId": FINAL_ID,
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
        },
    )
    final["text"] = "done"
    _item_notification(final, completed=True)
    _notify("test/unknown", {"secret": "must-not-become-final-text"})
    _notify("turn/completed", {"threadId": THREAD_ID, "turn": _turn(status)})


def main() -> None:
    pid_file = os.environ.get("KSADK_TEST_CODEX_PID_FILE")
    request_log = os.environ.get("KSADK_TEST_CODEX_REQUEST_LOG")
    if pid_file:
        Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")

    blocked_turn = False
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        if request_log and method != "initialized":
            with Path(request_log).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(message) + "\n")
        if "id" not in message:
            continue

        request_id = message["id"]
        if method == "thread/resume" and message.get("params", {}).get(
            "threadId"
        ) == os.environ.get("KSADK_TEST_CODEX_REJECT_RESUME_ID"):
            _write(
                {
                    "id": request_id,
                    "error": {
                        "code": -32600,
                        "message": "no rollout found for thread id "
                        + str(message["params"]["threadId"]),
                    },
                }
            )
            continue
        if method == "initialize":
            result: dict[str, Any] = {
                "serverInfo": {"name": "ksadk-test-app-server", "version": "0.144.4"},
                "userAgent": "ksadk-test-app-server/0.144.4",
            }
        elif method in {"thread/start", "thread/resume"}:
            result = _thread_response()
        elif method == "turn/start":
            result = {"turn": _turn()}
        elif method == "turn/interrupt":
            result = {}
        else:
            _write({"id": request_id, "error": {"code": -32601, "message": str(method)}})
            continue

        _write({"id": request_id, "result": result})
        if method == "turn/start":
            prompt = json.dumps(message.get("params", {}).get("input", []))
            blocked_turn = "BLOCK" in prompt
            _start_turn(blocked_turn)
        elif method == "turn/interrupt" and blocked_turn:
            _notify(
                "item/autoApprovalReview/completed",
                {
                    "reviewId": REVIEW_ID,
                    "targetItemId": "tool_1",
                    "threadId": THREAD_ID,
                    "turnId": TURN_ID,
                    "decision": "cancelled",
                },
            )
            _finish_turn("interrupted")
            blocked_turn = False


if __name__ == "__main__":
    main()
