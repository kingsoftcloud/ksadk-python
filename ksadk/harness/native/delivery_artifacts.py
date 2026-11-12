"""Inspect only this goal's confirmed deliverables inside its delivery window."""

from copy import deepcopy

from .state import DeliveryArtifacts


def written_delivery_paths(snapshot):
    state = snapshot.delivery_artifacts
    return state.paths if state is not None and state.goal_revision == snapshot.goal_revision else ()


def remember_delivery_write(snapshot, result, definition):
    previous = snapshot.delivery_artifacts
    if (
        definition is None or definition.delivery_artifact_role != "write"
        or not definition.delivery_write or definition.read_only or result.status != "ok"
        or not snapshot.pending_tools
    ):
        return previous
    path = snapshot.pending_tools[0].get("arguments", {}).get("path")
    if not isinstance(path, str) or path not in result.workspace_changed_paths:
        return previous
    paths = tuple(dict.fromkeys((*written_delivery_paths(snapshot), path)))[:2048]
    return DeliveryArtifacts(goal_revision=snapshot.goal_revision, paths=paths)


def scoped_delivery_reader(tool, snapshot):
    if (
        tool.delivery_artifact_role != "read" or not tool.read_only
        or tool.context_role != "workspace_read" or not written_delivery_paths(snapshot)
    ):
        return None
    paths = list(written_delivery_paths(snapshot))
    parameters = deepcopy(tool.parameters)
    parameters.setdefault("properties", {}).setdefault("path", {})["enum"] = paths
    # Preserve an existing schema's required fields and constraints.
    parameters.setdefault("allOf", []).append({
        "properties": {"path": {"enum": paths}}, "required": ["path"],
    })
    return tool.model_copy(update={
        "parameters": parameters,
        "description": tool.description + (
            " In the deadline delivery window, inspect only the listed paths already "
            "successfully written by this Run for its current goal. This permits checking "
            "current exact text before an edit, not further source exploration or a pass verdict."
        ),
    })


def delivery_read_outside_paths(snapshot, config, calls):
    from .tool_budget import wall_delivery_only

    if not wall_delivery_only(snapshot, config):
        return False
    readers = {t.name for t in config.tools if t.delivery_artifact_role == "read"}
    paths = written_delivery_paths(snapshot)
    return any(c.name in readers and c.arguments.get("path") not in paths for c in calls)
