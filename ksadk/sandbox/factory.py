from __future__ import annotations

import os
from typing import Any

from ksadk.sandbox.backends.e2b import E2BSandboxBackend
from ksadk.sandbox.backends.local_process import LocalProcessSandboxBackend
from ksadk.sandbox.base import SandboxBackend, SandboxError, SandboxSpec, SandboxType
from ksadk.sessions.local_service import resolve_local_session_dir


def bool_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_sandbox_api_url() -> str:
    """Probe sandbox control-plane URL at runtime (in-Pod).

    agentengine-server injects public E2B_API_URL on the management cluster.
    public_only compute pods can reach the public address directly; private_only
    pods have no public egress. Strategy: try the original (public) URL first;
    if reachable keep it, otherwise fall back to the 198 public-service-net
    internal address. Mirrors KSPMAS get_kspmas_api_base(): probe runs in-Pod.
    """
    raw = (os.environ.get("E2B_API_URL") or "").strip()
    if not raw:
        return raw

    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if host.endswith(".sdns.ksyun.com"):
        return raw
    if not host.endswith(".sandbox.ksyun.com"):
        return raw

    parts = host.split(".")
    region = ""
    if len(parts) >= 4 and parts[0] == "mgr":
        region = parts[1]
    elif len(parts) >= 4:
        region = parts[1] if parts[1] != "sandbox" else ""
    if not region:
        return raw

    import socket

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # 1) Try the original (public) URL first; reachable => keep it.
    try:
        s = socket.socket()
        s.settimeout(1.5)
        s.connect((host, port))
        s.close()
        return raw
    except OSError:
        pass

    # 2) Public unreachable (e.g. private_only node) => fall back to internal.
    internal_host = f"sandbox.{region}.sandbox.sdns.ksyun.com"
    try:
        s = socket.socket()
        s.settimeout(1.5)
        s.connect((internal_host, port))
        s.close()
    except OSError:
        return raw

    internal_url = urlunsplit((parsed.scheme or "https", internal_host, parsed.path, parsed.query, ""))
    return internal_url


def setup_sandbox_api_url_if_needed() -> None:
    """Override E2B_API_URL to internal address before creating E2B backend.

    Only runs in managed runtime (AGENT_RUNTIME_ID / K8S); local dev unaffected.
    """
    if not os.environ.get("AGENT_RUNTIME_ID") and not os.environ.get("KUBERNETES_SERVICE_HOST"):
        return
    resolved = _resolve_sandbox_api_url()
    if resolved != (os.environ.get("E2B_API_URL") or ""):
        os.environ["E2B_API_URL"] = resolved


def create_sandbox_backend(
    backend: str | None = None,
    *,
    sandbox_cls: Any | None = None,
) -> SandboxBackend:
    setup_sandbox_api_url_if_needed()
    resolved = (backend or os.environ.get("KSADK_SANDBOX_BACKEND") or "e2b").strip().lower()
    if resolved in {"local", "local_process"}:
        return LocalProcessSandboxBackend(
            workspace_root=resolve_local_session_dir() / "workspace", backend_name="local_process"
        )
    if resolved in {"pod", "pod_process"}:
        if not bool_env("KSADK_ALLOW_POD_PROCESS_TOOLS", False):
            raise SandboxError(
                "KSADK_ALLOW_POD_PROCESS_TOOLS=true is required for pod_process backend"
            )
        return LocalProcessSandboxBackend(
            workspace_root=resolve_local_session_dir() / "workspace", backend_name="pod_process"
        )
    if resolved != "e2b":
        raise SandboxError(f"Unsupported sandbox backend: {resolved}")
    return E2BSandboxBackend(spec=sandbox_spec_from_env(), sandbox_cls=sandbox_cls)


def sandbox_spec_from_env() -> SandboxSpec:
    template_id = (
        os.environ.get("KSADK_SANDBOX_TEMPLATE_ID")
        or os.environ.get("KSADK_SKILL_RUNTIME_TEMPLATE_ID")
        or ""
    )
    timeout = int(
        os.environ.get("KSADK_SANDBOX_TIMEOUT")
        or os.environ.get("KSADK_SKILL_RUNTIME_TIMEOUT")
        or "900"
    )
    allow_internet_access = bool_env(
        "KSADK_SANDBOX_ALLOW_INTERNET_ACCESS",
        bool_env("KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS", True),
    )
    sandbox_type = SandboxType.from_value(os.environ.get("KSADK_SANDBOX_TYPE", "aio"))
    return SandboxSpec(
        template_id=template_id,
        sandbox_type=sandbox_type,
        timeout=timeout,
        allow_internet_access=allow_internet_access,
    )
