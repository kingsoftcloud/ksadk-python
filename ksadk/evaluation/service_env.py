"""Endpoint resolution for the agent-eval cloud Dataset transport."""

from __future__ import annotations

import os

from ksadk.common.aicp_env import DEFAULT_AICP_REGION, resolve_aicp_connection

AGENT_EVAL_BASE_URL_ENV = "AGENT_EVAL_BASE_URL"


def resolve_agent_eval_direct_url() -> str | None:
    """Return the explicit direct-HTTP override, if one was configured."""

    value = os.environ.get(AGENT_EVAL_BASE_URL_ENV, "").strip().rstrip("/")
    return value or None


def resolve_agent_eval_kop_connection() -> dict[str, str]:
    """Resolve the AICP origin used by the default signed KOP transport."""

    connection = resolve_aicp_connection("KSADK_AGENT_EVAL")
    return {
        "base_url": f"{connection['scheme']}://{connection['endpoint']}".rstrip("/"),
        # pre-online is a routing marker, not an AWS V4 signing region.
        "region": DEFAULT_AICP_REGION,
    }
