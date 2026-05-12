"""Helpers for local commands that execute user Agent code."""

from __future__ import annotations

import os
from pathlib import Path
import site
import sys
from typing import Sequence


LOCAL_RUNTIME_VENV_REEXEC_ENV = "AGENTENGINE_LOCAL_RUNTIME_VENV_REEXEC"
LEGACY_WEB_VENV_REEXEC_ENV = "AGENTENGINE_WEB_VENV_REEXEC"


def find_project_venv_python(agent_path: Path) -> Path | None:
    for candidate in (
        agent_path / ".venv" / "bin" / "python",
        agent_path / ".venv" / "Scripts" / "python.exe",
        agent_path / "venv" / "bin" / "python",
        agent_path / "venv" / "Scripts" / "python.exe",
    ):
        if candidate.exists():
            return candidate.absolute()
    return None


def is_current_python(python_path: Path) -> bool:
    try:
        return Path(sys.executable).absolute() == python_path.absolute()
    except OSError:
        return False


def prepend_env_path(env: dict[str, str], key: str, value: str) -> None:
    existing = env.get(key)
    paths = existing.split(os.pathsep) if existing else []
    if value not in paths:
        paths.insert(0, value)
    env[key] = os.pathsep.join(paths)


def _current_site_package_paths() -> list[str]:
    paths: list[str] = []

    def _add(value: object) -> None:
        path = str(value or "")
        if path and path not in paths and Path(path).exists():
            paths.append(path)

    try:
        for path in site.getsitepackages():
            _add(path)
    except Exception:
        pass
    try:
        _add(site.getusersitepackages())
    except Exception:
        pass
    for path in sys.path:
        if "site-packages" in path or "dist-packages" in path:
            _add(path)
    return paths


def _build_bootstrap_code() -> str:
    ksadk_root = str(Path(__file__).resolve().parents[2])
    site_paths = _current_site_package_paths()
    return (
        "import site, sys; "
        f"[site.addsitedir(p) for p in {site_paths!r} if p not in sys.path]; "
        f"sys.path.insert(0, {ksadk_root!r}); "
        "from ksadk.cli import main; main()"
    )


def reexec_with_project_venv_if_needed(
    agent_path: Path,
    command_args: Sequence[str],
    *,
    reexec_env: str = LOCAL_RUNTIME_VENV_REEXEC_ENV,
) -> None:
    """Re-run local agent commands inside the project's virtualenv when present."""
    if os.environ.get(reexec_env) or os.environ.get(LEGACY_WEB_VENV_REEXEC_ENV):
        return

    venv_python = find_project_venv_python(agent_path)
    if not venv_python or is_current_python(venv_python):
        return

    env = os.environ.copy()
    env[reexec_env] = "1"
    env["VIRTUAL_ENV"] = str(venv_python.parents[1])
    prepend_env_path(env, "PATH", str(venv_python.parent))

    args = [str(venv_python), "-c", _build_bootstrap_code(), *command_args]
    os.execvpe(str(venv_python), args, env)
