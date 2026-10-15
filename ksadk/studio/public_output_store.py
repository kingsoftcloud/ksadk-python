"""Run-scoped, bounded, redacted text artifacts; never raw tool receipts."""

import hashlib
import json
import re

from ksadk.harness.tool_display import display_text
from ksadk.studio.errors import StudioError


class PublicOutputStore:
    MAX_BYTES = 1_048_576
    MAX_RUN_BYTES = 8 * MAX_BYTES
    MAX_FILES = 64

    def __init__(self, workspace):
        self.workspace = workspace

    def _directory(self, run_id):
        identity = hashlib.sha256(run_id.encode()).hexdigest()
        return self.workspace.resolve(f".agentkit/assets/public-tool-output/{identity}")

    def retain(self, run_id, text, *, incomplete=False):
        # Sanitize a bounded prefix before persistence, including incomplete PEM
        # blocks. Never save the original string, args, URLs or filesystem path.
        bounded = text[: self.MAX_BYTES]
        public = display_text(bounded, self.MAX_BYTES).encode("utf-8")
        truncated = incomplete or len(text) > len(bounded) or len(public) > self.MAX_BYTES
        public = public[: self.MAX_BYTES].decode("utf-8", errors="ignore")
        body = json.dumps({"text": public, "truncated": truncated}, ensure_ascii=False)
        encoded = body.encode("utf-8")
        identity = hashlib.sha256(encoded).hexdigest()
        directory = self._directory(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        files = tuple(directory.glob("*.json"))
        target = directory / f"{identity}.json"
        if target.is_file() and not target.is_symlink():
            return identity
        if (
            len(files) >= self.MAX_FILES
            or sum(p.stat().st_size for p in files) + len(encoded) > self.MAX_RUN_BYTES
        ):
            return None
        self.workspace.atomic_write_text(target.relative_to(self.workspace.root), body)
        return identity

    def read(self, run_id, identity):
        missing = StudioError(
            "PUBLIC_OUTPUT_NOT_FOUND", "公开输出不存在或不可访问", status_code=404
        )
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise missing
        target = self._directory(run_id) / f"{identity}.json"
        if (
            target.is_symlink()
            or not target.is_file()
            or target.stat().st_size > self.MAX_BYTES * 6 + 128
        ):
            raise missing
        encoded = target.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != identity:
            raise missing
        try:
            data = json.loads(encoded)
            if not isinstance(data["text"], str) or type(data["truncated"]) is not bool:
                raise ValueError("invalid artifact")
        except (ValueError, KeyError, TypeError) as exc:
            raise missing from exc
        return data
