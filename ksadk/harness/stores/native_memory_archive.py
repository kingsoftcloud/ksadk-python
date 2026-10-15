"""Lossless shared JSON nodes for historical in-process snapshots.

The live snapshot remains canonical JSON. Historical commits reference immutable
nodes, so unchanged messages and observations are retained once per Run. This is
an internal memory representation; the RunStore and RunSnapshot contracts stay
unchanged, and replay reconstructs the exact original canonical JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


def _json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def snapshot_json_from_nodes(nodes, root: int) -> str:
    """Restore a snapshot from its immutable nodes, including private evidence."""
    kind, value = nodes[root]
    if kind == "atom":
        return value
    if kind == "array":
        return "[" + ",".join(snapshot_json_from_nodes(nodes, item) for item in value) + "]"
    if kind == "object":
        return (
            "{"
            + ",".join(
                snapshot_json_from_nodes(nodes, key) + ":" + snapshot_json_from_nodes(nodes, item)
                for key, item in value
            )
            + "}"
        )
    raise ValueError("invalid memory snapshot node")


@dataclass(slots=True)
class SnapshotArchive:
    nodes: list[tuple] = field(default_factory=list)
    _index: dict[tuple, int] = field(default_factory=dict)

    def _intern(self, node: tuple) -> int:
        existing = self._index.get(node)
        if existing is not None:
            return existing
        index = len(self.nodes)
        self._index[node] = index
        self.nodes.append(node)
        return index

    def _freeze(self, value) -> int:
        if isinstance(value, dict):
            node = (
                "object",
                tuple((self._freeze(key), self._freeze(value[key])) for key in sorted(value)),
            )
        elif isinstance(value, list):
            node = ("array", tuple(self._freeze(item) for item in value))
        else:
            node = ("atom", _json(value))
        return self._intern(node)

    def store(self, canonical_snapshot: str) -> int:
        return self._freeze(json.loads(canonical_snapshot))

    def restore(self, root: int) -> str:
        return snapshot_json_from_nodes(self.nodes, root)
