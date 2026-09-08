#!/usr/bin/env python3
"""Fake codex app-server that emits a real item/commandExecution/requestApproval."""
import json, os, sys, time, threading

THREAD_ID = "019f0000-0000-7000-8000-0000000000aa"
TURN_ID = "019f0000-0000-7000-8000-0000000000bb"
APPROVAL_REQ_ID = 9001
ITEM_CMD = "tool_cmd_1"

def _write(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()

def _notify(method, params):
    _write({"method": method, "params": params})

def _item(item, completed, **p):
    key = "completedAtMs" if completed else "startedAtMs"
    _notify("item/completed" if completed else "item/started",
            {key: int(time.time()*1000), "item": item,
             "threadId": THREAD_ID, "turnId": TURN_ID, **p})

def _turn(status="inProgress"):
    return {"id": TURN_ID, "status": status, "items": [], "startedAt": int(time.time()),
            "completedAt": None, "durationMs": None, "error": None, "itemsView": "full"}

def _thread():
    now = int(time.time())
    return {"id": THREAD_ID, "createdAt": now, "updatedAt": now, "status": {"type": "idle"},
            "sessionId": THREAD_ID, "ephemeral": True, "source": "vscode", "turns": [],
            "cwd": os.getcwd(), "name": None, "path": None, "preview": "",
            "modelProvider": "test", "agentNickname": None, "agentRole": None,
            "cliVersion": "0.144.4-test", "forkedFromId": None, "gitInfo": None,
            "threadSource": None, "parentThreadId": None, "recencyAt": now}

def _start_turn():
    _notify("turn/started", {"threadId": THREAD_ID, "turn": _turn()})
    user = {"id": "user_1", "type": "userMessage", "clientId": None,
            "content": [{"text": "run uname -a", "textElements": [], "type": "text"}]}
    _item(user, False); _item(user, True)
    cmd = {"id": ITEM_CMD, "type": "commandExecution", "command": "uname -a", "cwd": "/app/code",
           "status": "inProgress", "exitCode": None, "aggregatedOutput": "", "executable": "uname", "cwd": "/app/code"}
    _item(cmd, False)
    # real JSON-RPC server request; response arrives later on stdin
    _write({"id": APPROVAL_REQ_ID, "method": "item/commandExecution/requestApproval",
            "params": {"threadId": THREAD_ID, "turnId": TURN_ID, "itemId": ITEM_CMD,
                       "kind": "command", "command": "uname -a",
                       "cwd": os.getcwd()}})
    # keep reading; on approval response finish the turn

def _finish_turn(decision):
    cmd = {"id": ITEM_CMD, "type": "commandExecution", "command": "uname -a", "cwd": "/app/code",
           "status": "completed", "exitCode": 0,
           "aggregatedOutput": "Linux fake 6.1 #1 SMP x86_64 GNU/Linux", "executable": "uname", "cwd": "/app/code"}
    _item(cmd, True)
    final = {"id": "msg_final", "type": "agentMessage", "phase": "final_answer",
             "text": "Linux fake 6.1 #1 SMP x86_64 GNU/Linux", "memoryCitation": None}
    _item(final, False); _item(final, True)
    t = _turn("completed"); t["completedAt"] = int(time.time())
    _notify("turn/completed", {"threadId": THREAD_ID, "turn": t})

def main():
    requested = False
    for line in sys.stdin:
        msg = json.loads(line)
        method = msg.get("method")
        if "id" not in msg:
            continue
        rid = msg["id"]
        if method == "initialize":
            result = {"serverInfo": {"name": "ksadk-fake-approval", "version": "0.144.4"},
                      "userAgent": "ksadk-fake-approval"}
            _write({"id": rid, "result": result})
        elif method in ("thread/start", "thread/resume"):
            _write({"id": rid, "result": {
                "approvalPolicy": "on-request", "approvalsReviewer": "user",
                "cwd": os.getcwd(), "instructionSources": [], "model": "fake-model",
                "modelProvider": "test", "reasoningEffort": "low",
                "sandbox": {"networkAccess": False, "type": "readOnly"},
                "serviceTier": None, "thread": _thread()}})
        elif method == "turn/start":
            _write({"id": rid, "result": {"turn": _turn()}})
            _start_turn(); requested = True
        elif method == "turn/interrupt":
            _write({"id": rid, "result": {}})
        else:
            _write({"id": rid, "result": {}})
        # a response to our approval request?
        if requested and "result" in msg and msg.get("id") == APPROVAL_REQ_ID:
            decision = (msg.get("result") or {}).get("decision")
            _finish_turn(decision)
            requested = False

if __name__ == "__main__":
    main()
