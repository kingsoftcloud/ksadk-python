#!/usr/bin/env python3
"""Patch @electron/osx-sign's bundled isbinaryfile to survive huge files.

isbinaryfile's protobuf sniffing allocates an array from an untrusted
varint length; a Python venv .so inside extraResources triggers
`RangeError: Invalid array length` and crashes electron-builder signing.
Wrap isBinaryProto in try/catch so a malformed read counts as "not binary"
(scanning continues with the remaining checks).

Run after `npm install` (electron-builder 26.x pins @electron/osx-sign,
which vendors its own copy of isbinaryfile). Idempotent.
"""

from __future__ import annotations

import sys
from pathlib import Path

PATCHED_MARKER = "/* patched-by: ksadk patch_isbinaryfile (RangeError guard) */"

TARGET_SNIPPETS = [
    "node_modules/@electron/osx-sign/node_modules/isbinaryfile/lib/index.js",
    "node_modules/isbinaryfile/lib/index.js",
]


def patch_file(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if PATCHED_MARKER in text:
        print(f"already patched: {path}")
        return True
    if "function isBinaryProto" not in text:
        print(f"isBinaryProto not found (upstream changed?): {path}")
        return False
    # Tolerate a previous manual patch that inserted the try without the
    # marker: if the function body already opens with a try, skip.
    probe = text[text.index("function isBinaryProto"):]
    probe = probe[: probe.index("}", probe.index("{") + 1) + 1]
    if "try {" in probe:
        print(f"already has try guard (manual patch): {path}")
        return True
    text = text.replace(
        "function isBinaryProto(fileBuffer, totalBytes) {",
        f"{PATCHED_MARKER}\nfunction isBinaryProto(fileBuffer, totalBytes) {{\n    try {{",
        1,
    )
    # The function ends with `return numMessages > 0;` followed by `}` —
    # close the try before that closing brace.
    old_tail = "    return numMessages > 0;\n}"
    if old_tail in text:
        text = text.replace(
            old_tail,
            "    return numMessages > 0;\n    } catch (error) {\n        return false;\n    }\n}",
            1,
        )
    else:
        print(f"WARNING: isBinaryProto tail not matched in {path}; skipping")
        return False
    path.write_text(text, encoding="utf-8")
    print(f"patched: {path}")
    return True


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    patched_any = False
    for snippet in TARGET_SNIPPETS:
        if patch_file(repo_root / snippet):
            patched_any = True
    if not patched_any:
        print("ERROR: no isbinaryfile copy was patched", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
