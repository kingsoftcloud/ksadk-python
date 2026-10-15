from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from ksadk.studio.errors import StudioError
from ksadk.tools.gateway import ToolPolicy, default_tool_gateway
from ksadk.toolsets._langchain import as_tool

_SKILL_MANAGEMENT_TOOL_POLICIES = {
    "skill_discover_github": ToolPolicy(
        risk_level="medium",
        side_effects=("public_network_read", "skill_inspection_cache_write"),
    ),
    "skill_install": ToolPolicy(
        risk_level="high",
        side_effects=("workspace_skill_install",),
    ),
    "skill_bind_agent": ToolPolicy(
        risk_level="high",
        side_effects=("agent_revision_write",),
    ),
}


def _gateway():
    return default_tool_gateway(_SKILL_MANAGEMENT_TOOL_POLICIES)


def skill_discover_github(source: str, ref: str = "main", limit: int = 5) -> dict[str, Any]:
    """Find or inspect a public GitHub Skill without executing repository code.

    Pass a GitHub repository URL to resolve its mutable ref to an immutable
    commit and inspect every SKILL.md candidate. Pass a short name or search
    phrase to receive public repository candidates first. Installing remains a
    separate confirmation-gated action.
    """

    return _dict_result(
        _gateway().invoke("skill_discover_github", _skill_discover_github_impl, source, ref, limit),
        tool_name="skill_discover_github",
    )


