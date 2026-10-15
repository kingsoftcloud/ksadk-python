"""Bounded public summary lines, separate from private provider reasoning."""

from ksadk.harness.tool_display import display_text


class PublicSummaryBuffer:
    LIMIT = 16384

    def __init__(self):
        self.text = ""
        self.consumed = 0
        self.private_key = False

    def drain(self, text: str, *, final=False) -> str:
        # Only validated, explicit summary parts enter here. Buffer complete
        # lines so a credential split across provider chunks is never disclosed.
        bounded = text[: self.LIMIT]
        if not bounded.startswith(self.text):
            return ""  # Rewrites cannot replace already emitted public text.
        self.text = bounded
        boundary = len(bounded) if final else bounded.rfind("\n") + 1
        if boundary <= self.consumed:
            return ""
        lines = bounded[self.consumed : boundary].splitlines(keepends=True)
        public = []
        for line in lines:
            if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
                self.private_key = "-----END " not in line
                public.append("[redacted key]\n")
            elif self.private_key:
                if "-----END " in line and "PRIVATE KEY-----" in line:
                    self.private_key = False
            else:
                public.append(display_text(line, self.LIMIT))
        self.consumed = boundary
        return "".join(public)
