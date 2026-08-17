"""校验 Phase 1 跨仓基线 manifest（plan Task 0）。

拒绝 dirty、缺 commit、缺 remote、未验收 Phase 0 和重复 repo key。
退出码非 0 时阻断 Phase 1 合并与部署。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from scripts.build_phase1_baseline import BaselineError, Phase1Baseline


def verify_manifest(path: Path) -> None:
    baseline = Phase1Baseline.model_validate_json(path.read_text())
    if not baseline.phase0.accepted:
        raise BaselineError(f"phase0_not_accepted: {baseline.phase0.status}")
    seen: set[str] = set()
    for repo in baseline.repositories:
        if repo.repo in seen:
            raise BaselineError(f"duplicate_repo: {repo.repo}")
        seen.add(repo.repo)
        if repo.dirty:
            raise BaselineError(f"dirty_worktree: {repo.repo}")
        if not repo.commit_sha or len(repo.commit_sha) != 40:
            raise BaselineError(f"missing_commit: {repo.repo}")
        if not repo.remote:
            raise BaselineError(f"missing_remote: {repo.repo}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Phase 1 baseline manifest")
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        verify_manifest(args.path)
    except (BaselineError, ValidationError) as exc:
        print(f"baseline_rejected: {exc}", file=sys.stderr)
        return 1
    print("baseline_verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
