"""Conservative intent boundary for questions about the active Skill binding.

This is only a tool-scope decision. It never claims that a Skill exists on the
network or that reading an unrelated workspace can change the locked Build.
"""

from __future__ import annotations

import re

# A shortcut removes tools and may replace the model with a host fact. Its
# admission therefore needs positive evidence for the *whole* request, not
# an availability substring plus an ever-growing list of excluded verbs.
# Unknown wording goes through the normal, already-authorized Agent loop.
_NAME = r'(?:`[^`\r\n]{1,80}`|“[^”\r\n]{1,80}”|"[^"\r\n]{1,80}"|[a-z0-9_][a-z0-9_.@:/+\-]{0,79})'
_KIND = r"(?:skills?|plugins?|技能|插件)"
_OWNER = r"(?:(?:当前|这个|此)\s*(?:agent|智能体)\s*)?"
_INVENTORY_QUERIES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"{_OWNER}(?:有|有没有|是否有)(?:安装\s*)?\s*"
        rf"(?:{_NAME}\s*(?:这个|这项)?\s*)?{_KIND}\s*(?:吗|么|呢)?",
        rf"{_OWNER}(?:有哪些|有什么|哪些)(?:已绑定(?:的)?|可用(?:的)?)?\s*{_KIND}",
        rf"{_OWNER}(?:(?:这个|此)\s*|{_NAME}\s*)?{_KIND}"
        rf"\s*(?:有吗|有没有|是否有|已绑定|绑定了吗|可用吗)",
        rf"{_OWNER}(?:已绑定(?:的)?|可用(?:的)?)\s*{_KIND}\s*(?:有哪些|有什么)",
        rf"(?:is|are)\s+(?:the\s+)?(?:{_NAME}\s+)?{_KIND}\s+(?:bound|available)"
        rf"(?:\s+(?:to|for)\s+(?:this|the|current)\s+agent)?",
        rf"(?:which|what)\s+{_KIND}\s+(?:are\s+)?(?:bound|available)"
        rf"(?:\s+(?:to|for)\s+(?:this|the|current)\s+agent)?",
    )
)


def is_skill_binding_question(goal: str) -> bool:
    """Recognize a short, standalone question about this Build's bindings.

    Mixed requests and requests to discover, install, inspect, or use a Skill
    remain ordinary Agent work with their normal authorized tool surface.
    """

    text = goal.strip().rstrip("?？。.!！").strip()
    return bool(
        text
        and len(text) <= 160
        and any(pattern.fullmatch(text) for pattern in _INVENTORY_QUERIES)
    )
