"""A2A tool risk declaration, usable without the managed database stack."""

from ksadk.tools.gateway import ToolPolicy

A2A_REMOTE_TOOL_POLICY = ToolPolicy(risk_level="high", side_effects=("a2a_remote_call",))
