"""Read-only Agent directory for Teams; listing never starts a Build Kernel.

The application validates only selected bindings against the execution host
before creating/rebinding members. Directory readiness is not an execution
receipt and cannot authorize a dispatch.
"""

from __future__ import annotations

import asyncio
from typing import Any


class StudioTeamsCatalog:
    def __init__(self, studio: Any, *, authority_ref: str) -> None:
        self.studio = studio
        self.authority_ref = authority_ref
        self.cloud_error: str | None = None

    def _local(self) -> list[dict[str, Any]]:
        drafts = {draft.metadata.id: draft for draft in self.studio.drafts.list(limit=10000)}
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for build in self.studio.builds.list():
            seen.add(build.agent_id)
            draft = drafts.get(build.agent_id)
            status = getattr(build.status, "value", build.status)
            ready = status == "SUCCEEDED" and bool(build.artifact_path)
            provider = str(build.runtime_type or "")
            # Harness declares execution-policy support. Other providers must
            # explicitly expose it; ordinary chat support is insufficient.
            declared = build.runtime_lock.get("capabilities", {})
            policy = declared.get("execution_policy", {}) if isinstance(declared, dict) else {}
            supported = provider == "harness" or (
                isinstance(policy, dict) and policy.get("supported") is True
            )
            available = ready and supported
            reason = (
                "创建团队时将检查运行环境"
                if available
                else "该版本尚未构建成功，请先完成构建"
                if not ready
                else "该运行时尚未声明团队执行策略能力"
            )
            items.append(
                {
                    "bindingRef": f"local-build:{build.id}",
                    "providerRef": provider or "unknown",
                    "kind": "local_build",
                    "agentId": build.agent_id,
                    "buildId": build.id,
                    "authorityRef": self.authority_ref,
                    "tenantId": "local-studio",
                    "name": draft.metadata.name if draft else build.agent_id,
                    "description": draft.spec.description if draft else "",
                    "createdAt": build.created_at.isoformat(),
                    "pluginLockDigest": build.bundle_digest or build.resolved_digest,
                    "buildDigest": build.bundle_digest or build.resolved_digest,
                    "capabilities": {"enqueue": available, "leader": available},
                    "availability": {
                        "state": "unchecked" if available else "unavailable",
                        "code": "preflight_required"
                        if available
                        else ("build_required" if not ready else "teams_policy_unsupported"),
                        "reason": reason,
                        "action": "preflight" if available else "open_agent",
                    },
                }
            )
        for agent_id, draft in drafts.items():
            if agent_id not in seen:
                items.append(
                    {
                        "bindingRef": f"local-agent:{agent_id}",
                        "kind": "local_build",
                        "providerRef": "unbuilt",
                        "agentId": agent_id,
                        "authorityRef": self.authority_ref,
                        "tenantId": "local-studio",
                        "name": draft.metadata.name,
                        "description": draft.spec.description,
                        "capabilities": {"enqueue": False, "leader": False},
                        "availability": {
                            "state": "unavailable",
                            "code": "build_required",
                            "reason": "尚未构建，请先构建 Agent",
                            "action": "open_agent",
                        },
                    }
                )
        return items

    def node_advertisements(
        self, host_descriptors: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Advertise only fixed builds with explicit trusted Host descriptors.

        A provider name (including Harness) is not proof that this process has
        mounted grant expiry, policy enforcement or canonical event storage.
        Bootstrap/probe supplies descriptors; reading the directory starts no runtime.
        """
        from ksadk.studio.teams_node_v1 import normalized_node_bindings

        items = []
        for binding in self._local():
            descriptor = host_descriptors.get(binding["bindingRef"])
            if not descriptor or binding.get("buildId") is None:
                continue
            if descriptor.get("bundleDigest") != binding.get("buildDigest"):
                continue
            items.append(
                {
                    "localBindingRef": binding["bindingRef"],
                    "agentId": binding["agentId"],
                    "buildId": binding["buildId"],
                    "providerRef": binding["providerRef"],
                    "name": binding["name"],
                    "bundleDigest": descriptor["bundleDigest"],
                    "contractDigest": descriptor["contractDigest"],
                    "capabilitiesDigest": descriptor["capabilitiesDigest"],
                    "capabilities": descriptor.get("capabilities", {}),
                }
            )
        return normalized_node_bindings(items)

    async def list_bindings(self) -> list[dict[str, Any]]:
        items = self._local()
        gateway = getattr(getattr(self.studio, "cloud", None), "gateway", None)
        listing = getattr(gateway, "list_account_agents", None)
        self.cloud_error = None
        if not callable(listing):
            return items
        try:
            payload = await asyncio.wait_for(listing(page=1, size=100), timeout=5)
        except Exception:
            # Cloud failure never hides locally usable Agents. The status
            # facade can surface this code without leaking provider errors.
            self.cloud_error = "cloud_directory_unavailable"
            return items
        for agent in payload.get("items", []):
            agent_id = agent.get("agentId")
            if not agent_id:
                continue
            version_id = agent.get("versionId") or "unversioned"
            items.append(
                {
                    "bindingRef": f"cloud-agent:{agent_id}:{version_id}",
                    "providerRef": "agentengine",
                    "kind": "cloud",
                    "agentId": agent_id,
                    "versionId": agent.get("versionId"),
                    "authorityRef": self.authority_ref,
                    "tenantId": "local-studio",
                    "name": agent.get("name") or agent_id,
                    "description": "云端 Agent",
                    "createdAt": agent.get("updatedAt"),
                    "capabilities": {"enqueue": False, "leader": False},
                    "availability": {
                        "state": "unavailable",
                        "code": "server_authority_required",
                        "reason": "连接团队服务端并注册受控执行节点后可加入团队",
                        "action": "connect_teams_server",
                    },
                }
            )
        return items
