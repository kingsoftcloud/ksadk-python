"""Present the terminal Native verifier separately from the model candidate.

The candidate was written before the verifier ran. At a repair budget limit it
may describe an older checkpoint, so the host appends only whitelisted final
facts from its committed verification event. This never changes the verifier
outcome or upgrades an unverified run to success.
"""

from __future__ import annotations

import re
from typing import Any

_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_STATUS = {"passed": "通过", "failed": "失败", "not_run": "未验证", "blocked": "受阻"}


def terminal_verification_notice(metadata: dict[str, Any]) -> str:
    """Correct failed checks and exhausted repairs using only host terminal facts."""
    if metadata.get("engine_kind") != "native":
        return ""
    exhausted = (
        metadata.get("reason") == "repair_budget_exhausted"
        and metadata.get("outcome") in {"unverified", "partial", "blocked"}
    )
    raw = metadata.get("verification_items")
    if not isinstance(raw, list):
        return ""
    failures: list[tuple[str, str]] = []
    other_items: list[tuple[str, str]] = []
    seen: set[str] = set()
    current_goal_status: str | None = None
    omitted = False
    for row in raw:
        if not isinstance(row, dict):
            continue
        name, status = row.get("acceptance_id"), row.get("status")
        if (
            not isinstance(name, str)
            or _SAFE_ID.fullmatch(name) is None
            or status not in _STATUS
            or name in seen
        ):
            continue
        seen.add(name)
        if name == "current_user_goal":
            current_goal_status = status
            continue
        bucket = failures if status in {"failed", "blocked"} else other_items
        if len(bucket) < 128:
            bucket.append((name, status))
        else:
            omitted = True
    items = (failures + other_items)[:128]
    omitted = omitted or len(failures) + len(other_items) > len(items)
    if not failures and (
        current_goal_status in {"failed", "blocked"}
        or exhausted and current_goal_status == "not_run"
    ):
        items = [("current_user_goal", current_goal_status), *items][:128]
    if not failures and current_goal_status not in {"failed", "blocked"} and not exhausted:
        return ""
    if not items:
        return ""
    listed = "；".join(f"`{name}`：{_STATUS[status]}" for name, status in items)
    return (
        "\n\n---\n**宿主最终验收（以此为准）**："
        + listed
        + ("；其余验收项未展示" if omitted else "")
        + ("。验收修正次数已用尽，任务仍未通过完整验收" if exhausted else "")
        + "。上方模型回答可能引用了较早的检查点；本轮未通过完整验收。"
    )


__all__ = ["terminal_verification_notice"]
