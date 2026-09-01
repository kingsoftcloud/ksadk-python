"""Harness Artifact Store（缺口 5 / plan §6.2 大结果外置）。

工具产出的大结果（报表、文件、生成物）落工作区磁盘，State/事件流只带
引用（``artifact.created`` payload 的 ``uri``）。同一 ``(run, name)`` 重复
保存递增 ``version``，历史版本保留可下载。

存储形态：内容文件 ``{root}/{run_id}/{name}.v{version}`` + SQLite 索引
（run_id / name / version / mime / content_hash / bytes / created_at）。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArtifactRecord:
    run_id: str
    name: str
    version: int
    mime: str
    content_hash: str
    bytes: int
    uri: str
    created_at: str


class ArtifactStore:
    """Run 作用域 Artifact 持久化（线程安全由 SQLite 连接锁保证）。"""

    def __init__(self, root: str | Path, db_path: str | Path | None = None) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path or self._root / "artifacts.sqlite3"))
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                run_id TEXT NOT NULL,
                name TEXT NOT NULL,
                version INTEGER NOT NULL,
                mime TEXT NOT NULL DEFAULT "text/plain",
                content_hash TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                uri TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, name, version)
            )
            """
        )
        self._db.commit()

    # ------------------------------------------------------------- 写入

    def save(
        self, *, run_id: str, name: str, content: bytes, mime: str = "text/plain"
    ) -> ArtifactRecord:
        name = _safe_name(name)
        if not name:
            raise ValueError("artifact name 不能为空")
        run_id = _safe_name(run_id)
        if not run_id:
            raise ValueError("run_id 不能为空")
        version = self._next_version(run_id, name)
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        directory = self._root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        file_path = directory / f"{name}.v{version}"
        # 目录穿越防线：规范化后必须仍在 Store 根目录内（run_id/name 已各
        # 自过 _safe_name，此处校验最终落点，防御纵深）。
        _assert_within_root(self._root, file_path)
        file_path.write_bytes(content)
        uri = f"artifact://{run_id}/{name}@v{version}"
        record = ArtifactRecord(
            run_id=run_id,
            name=name,
            version=version,
            mime=mime,
            content_hash=digest,
            bytes=len(content),
            uri=uri,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._db.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.run_id,
                record.name,
                record.version,
                record.mime,
                record.content_hash,
                record.bytes,
                record.uri,
                record.created_at,
            ),
        )
        self._db.commit()
        return record

    # ------------------------------------------------------------- 读取

    def list(self, run_id: str, *, name: str | None = None) -> list[ArtifactRecord]:
        run_id = _safe_name(run_id)
        query = "SELECT * FROM artifacts WHERE run_id = ?"
        params: list[Any] = [run_id]
        if name is not None:
            query += " AND name = ?"
            params.append(_safe_name(name))
        query += " ORDER BY name, version"
        rows = self._db.execute(query, params).fetchall()
        return [_record(row) for row in rows]

    def latest(self, run_id: str, name: str) -> ArtifactRecord | None:
        records = self.list(run_id, name=name)
        return records[-1] if records else None

    def read(self, record: ArtifactRecord) -> bytes:
        return self._read_uri(record.uri)

    def read_latest(self, run_id: str, name: str) -> bytes | None:
        record = self.latest(run_id, name)
        return None if record is None else self.read(record)

    def _read_uri(self, uri: str) -> bytes:
        # uri: artifact://{run_id}/{name}@v{version}
        try:
            body = uri[len("artifact://") :]
            run_id, rest = body.split("/", 1)
            name, version = rest.rsplit("@v", 1)
            file_path = self._root / _safe_name(run_id) / f"{_safe_name(name)}.v{int(version)}"
        except ValueError as exc:
            raise ValueError(f"非法 artifact uri: {uri!r}") from exc
        _assert_within_root(self._root, file_path)
        if not file_path.is_file():
            raise FileNotFoundError(f"artifact 内容缺失: {uri}")
        return file_path.read_bytes()

    # ------------------------------------------------------------- 内部

    def _next_version(self, run_id: str, name: str) -> int:
        row = self._db.execute(
            "SELECT MAX(version) FROM artifacts WHERE run_id = ? AND name = ?",
            (run_id, name),
        ).fetchone()
        return (row[0] or 0) + 1

    def close(self) -> None:
        self._db.close()


class BudgetedArtifactStore:
    """Run 级写入守卫；在委托真实 Store 前原子检查数量预算。"""

    def __init__(self, store: ArtifactStore, *, run_id: str, max_artifacts: int) -> None:
        self._store = store
        self._run_id = run_id
        self._max_artifacts = max_artifacts

    def save(self, *, run_id: str, name: str, content: bytes, mime: str = "text/plain"):
        if run_id != self._run_id:
            raise ValueError("budgeted artifact store run_id mismatch")
        if len(self._store.list(run_id)) >= self._max_artifacts:
            raise ArtifactBudgetExceeded(
                f"artifact budget {self._max_artifacts} exhausted before write"
            )
        return self._store.save(run_id=run_id, name=name, content=content, mime=mime)

    def list(self, run_id: str, *, name: str | None = None):
        return self._store.list(run_id, name=name)

    def __getattr__(self, name: str):
        return getattr(self._store, name)


class ArtifactBudgetExceeded(ValueError):
    """Artifact 写入在产生文件或索引副作用前被预算拒绝。"""


def _safe_name(name: str) -> str:
    keep = [c if (c.isalnum() or c in "-_.") else "_" for c in str(name or "").strip()]
    return "".join(keep)[:128]


def _assert_within_root(root: Path, path: Path) -> None:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    if root_resolved != path_resolved and root_resolved not in path_resolved.parents:
        raise ValueError(f"artifact 路径越界: {path}")


def _record(row: tuple) -> ArtifactRecord:
    return ArtifactRecord(
        run_id=row[0],
        name=row[1],
        version=int(row[2]),
        mime=row[3],
        content_hash=row[4],
        bytes=int(row[5]),
        uri=row[6],
        created_at=row[7],
    )


__all__ = [
    "ArtifactBudgetExceeded",
    "ArtifactRecord",
    "ArtifactStore",
    "BudgetedArtifactStore",
]