def _skill_discover_github_impl(
    source: str,
    ref: str = "main",
    limit: int = 5,
) -> dict[str, Any]:
    value = str(source or "").strip()
    if not value:
        return {
            "ok": False,
            "error_type": "source_required",
            "error_message": "source must be a public GitHub URL or search phrase",
        }
    if value.startswith(("https://github.com/", "http://github.com/", "git@github.com:")):
        try:
            inspection = _catalog().skill_discovery.discover_github(value, ref=ref)
        except StudioError as exc:
            return _studio_error(exc)
        return {"ok": True, "mode": "inspection", **inspection}

    bounded_limit = max(1, min(int(limit or 5), 10))
    try:
        with httpx.Client(
            timeout=20,
            follow_redirects=True,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ksadk-studio-skill-discovery/1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as client:
            response = client.get(
                "https://api.github.com/search/repositories",
                params={"q": f"{value} in:name,description", "per_page": bounded_limit},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "ok": False,
            "error_type": "skill_search_failed",
            "error_message": "Unable to search public GitHub repositories",
            "detail": str(exc)[:500],
        }
    repositories = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        repositories.append(
            {
                "full_name": str(item.get("full_name") or ""),
                "url": str(item.get("html_url") or ""),
                "description": str(item.get("description") or ""),
                "owner": str(owner.get("login") or ""),
                "stars": int(item.get("stargazers_count") or 0),
                "default_branch": str(item.get("default_branch") or "main"),
                "archived": bool(item.get("archived")),
            }
        )
    return {
        "ok": True,
        "mode": "search",
        "query": value,
        "repositories": repositories,
        "next": (
            "Choose the intended repository, then call skill_discover_github again "
            "with its exact url before installing."
        ),
    }


def skill_install(
    inspection_token: str,
    candidate_id: str | None = None,
    candidate_ids: list[str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Install one previously inspected Skill candidate into the Studio resource catalog.

    This action accepts only a persisted inspection token and candidate id; it
    never downloads a new mutable ref during installation. Binding the installed
    resource to the current Agent remains a separate action.
    """

    return _dict_result(
        _gateway().invoke(
            "skill_install",
            _skill_install_impl,
            inspection_token,
            candidate_id,
            candidate_ids,
            overwrite,
        ),
        tool_name="skill_install",
    )


def _skill_install_impl(
    inspection_token: str,
    candidate_id: str | None = None,
    candidate_ids: list[str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    selected = [str(item).strip() for item in (candidate_ids or []) if str(item).strip()]
    if candidate_id and str(candidate_id).strip() not in selected:
        selected.insert(0, str(candidate_id).strip())
    if not selected:
        return {
            "ok": False,
            "error_type": "candidate_required",
            "error_message": "candidate_id or candidate_ids is required",
        }
    installed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    catalog = _catalog()
    for selected_id in selected:
        try:
            descriptor = catalog.commit_discovered_skill(
                inspection_token,
                selected_id,
                overwrite=bool(overwrite),
            )
        except StudioError as exc:
            failures.append({"candidate_id": selected_id, **_studio_error(exc)})
            continue
        installed.append(
            descriptor.model_dump(by_alias=True, exclude_none=True, mode="json")
        )
    result: dict[str, Any] = {
        "ok": not failures,
        "installed": installed,
        "failures": failures,
        "next": "Call skill_bind_agent with the installed resourceIds to bind them.",
    }
    if len(installed) == 1:
        result["installed_resource"] = installed[0]
    return result


def skill_bind_agent(
    resource_id: str | None = None,
    resource_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Bind an installed Skill to the current Studio Agent revision.

    The current immutable run is not mutated. Studio automatically builds the
    new Agent revision for the next user turn, which can then disclose and use
    the Skill in the same conversation.
    """

    return _dict_result(
        _gateway().invoke(
            "skill_bind_agent",
            _skill_bind_agent_impl,
            resource_id,
            resource_ids,
        ),
        tool_name="skill_bind_agent",
    )


def _skill_bind_agent_impl(
    resource_id: str | None = None,
    resource_ids: list[str] | None = None,
) -> dict[str, Any]:
    from ksadk.studio.contracts import CapabilityBinding
    from ksadk.studio.repository import AgentDraftRepository
    from ksadk.studio.resource_catalog import LocalResourceCatalog

    workspace, agent_id = _studio_context()
    catalog = LocalResourceCatalog(workspace)
    try:
        selected = [str(item).strip() for item in (resource_ids or []) if str(item).strip()]
        if resource_id and str(resource_id).strip() not in selected:
            selected.insert(0, str(resource_id).strip())
        if not selected:
            return {
                "ok": False,
                "error_type": "resource_required",
                "error_message": "resource_id or resource_ids is required",
            }
        descriptors = [catalog.get(item) for item in selected]
        if any(item.kind != "skill" or item.status != "ready" for item in descriptors):
            return {
                "ok": False,
                "error_type": "skill_not_ready",
                "error_message": "Only installed, ready Skills can be bound",
            }
        drafts = AgentDraftRepository(workspace)
        current = drafts.get(agent_id)
        existing = {binding.resource_id for binding in current.spec.bindings.skills}
        additions = [item for item in descriptors if item.resource_id not in existing]
        if not additions:
            return {
                "ok": True,
                "already_bound": True,
                "agent_id": agent_id,
                "resource_ids": selected,
                "revision": current.metadata.revision,
                "effective_from": "next_run",
            }
        updated_spec = current.spec.model_copy(deep=True)
        updated_spec.bindings.skills.extend(
            CapabilityBinding(resource_id=item.resource_id, enabled=True)
            for item in additions
        )
        catalog.resolve_skills(updated_spec.bindings)
        updated = drafts.update(
            agent_id,
            updated_spec,
            expected_revision=current.metadata.revision,
        )
    except StudioError as exc:
        return _studio_error(exc)
    return {
        "ok": True,
        "already_bound": False,
        "agent_id": agent_id,
        "resource_ids": [item.resource_id for item in additions],
        "revision": updated.metadata.revision,
        "effective_from": "next_run",
        "message": "Skill is bound. It becomes available on the next user turn.",
    }


def _studio_context() -> tuple[Any, str]:
    from ksadk.studio.workspace import Workspace

    project = str(os.environ.get("KSADK_PROJECT_DIR") or "").strip()
    agent_id = str(os.environ.get("KSADK_STUDIO_AGENT_ID") or "").strip()
    if not project or not agent_id:
        raise StudioError(
            "STUDIO_SKILL_MANAGEMENT_UNAVAILABLE",
            "Skill management tools are available only inside a bound local Studio Agent run",
            status_code=409,
        )
    root = Path(project).expanduser().resolve()
    return Workspace(root), agent_id


def _catalog() -> Any:
    from ksadk.studio.resource_catalog import LocalResourceCatalog

    workspace, _ = _studio_context()
    return LocalResourceCatalog(workspace)


def _studio_error(error: StudioError) -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": error.code,
        "error_message": error.message,
        "details": error.details,
    }


def _dict_result(value: Any, *, tool_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{tool_name} returned {type(value).__name__}, expected dict")
    return dict(value)


def get_skill_management_tools() -> list[Any]:
    return [
        as_tool(skill_discover_github),
        as_tool(skill_install),
        as_tool(skill_bind_agent),
    ]
