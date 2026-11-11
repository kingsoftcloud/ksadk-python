"""Bound the model's directory view without replacing the executed receipt."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import PurePosixPath

DIRECTORY_OBSERVATION_POLICY = "studio-directory-observation/v1"
DIRECTORY_MODEL_DESCRIPTION = (
    " Use glob to find filenames directly (for example '**/*.py'); narrow path "
    "before recursive listing. Native returns a bounded path view with explicit "
    "omissions; the saved complete result retains entry metadata. Sorting by "
    "size or mtime also exposes that sort value."
)
DIRECTORY_SCOPE_MODEL_DESCRIPTION = (
    DIRECTORY_MODEL_DESCRIPTION
    + " glob filters the selected entries; it does not enable recursion. "
    "Set recursive=true to search nested directories, including with '**/*.py'. "
    "An empty nonrecursive listing describes only immediate children."
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def directory_feedback_digest(result_bytes: int, *, policy: str = DIRECTORY_OBSERVATION_POLICY) -> str:
    _validate_policy(policy)
    return (
        "sha256:"
        + hashlib.sha256(
            _json(
                {
                    "policy": policy,
                    "max_entries": 100,
                    "max_model_bytes": 4096,
                    "archive_framing_reserve_bytes": 640,
                    "result_bytes": result_bytes,
                    "history": "observed-archive/v4",
                }
            ).encode()
        ).hexdigest()
    )


def _validate_policy(policy):
    if policy not in {
        DIRECTORY_OBSERVATION_POLICY,
        "studio-directory-observation/v2", "studio-directory-observation/v3",
    }:
        raise ValueError("native_directory_observation_policy_invalid")


def _child_directories(value):
    """Quote only actual immediate directory entries, never infer unseen paths."""
    base = value.get("path")
    if not isinstance(base, str) or not base or "\\" in base:
        return []
    root = PurePosixPath(base)
    if root.is_absolute() or ".." in root.parts:
        return []
    return [entry["path"] + "/" for entry in value["entries"]
            if entry["type"] == "directory"
            and "\\" not in entry["path"]
            and ".." not in PurePosixPath(entry["path"]).parts
            and PurePosixPath(entry["path"]).parent == root]


def directory_model_reply(
    value, arguments, *, result_bytes: int, policy: str = DIRECTORY_OBSERVATION_POLICY,
) -> str | None:
    """Project only a successful built-in listing; keep all raw data at the caller.

    Limits apply to this model observation, never to the actual requested
    listing. Omitted names and metadata remain accessible in the canonical
    archived result. An unfamiliar response uses the normal result governance.
    """
    _validate_policy(policy)
    if (
        not isinstance(value, Mapping)
        or value.get("ok") is not True
        or not isinstance(value.get("entries"), list)
        or type(value.get("truncated")) is not bool
    ):
        return None
    entries = value["entries"]
    sort = str(arguments.get("sort_by") or "name").strip().lower()
    sort_field = {"size": "size", "mtime": "mtime_ns"}.get(sort)
    paths = []
    for entry in entries:
        if (
            not isinstance(entry, Mapping)
            or not isinstance(entry.get("path"), str)
            or not entry["path"]
            or entry.get("type") not in ("file", "directory")
        ):
            return None
        if sort_field is not None and type(entry.get(sort_field)) is not int:
            return None
    budget = min(4096, result_bytes - 640)
    overview = {}
    if policy == "studio-directory-observation/v3":
        # Keep the executed search boundary even for an empty result. A glob
        # filters a shallow listing unless recursion was explicitly requested;
        # it cannot prove anything about unsearched descendants. Never broaden
        # execution or perform another filesystem read for this observation.
        if not isinstance(value.get("path"), str) or type(value.get("recursive")) is not bool:
            return None
        pattern = arguments.get("glob")
        if pattern is not None and not isinstance(pattern, str):
            return None
        overview["listing_scope"] = {
            "path": value["path"], "recursive": value["recursive"], "glob": pattern,
        }
        if not entries and value["recursive"] is False:
            overview["scope_note"] = (
                "Only immediate children were searched; glob does not enable recursion. "
                "For nested paths, set recursive=true."
            )

    def render():
        return _json(
            {
                "status": "ok",
                "content": {
                    "paths": paths,
                    "returned_entries": len(entries),
                    "shown_entries": len(paths),
                    "omitted_entries": len(entries) - len(paths),
                    "result_truncated": value["truncated"],
                    "metadata": "Metadata omitted from this view; use available artifact_uri or narrow path/glob.",
                    **overview,
                },
            }
        )

    if len(render().encode()) > budget:
        return None
    # A recursive alphabetical prefix can be entirely occupied by one large
    # subtree. Preserve the requested path order and separately expose only
    # sibling directories already returned by that same authorized execution.
    # This is not another filesystem read or a claim of complete tree coverage.
    if (policy in {"studio-directory-observation/v2", "studio-directory-observation/v3"}
            and value.get("recursive") is True):
        directories = _child_directories(value)
        shown = []
        overview.update(
            observed_child_directories=shown,
            child_directories_omitted=len(directories),
            child_directory_scope=(
                "returned entries only; listing truncated"
                if value["truncated"] else "returned entries only"
            ),
        )
        if len(render().encode()) > budget - 128:
            for key in (
                "observed_child_directories", "child_directories_omitted", "child_directory_scope",
            ):
                overview.pop(key, None)
        else:
            overview_budget = min(1024, budget // 2)
            for directory in directories:
                shown.append(directory)
                overview["child_directories_omitted"] = len(directories) - len(shown)
                if len(_json(overview).encode()) > overview_budget or len(render().encode()) > budget - 128:
                    shown.pop()
                    overview["child_directories_omitted"] = len(directories) - len(shown)
                    break
    for entry in entries[:100]:
        path = entry["path"] + ("/" if entry["type"] == "directory" else "")
        paths.append({"path": path, sort_field: entry[sort_field]} if sort_field else path)
        if len(render().encode()) > budget:
            paths.pop()
            break
    return render()
