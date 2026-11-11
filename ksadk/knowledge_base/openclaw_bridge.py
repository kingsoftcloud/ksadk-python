"""OpenClaw knowledge base bridge.

Called by the openclaw-ksadk-knowledge JS extension via subprocess.
Reads a JSON payload from stdin, performs the knowledge base search using
KsADK's KnowledgeBaseService, and writes a JSON response to stdout.

This module mirrors the pattern used by ksadk.memory.openclaw_bridge.
"""

from __future__ import annotations

import json
import logging
import sys

logger = logging.getLogger(__name__)


def _search(payload: dict) -> dict:
    query = str(payload.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "EMPTY_QUERY"}

    top_k_raw = payload.get("top_k")
    top_k = int(top_k_raw) if isinstance(top_k_raw, (int, float)) and top_k_raw > 0 else None

    try:
        from ksadk.knowledge_base.service import KnowledgeBaseService, format_knowledge_results

        service = KnowledgeBaseService.from_env()
        results = service.search(query, top_k=top_k)
        text = format_knowledge_results(results)
        return {"ok": True, "result": text, "count": len(results)}
    except Exception as exc:
        logger.error("openclaw_knowledge_bridge search failed: %s", type(exc).__name__)
        return {"ok": False, "error": type(exc).__name__}


def main() -> None:
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        print(json.dumps({"ok": False, "error": "INVALID_JSON"}))
        sys.exit(1)

    operation = str(payload.get("operation") or "").strip()
    if operation == "search":
        response = _search(payload)
    else:
        response = {"ok": False, "error": f"UNKNOWN_OPERATION: {operation}"}

    print(json.dumps(response, ensure_ascii=False))
    sys.exit(0 if response.get("ok") else 1)


if __name__ == "__main__":
    main()
