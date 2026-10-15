"""G6 RPC 模式（总案 §6.2）：stdin/stdout JSONL 供 IDE/宿主嵌入。

分帧规则（照抄 pi 踩过的坑，G8 §10.3）：
- **LF-only 分帧**：每一行（``\\n``）是一个完整 JSON 对象；写入端不得出现
  裸 ``\\r``；
- **U+2028/U+2029 不是帧边界**：这两个字符可原样出现在 JSON 字符串内部，
  宿主必须只按 ``\\n`` 切帧（JSONL 解析器的自然行为，显式成文防踩坑）；
- non_exhaustive 演进承诺（G8）：``op`` 与 ``event`` 词汇表只增不改不删；
  宿主必须忽略未知的 op/event——新增能力不破坏旧宿主。完整词汇表与版本化
  承诺见 ``docs/harness-rpc-protocol.md``。

协议（宿主 → 引擎）::

    {"op": "prompt", "request_id": "r1", "text": "...", "session_id": "s", "user_id": "u"}
    {"op": "steer", "run_id": "...", "text": "..."}
    {"op": "follow_up", "run_id": "...", "text": "..."}
    {"op": "abort", "run_id": "..."}
    {"op": "interrupt-response", "run_id": "...", "decision": "approved"}
    {"op": "fork", "source_session_id": "...", "from_entry_id": null, "new_session_id": "..."}

引擎 → 宿主::

    {"ready": true, "protocol": "ksadk.harness.rpc/v1"}
    {"response": {"request_id": "r1", "op": "prompt", "ok": true, "run_id": "..."}}
    {"event": {"event_type": "...", "payload": {...}, ...}}
    {"response": {..., "ok": false, "error": "..."}}   # 失败可见，不静默吞
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from ksadk.harness.sdk import HarnessSession, create_harness_session
from ksadk.harness.spec import HarnessSpec

PROTOCOL_VERSION = "ksadk.harness.rpc/v1"

#: 已知 op 词汇（non_exhaustive：宿主发未知 op 得到可见错误响应，不崩）。
KNOWN_OPS = frozenset(
    {"prompt", "steer", "follow_up", "abort", "interrupt-response", "fork"}
)


class HarnessRpcServer:
    """JSONL RPC 服务核（stdin → op，stdout → event/response）。可注入 IO 测试。"""

    def __init__(self, session: HarnessSession) -> None:
        self._session = session
        self._tasks: set[asyncio.Task[Any]] = set()
        self._writer: TextIO | None = None

    async def serve(self, *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        """逐行读 op、异步执行、事件流并发写出。stdin EOF 即退出。"""
        reader = stdin if stdin is not None else sys.stdin
        writer = stdout if stdout is not None else sys.stdout
        self._writer = writer
        self._emit(writer, {"ready": True, "protocol": PROTOCOL_VERSION})
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, reader.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                self._emit(writer, {"error": f"invalid json frame: {exc}"})
                continue
            await self._handle(request, writer)

    # ------------------------------------------------------------ op 分派

    async def _handle(self, request: Any, writer: TextIO) -> None:
        if not isinstance(request, dict) or "op" not in request:
            self._emit(writer, {"error": "frame must be an object with an 'op' key"})
            return
        op = str(request.get("op") or "")
        request_id = request.get("request_id")
        if op not in KNOWN_OPS:
            # non_exhaustive 契约的反向面：未知 op 可见失败，旧宿主可忽略。
            self._emit(
                writer,
                {"response": {"request_id": request_id, "op": op, "ok": False, "error": "unknown op"}},
            )
            return
        try:
            result = await self._dispatch(op, request)
            self._emit(
                writer,
                {"response": {"request_id": request_id, "op": op, "ok": True, **result}},
            )
        except Exception as exc:  # noqa: BLE001 - 失败可见，不断开宿主
            self._emit(
                writer,
                {"response": {"request_id": request_id, "op": op, "ok": False, "error": str(exc)[:512]}},
            )

    async def _dispatch(self, op: str, request: dict[str, Any]) -> dict[str, Any]:
        if op == "prompt":
            handle = await self._session.prompt(
                str(request.get("text") or ""),
                user_id=str(request.get("user_id") or "local"),
                session_id=str(request.get("session_id") or "default"),
                metadata=request.get("metadata") if isinstance(request.get("metadata"), dict) else None,
            )
            self._pump_events(handle)
            return {"run_id": handle.run_id}
        if op == "steer":
            receipt = await self._session.send(_handle_of(request), str(request.get("text") or ""))
            return {"status": receipt.status, "mode": receipt.mode, "reason": receipt.reason}
        if op == "follow_up":
            receipt = await self._session.follow_up(_handle_of(request), str(request.get("text") or ""))
            return {"status": receipt.status, "mode": receipt.mode, "reason": receipt.reason}
        if op == "abort":
            result = await self._session.cancel(_handle_of(request))
            return {"cancel": str(result)}
        if op == "interrupt-response":
            handle = await self._session.resume_approval(
                _handle_of(request), str(request.get("decision") or "")
            )
            return {"run_id": handle.run_id}
        if op == "fork":
            result = self._session.fork(
                source_session_id=str(request.get("source_session_id") or ""),
                from_entry_id=request.get("from_entry_id"),
                new_session_id=str(request.get("new_session_id") or ""),
            )
            return {
                "new_session_id": result.new_session_id,
                "fork_root_entry_id": result.fork_root_entry_id,
            }
        raise RuntimeError(f"unhandled op: {op}")  # pragma: no cover - KNOWN_OPS 守卫

    # ------------------------------------------------------------ 事件泵

    def _pump_events(self, handle: Any) -> None:
        """为 Run 起后台事件泵；Run 终态后自动退出。"""
        task = asyncio.get_running_loop().create_task(self._drain(handle))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drain(self, handle: Any) -> None:
        writer = self._writer or sys.stdout
        async for event in self._session.events(handle):
            payload = event.to_v2() if hasattr(event, "to_v2") else event
            self._emit(
                writer,
                {"event": payload.model_dump(mode="json") if hasattr(payload, "model_dump") else str(payload)},
            )

    @staticmethod
    def _emit(writer: TextIO, payload: dict[str, Any]) -> None:
        # LF-only 分帧（§6.2）：U+2028/U+2029 转义为 JSON 合法的 \u2028 形态，
        # 保证即使宿主误把这两个字符当行界也不会撕裂帧（pi 踩过的坑）。
        text = json.dumps(payload, ensure_ascii=False)
        text = text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        writer.write(text + "\n")
        writer.flush()


def _handle_of(request: dict[str, Any]):
    from ksadk.runtime import RunHandle

    return RunHandle(
        run_id=str(request.get("run_id") or ""),
        session_id=str(request.get("session_id") or ""),
        runtime_type="harness",
        native_ref={},
    )


def load_spec(spec_path: str | None) -> HarnessSpec:
    """从 JSON 文件加载 HarnessSpec（ ``HarnessSpec.model_dump_json`` 产物）。"""
    if not spec_path:
        raise RuntimeError(
            "RPC 模式需要 HarnessSpec JSON：作为 argv[1] 传入，或设 KSADK_HARNESS_SPEC"
        )
    return HarnessSpec.model_validate_json(Path(spec_path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> None:
    import os

    args = list(sys.argv[1:] if argv is None else argv)
    spec = load_spec(args[0] if args else os.environ.get("KSADK_HARNESS_SPEC"))
    session = create_harness_session(spec)
    asyncio.run(HarnessRpcServer(session).serve())


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["KNOWN_OPS", "PROTOCOL_VERSION", "HarnessRpcServer", "load_spec", "main"]
