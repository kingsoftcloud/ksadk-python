"""Host-owned, per-principal consent to read project instruction resources.

Stored outside a cloned workspace: repository files cannot pre-grant consent.
This grants document consumption only, never tool or system-message authority.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


class ProjectInstructionTrustStore:
    def __init__(self, path: Path | None = None):
        self.path = path or Path.home() / ".agentengine" / "project-instructions.sqlite"

    @staticmethod
    def _key(root, *, tenant_id, workspace_id, user_id):
        if any(not isinstance(v, str) or not v.strip()
               for v in (tenant_id, workspace_id, user_id)):
            raise ValueError("project_instruction_principal_required")
        return hashlib.sha256(json.dumps(
            [str(Path(root).resolve()), tenant_id, workspace_id, user_id],
            ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()

    def state(self, root, **principal) -> tuple[bool, int]:
        key = self._key(root, **principal)
        if not self.path.exists():
            return False, 0
        with sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='project_instruction_trust'").fetchone() is None:
                return False, 0
            row = db.execute("SELECT trusted, revision FROM project_instruction_trust WHERE id=?",
                             (key,)).fetchone()
        return (bool(row[0]), row[1]) if row else (False, 0)

    def set(self, root, trusted: bool, **principal) -> tuple[bool, int]:
        if type(trusted) is not bool:
            raise ValueError("project_instruction_trust_boolean_required")
        key = self._key(root, **principal)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS project_instruction_trust "
                       "(id TEXT PRIMARY KEY, trusted INTEGER NOT NULL, revision INTEGER NOT NULL)")
            db.execute("INSERT INTO project_instruction_trust VALUES (?, ?, 1) "
                       "ON CONFLICT(id) DO UPDATE SET trusted=excluded.trusted, revision=revision+1",
                       (key, int(trusted)))
            row = db.execute("SELECT trusted, revision FROM project_instruction_trust WHERE id=?",
                             (key,)).fetchone()
        return bool(row[0]), row[1]
