"""Frozen inputs and per-attempt working trees inside an execution node.

The authority stores an intent only. This module resolves filesystem paths on
the actual execution node, within roots explicitly granted by its owner.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from .artifacts import read_workspace_artifact
from .errors import TeamsError
from .store import digest, encode


def validate_workspace_plan(plan: dict | None, *, require_pinned_git: bool = False) -> None:
    if (
        plan
        and require_pinned_git
        and plan.get("mode", "auto") != "directory"
        and not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", str(plan.get("baseRef") or ""))
    ):
        raise TeamsError(
            "workspace_commit_required",
            "服务端 Git 任务需要完整提交哈希；非 Git 文件请选择目录模式",
            status=422,
        )


def _component(value: str) -> str:
    if re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", value):
        return value
    return hashlib.sha256(value.encode()).hexdigest()


def _git(source: Path, *arguments: str, required: bool = True) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(source), *arguments],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TeamsError(
            "workspace_git_unavailable", "Git 工作区准备失败，请检查节点环境", status=422
        ) from error
    if result.returncode:
        if not required:
            return None
        raise TeamsError(
            "workspace_git_failed", "无法准备所选 Git 基线，请检查提交与仓库", status=422
        )
    return result.stdout.strip()


def _relative_input(value: str) -> Path:
    path = Path(value)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or ".git" in path.parts
        or "\x00" in value
    ):
        raise TeamsError(
            "workspace_input_forbidden", "输入必须是源目录内的普通相对文件", status=422
        )
    return path


def _mkdir(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise TeamsError("workspace_path_forbidden", "工作区路径不能包含符号链接", status=403)
        current.mkdir(exist_ok=True)


def prepare_workspace(
    output_root: Path,
    *,
    group_id: str,
    team_run_id: str,
    member_id: str,
    attempt_id: str,
    plan: dict | None,
    allowed_roots: Sequence[Path],
    require_pinned_git: bool = False,
) -> Path:
    """Return the real cwd; each run resolves its baseline exactly once per node.

    Git HEAD/ref includes committed content. Explicit input files are frozen
    separately, allowing a user to intentionally supply an untracked file.
    Existing attempt directories are reused without resetting their changes.
    """
    validate_workspace_plan(plan, require_pinned_git=require_pinned_git)
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_root = output_root / _component(group_id) / _component(team_run_id)
    target = run_root / _component(member_id) / _component(attempt_id)
    _mkdir(target.parent, output_root)
    if not plan:
        _mkdir(target, output_root)
        return target
    source = Path(plan["sourcePath"]).expanduser().resolve()
    if not any(source.is_relative_to(Path(root).resolve()) for root in allowed_roots):
        raise TeamsError(
            "workspace_source_forbidden", "所选目录不在该执行节点允许的工作目录内", status=403
        )
    if not source.is_dir():
        raise TeamsError("workspace_source_missing", "所选源目录在执行节点上不存在", status=422)
    # A filesystem lock coordinates simultaneous members and multiple node
    # workers. Keep the lock file outside their writable attempt directories.
    try:
        import fcntl
    except ImportError as error:
        raise TeamsError(
            "workspace_platform_unsupported", "当前节点不支持隔离工作区锁", status=422
        ) from error
    lock = run_root / ".workspace.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TeamsError(
                "workspace_busy", "任务基线正在由另一个执行准备，请稍后重试", status=409
            ) from error
        manifest_path = run_root / ".workspace.json"
        if manifest_path.is_symlink():
            raise TeamsError("workspace_manifest_invalid", "任务基线文件不可用", status=409)
        plan_digest = digest(plan)
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if manifest["planDigest"] != plan_digest:
                raise TeamsError(
                    "workspace_plan_changed", "任务基线已经冻结，不能在执行中修改", status=409
                )
        else:
            git_root = None
            if plan.get("mode", "auto") == "auto":
                git_root = _git(source, "rev-parse", "--show-toplevel", required=False)
            if git_root:
                repository = Path(git_root).resolve()
                if not any(
                    repository.is_relative_to(Path(root).resolve()) for root in allowed_roots
                ):
                    raise TeamsError(
                        "workspace_source_forbidden", "Git 仓库根目录超出节点授权范围", status=403
                    )
                commit = _git(
                    repository,
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    str(plan.get("baseRef") or "HEAD") + "^{commit}",
                )
                if require_pinned_git and commit.lower() != plan["baseRef"].lower():
                    raise TeamsError(
                        "workspace_commit_mismatch", "节点仓库与任务冻结提交不一致", status=409
                    )
            else:
                repository, commit = None, None
                if plan.get("baseRef"):
                    raise TeamsError(
                        "workspace_base_ref_invalid", "只有 Git 工作区可以选择提交基线", status=422
                    )
            manifest = {
                "planDigest": plan_digest,
                "sourcePath": str(source),
                "repository": str(repository) if repository else None,
                "commit": commit,
                "inputs": {},
            }
            frozen = run_root / ".inputs"
            _mkdir(frozen, output_root)
            total = 0
            # Validate all files before publishing the run baseline.
            supplied = []
            for value in dict.fromkeys(plan.get("inputs", [])):
                relative = _relative_input(value)
                _, content = read_workspace_artifact(source, str(relative))
                total += len(content)
                if total > 64 * 1024 * 1024:
                    raise TeamsError(
                        "workspace_inputs_too_large", "单任务输入文件总计不能超过64MiB", status=422
                    )
                supplied.append((relative, content))
            for relative, content in supplied:
                input_target = frozen / relative
                _mkdir(input_target.parent, output_root)
                if input_target.is_symlink():
                    raise TeamsError(
                        "workspace_input_forbidden", "输入快照不能包含符号链接", status=403
                    )
                input_target.write_bytes(content)
                manifest["inputs"][str(relative)] = hashlib.sha256(content).hexdigest()
            temporary = manifest_path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(encode(manifest))
            temporary.replace(manifest_path)
        marker = target.parent / (target.name + ".prepared.json")
        if marker.exists():
            if (
                marker.is_symlink()
                or json.loads(marker.read_text()).get("planDigest") != plan_digest
            ):
                raise TeamsError("workspace_manifest_invalid", "执行工作区标识不一致", status=409)
            if target.is_symlink() or not target.is_dir():
                raise TeamsError("workspace_path_forbidden", "执行工作区不可用", status=409)
            return target
        if target.exists():
            # Never reset or delete partially prepared / user-edited files.
            raise TeamsError(
                "workspace_preparation_incomplete",
                "工作区准备未完成，请检查该次尝试的目录后重试",
                status=409,
            )
        if manifest["repository"]:
            _git(
                Path(manifest["repository"]),
                "worktree",
                "add",
                "--detach",
                str(target),
                manifest["commit"],
            )
        else:
            _mkdir(target, output_root)
        for value, checksum in manifest["inputs"].items():
            relative = _relative_input(value)
            _, content = read_workspace_artifact(run_root / ".inputs", value)
            if hashlib.sha256(content).hexdigest() != checksum:
                raise TeamsError("workspace_input_changed", "冻结输入文件摘要已改变", status=409)
            destination = target / relative
            _mkdir(destination.parent, output_root)
            if destination.is_symlink():
                raise TeamsError("workspace_input_forbidden", "输入目标不能是符号链接", status=403)
            destination.write_bytes(content)
        with marker.open("x", encoding="utf-8") as stream:
            stream.write(encode({"planDigest": plan_digest, "commit": manifest["commit"]}))
        return target
    finally:
        os.close(fd)
