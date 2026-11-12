"""JSON bridge used by the bundled OpenClaw Ksmem adapter.

The bridge deliberately contains no Ksmem transport logic. Every operation is
delegated to :class:`LongTermMemoryService`, keeping credentials, SDK behavior,
and storage semantics inside KsADK.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import Any, TextIO

from ksadk.memory.models import UnsupportedMemoryOperation
from ksadk.memory.service import LongTermMemoryService


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"'{key}' is required")
    return value


def _record_payload(record: Any) -> dict[str, Any]:
    if hasattr(record, "__dataclass_fields__"):
        data = asdict(record)
    else:
        data = dict(record)
    return {
        "id": str(data.get("memory_id") or data.get("id") or ""),
        "memory": str(data.get("content") or data.get("memory") or ""),
        "score": data.get("score"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "metadata": data.get("metadata") or {},
    }


def handle_request(
    payload: dict[str, Any],
    *,
    service: LongTermMemoryService | None = None,
) -> dict[str, Any]:
    operation = _required_text(payload, "operation").lower()
    user_id = _required_text(payload, "user_id")
    memory = service or LongTermMemoryService.from_env(app_name="openclaw")

    if operation == "search":
        query = _required_text(payload, "query")
        top_k = int(payload.get("top_k") or memory.top_k)
        try:
            records = memory.search_records(user_id=user_id, query=query, top_k=top_k)
            results = [_record_payload(record) for record in records]
        except UnsupportedMemoryOperation:
            results = [
                {"id": "", "memory": entry, "score": None, "metadata": {}}
                for entry in memory.search_entries(
                    user_id=user_id, query=query, top_k=top_k
                )
            ]
        if memory.last_error:
            raise RuntimeError("Ksmem search failed")
        return {"ok": True, "memories": results}

    if operation == "add":
        content = _required_text(payload, "content")
        accepted = memory.save_text(
            user_id=user_id,
            content=content,
            metadata={"source": "OPENCLAW"},
            session_id=str(payload.get("session_id") or "").strip() or None,
            flush=True,
        )
        if not accepted:
            raise RuntimeError("Ksmem write failed")
        return {"ok": True, "accepted": True}

    if operation == "update":
        result = memory.update_memory(
            user_id=user_id,
            memory_id=_required_text(payload, "memory_id"),
            content=_required_text(payload, "content"),
        )
        return {"ok": result.ok, "result": asdict(result)}

    if operation == "delete":
        result = memory.delete_memory(
            user_id=user_id,
            memory_id=_required_text(payload, "memory_id"),
        )
        return {"ok": result.ok, "result": asdict(result)}

    raise ValueError(f"Unsupported operation: {operation}")


def main(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    try:
        raw = stdin.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("request must be a JSON object")
        response = handle_request(payload)
        status = 0
    except Exception as exc:
        response = {"ok": False, "error": type(exc).__name__}
        status = 1
    stdout.write(json.dumps(response, ensure_ascii=False))
    stdout.write("\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
