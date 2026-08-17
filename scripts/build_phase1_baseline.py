"""构建 Phase 1 跨仓基线 manifest（plan Task 0）。

phase0_gate_status 只能来自 Phase 0 release manifest 的 accepted 字段，
不接受任何命令行布尔参数伪造。git 事实（commit/remote/dirty）通过
`git -C <repo> rev-parse/status/remote` 读取。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

PHASE0_MANIFEST_RELATIVE = "docs/superpowers/evidence/phase0/manifest.json"


class BaselineError(Exception):
    """基线不满足 Phase 1 前置条件。"""


class RepoBaseline(BaseModel):
    repo: str
    remote: str
    branch_base: str
    commit_sha: str
    dirty: bool


class Phase0Gate(BaseModel):
    accepted: bool
    status: str
    manifest_path: str
    manifest_digest: str | None = None


class Phase1Baseline(BaseModel):
    schema_version: int = 1
    repositories: list[RepoBaseline]
    phase0: Phase0Gate
    contract_digest: str | None = None
    captured_at: str


def _git(repo_root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise BaselineError(f"git {' '.join(args)} failed in {repo_root}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def load_phase0_gate(workspace_or_repo: Path) -> Phase0Gate:
    """读取 Phase 0 release manifest；缺失/非法一律视为未验收。"""
    candidates = (
        workspace_or_repo / PHASE0_MANIFEST_RELATIVE,
        workspace_or_repo / "manifest.json",
    )
    path = next((p for p in candidates if p.exists()), candidates[0])
    if not path.exists():
        return Phase0Gate(
            accepted=False,
            status="phase0_manifest_missing",
            manifest_path=PHASE0_MANIFEST_RELATIVE,
        )
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise BaselineError(f"phase0_manifest_invalid: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("accepted"), bool):
        raise BaselineError("phase0_manifest_invalid: accepted must be a boolean")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return Phase0Gate(
        accepted=raw["accepted"],
        status="accepted" if raw["accepted"] else str(raw.get("status", "failed")),
        manifest_path=PHASE0_MANIFEST_RELATIVE,
        manifest_digest=digest,
    )


def build_manifest(
    repo_roots: Mapping[str, Path], *, exclude: Mapping[str, Path] | None = None
) -> Phase1Baseline:
    phase0 = load_phase0_gate(next(iter(repo_roots.values())))
    repositories: list[RepoBaseline] = []
    for repo, root in sorted(repo_roots.items()):
        root = Path(root)
        commit_sha = _git(root, "rev-parse", "HEAD")
        remote = _git(root, "remote", "get-url", "origin")
        branch_base = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        dirty_lines = _git(root, "status", "--porcelain=v1").splitlines()
        excluded = str((exclude or {}).get(repo, "")) if exclude else ""
        dirty = any(not line[3:] == excluded for line in dirty_lines)
        repositories.append(
            RepoBaseline(
                repo=repo,
                remote=remote,
                branch_base=branch_base,
                commit_sha=commit_sha,
                dirty=dirty,
            )
        )
    return Phase1Baseline(
        repositories=repositories,
        phase0=phase0,
        captured_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Phase 1 cross-repo baseline manifest")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    repo_roots: dict[str, Path] = {
        "ksadk-python": Path(__file__).resolve().parents[1],
    }
    agentengine = args.workspace / "agentengine"
    for candidate in (
        "agentengine-server",
        "agent-runtime-service",
        "agent-platform-operator",
        "agentengine-gateway",
        "ksadk-web",
    ):
        path = agentengine / candidate
        if path.exists():
            repo_roots[candidate] = path

    baseline = build_manifest(repo_roots)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(baseline.model_dump_json(indent=2) + "\n")
    print(f"baseline written: {args.output}")
    if not baseline.phase0.accepted:
        print(f"phase0_not_accepted: {baseline.phase0.status}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
