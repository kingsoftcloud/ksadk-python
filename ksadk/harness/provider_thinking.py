"""Append-only display of an explicitly selected provider channel."""

from difflib import SequenceMatcher
from time import monotonic

from ksadk.harness.tool_display import display_text


class ProviderThinkingBuffer:
    # Keep the display separate from the unmodified provider replay. A small
    # trailing overlap prevents token/chunk boundaries exposing partial secrets
    # without requiring a newline (many models emit one long reasoning line).
    OVERLAP = 80
    MIN_UPDATE_INTERVAL = 0.1
    BATCH_CHARS = 1024

    def __init__(self):
        self.raw = ""
        self.emitted = ""
        self._safe_prefix = ""
        self.processed_chars = 0
        self.updated_at = None

    def push(self, delta: str, *, final=False) -> str:
        # ZCode model.ts retains the full returned reasoning block. The request
        # output budget bounds generation; a separate display character cap must
        # not freeze the latest preview while that generation continues.
        self.raw += delta
        now = monotonic()
        if (
            not final
            and self.updated_at is not None
            and (
                len(self.raw) - self.processed_chars < self.BATCH_CHARS
                and now - self.updated_at < self.MIN_UPDATE_INTERVAL
            )
        ):
            return ""
        self.updated_at = now
        self.processed_chars = len(self.raw)
        safe = display_text(self.raw, len(self.raw))
        boundary = len(safe) if final else max(0, len(safe) - self.OVERLAP)
        if not final:
            # Do not publish a partial word, URL or key marker.
            while boundary > 0 and not safe[boundary - 1].isspace():
                boundary -= 1
        prefix = safe[:boundary]
        previous = self._safe_prefix
        if prefix.startswith(previous):
            new = prefix[len(previous):]
        else:
            # Redaction can revise a previously sanitized marker (e.g. a key
            # block becoming complete). Align bounded safe tails, never raw
            # provider bytes, so subsequent progress does not freeze forever.
            old_tail, new_tail = previous[-2048:], prefix[-4096:]
            matches = SequenceMatcher(
                None, old_tail, new_tail, autojunk=False
            ).get_matching_blocks()
            end = max((match.b + match.size for match in matches if match.size), default=0)
            new = new_tail[end:] if end else "\n" + new_tail
        self._safe_prefix = prefix
        self.emitted += new
        return new
