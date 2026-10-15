"""Select the legacy single-string reply from canonical A2A outputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def select_terminal_reply(
    output_refs: Sequence[Any],
    snapshots: Mapping[tuple[str, str], Mapping[str, str]],
    item_kinds: Mapping[tuple[str, str], str],
) -> str:
    """Return one authoritative text item without flattening conversation history.

    Canonical output refs deliberately retain every terminal artifact/message for
    replay and presentation.  The legacy tool envelope, however, has only one
    ``reply`` string.  Prefer the last text artifact (the A2A task result), and
    only fall back to the last agent message for peers that return no artifact.
    Parts within that selected item retain their declared order.
    """

    selected: dict[tuple[str, str], list[str]] = {}
    ordered: dict[str, list[tuple[str, str]]] = {"artifact": [], "message": []}
    for ref in output_refs:
        key = (str(ref.scope_id), str(ref.item_id))
        kind = item_kinds.get(key)
        parts = snapshots.get(key)
        if kind not in ordered or not parts:
            continue
        if key not in selected:
            selected[key] = []
            ordered[kind].append(key)
        if ref.part_id is None:
            values = parts.values()
        else:
            value = parts.get(str(ref.part_id))
            values = () if value is None else (value,)
        for value in values:
            if value:
                selected[key].append(value)

    candidates = ordered["artifact"] or ordered["message"]
    if not candidates:
        return ""
    return "".join(selected[candidates[-1]])


__all__ = ["select_terminal_reply"]
