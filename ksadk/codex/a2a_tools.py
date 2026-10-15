"""Synchronous app-server callbacks bridged into the owning async turn stream."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

from ksadk.a2a.codex import CodexToolCall, compile_codex_experts


def failed_tool_result(code):
    return {"success": False, "contentItems": [{"type": "inputText", "text": json.dumps({"ok": False, "error_type": code})}]}


def denied_tool_result():
    # The tool protocol completed successfully; the requested remote action did not.
    # A generic failed tool response makes Codex retry the same denied action.
    return {
        "success": True,
        "contentItems": [{
            "type": "inputText",
            "text": json.dumps({
                "ok": False,
                "state": "denied",
                "error_type": "A2A_APPROVAL_DENIED",
                "reply": "用户已拒绝本轮远端 Agent 调用。请直接告知未执行，不要再次调用工具。",
            }, ensure_ascii=False),
        }],
    }


@dataclass
class PendingExpertCall:
    call: CodexToolCall
    completed: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None


class CodexExpertBridge:
    def __init__(self, specs):
        self.experts = {expert.spec.local_name: expert for expert in compile_codex_experts(specs)}
        self._threads = set()
        self._queues = {}
        self._pending = {}
        self._denied_turns = set()
        self._lock = threading.Lock()
        self._closed = False

    def declarations(self):
        return [expert.tool_spec() for expert in self.experts.values()]

    def register_thread(self, thread_id):
        with self._lock:
            if self._closed or not thread_id:
                raise ValueError("A2A_CODEX_THREAD_UNAVAILABLE")
            self._threads.add(thread_id)

    def activate(self, thread_id, output_queue):
        with self._lock:
            if self._closed or thread_id not in self._threads:
                raise ValueError("A2A_CODEX_THREAD_UNAVAILABLE")
            if thread_id in self._queues:
                raise ValueError("A2A_CODEX_CONCURRENT_TURN_UNSUPPORTED")
            self._queues[thread_id] = output_queue

    @staticmethod
    def key(call):
        return call.thread_id, call.turn_id, call.call_id

    def request(self, params):
        try:
            call = CodexToolCall.model_validate(params)
        except Exception:
            return failed_tool_result("A2A_CODEX_TOOL_REQUEST_INVALID")
        key = self.key(call)
        with self._lock:
            if (call.thread_id, call.turn_id) in self._denied_turns:
                return denied_tool_result()
            output = self._queues.get(call.thread_id)
            expert = self.experts.get(call.tool)
            if self._closed or output is None or expert is None:
                return failed_tool_result("A2A_CODEX_TOOL_NOT_REGISTERED")
            pending = self._pending.get(key)
            if pending is not None and pending.call != call:
                return failed_tool_result("A2A_CODEX_TOOL_CALL_CONFLICT")
            if pending is None:
                pending = PendingExpertCall(call)
                self._pending[key] = pending
                output.put({"method": "ksadk/a2a/toolCall", "params": call.model_dump(by_alias=True)})
        # A Kernel-owned handler completes this request. Timeout does not claim
        # remote cancellation; notify that handler so it can cancel honestly.
        if not pending.completed.wait(expert.spec.timeout_seconds or 300.0):
            with self._lock:
                if not pending.completed.is_set():
                    pending.response = failed_tool_result("A2A_CODEX_TOOL_TIMEOUT")
                    pending.completed.set()
                    output.put({"method": "ksadk/a2a/toolCancel", "params": call.model_dump(by_alias=True)})
        return pending.response or failed_tool_result("A2A_CODEX_TOOL_CLOSED")

    def mark_denied(self, call):
        call = CodexToolCall.model_validate(call)
        with self._lock:
            self._denied_turns.add((call.thread_id, call.turn_id))

    def resolve(self, call, response):
        call = CodexToolCall.model_validate(call)
        # Do not permit arbitrary data to escape the fixed expert wire contract.
        if not isinstance(response, dict) or set(response) != {"success", "contentItems"}:
            raise ValueError("A2A_CODEX_TOOL_RESPONSE_INVALID")
        if not isinstance(response["success"], bool) or not isinstance(response["contentItems"], list):
            raise ValueError("A2A_CODEX_TOOL_RESPONSE_INVALID")
        for item in response["contentItems"]:
            if not isinstance(item, dict) or set(item) != {"type", "text"} or item["type"] != "inputText" or not isinstance(item["text"], str):
                raise ValueError("A2A_CODEX_TOOL_RESPONSE_INVALID")
        with self._lock:
            pending = self._pending.get(self.key(call))
            if pending is None or pending.call != call or pending.completed.is_set():
                return False
            pending.response = json.loads(json.dumps(response))
            pending.completed.set()
            return True

    def deactivate(self, thread_id):
        with self._lock:
            self._queues.pop(thread_id, None)
            self._denied_turns = {
                key for key in self._denied_turns if key[0] != thread_id
            }
            for key in [key for key in self._pending if key[0] == thread_id]:
                pending = self._pending.pop(key)
                if not pending.completed.is_set():
                    pending.response = failed_tool_result("A2A_CODEX_TOOL_CLOSED")
                    pending.completed.set()

    def close(self):
        with self._lock:
            self._closed = True
            active = list(self._queues)
        for thread_id in active:
            self.deactivate(thread_id)
