"""Managed DeepSeek Harness toolchain and standard bundle developer workflow.

KsADK does not copy, fork, or rebuild DeepSeek Harness.  It installs one
published CLI package into an isolated AgentEngine configuration directory and
delegates bundle composition to that exact executable.  Plugin source remains
an ordinary npm package that follows DSH's public ``dsh.bundle.patch`` format.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict

from ksadk.configs.global_config import get_global_config_dir
from ksadk.plugins.bridges.dsh import (
    DshBridgeError,
    DshProfilePluginBridge,
    dsh_subprocess_environment,
)

DSH_PACKAGE = "@deepseek-ai/dsh"
DSH_VERSION = "0.1.1-rc.2"
DSH_PACKAGE_SPEC = f"{DSH_PACKAGE}@{DSH_VERSION}"
CORDIS_VERSION_RANGE = "^4.0.1"
PNPM_VERSION = "11.7.0"

TOOLCHAIN_HOME_ENV = "AGENTENGINE_PLUGIN_TOOLCHAIN_HOME"
PNPM_BIN_ENV = "AGENTENGINE_PNPM_BIN"

_VERSION = re.compile(r"\b(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\b")
_PACKAGE_NAME = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$"
)
_LOCK_TIMEOUT_SECONDS = 30.0
_STALE_LOCK_SECONDS = 10 * 60
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _ToolchainModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )


class DshToolchainStatus(_ToolchainModel):
    package: Literal["@deepseek-ai/dsh"] = DSH_PACKAGE
    expected_version: Literal["0.1.1-rc.2"] = DSH_VERSION
    installed: bool
    usable: bool
    root: str
    executable: str | None = None
    actual_version: str | None = None
    lockfile_present: bool = False
    pnpm_available: bool = False
    pnpm_version: str | None = None
    problem: str | None = None


class DshPluginCreateResult(_ToolchainModel):
    ecosystem: Literal["dsh"] = "dsh"
    package_name: str
    target: str
    entrypoint: str
    bundle_patch: str


class DshPluginValidationResult(_ToolchainModel):
    ecosystem: Literal["dsh"] = "dsh"
    package_name: str
    package_version: str
    host_version: str
    profile_digest: str
    lifecycle: tuple[str, ...] = (
        "install",
        "project",
        "disable",
        "enable",
        "uninstall",
    )


class DshPluginPackResult(_ToolchainModel):
    ecosystem: Literal["dsh"] = "dsh"
    package_name: str
    package_version: str
    artifact: str


class DshToolchainError(RuntimeError):
    """Base error for the managed DSH development toolchain."""


class DshToolchainUnavailableError(DshToolchainError):
    pass


class DshToolchainVersionMismatchError(DshToolchainError):
    pass


class DshToolchainInstallError(DshToolchainError):
    pass


class DshPluginSourceError(DshToolchainError):
    pass


class DshPluginValidationError(DshToolchainError):
    def __init__(self, stage: str, message: str = "DSH plugin validation failed") -> None:
        super().__init__(message)
        self.stage = stage


class DshPluginPackError(DshToolchainError):
    pass


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], Path, Mapping[str, str]], CommandResult]


class DshToolchainManager:
    """Install and resolve the one supported published DSH CLI version."""

    def __init__(
        self,
        *,
        base_dir: Path | None = None,
        pnpm_command: Sequence[str] | None = None,
        command_runner: CommandRunner | None = None,
        lock_timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
    ) -> None:
        configured = os.environ.get(TOOLCHAIN_HOME_ENV, "").strip()
        base = (
            base_dir
            or (Path(configured).expanduser() if configured else None)
            or get_global_config_dir() / "plugin-toolchains"
        )
        self._base_dir = base.expanduser().resolve()
        self._root = self._base_dir / "dsh" / DSH_VERSION
        self._pnpm_command = tuple(pnpm_command) if pnpm_command is not None else None
        if self._pnpm_command is not None and not self._pnpm_command:
            raise ValueError("pnpm command cannot be empty")
        self._runner = command_runner or self._run_command
        self._lock_timeout_seconds = lock_timeout_seconds
        self._assert_safe_managed_root(self._root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def executable(self) -> Path:
        return self._root / "node_modules" / ".bin" / "dsh"

    def status(self) -> DshToolchainStatus:
        """Inspect the managed installation without mutating it."""

        pnpm_path, pnpm_version = self._inspect_pnpm()
        manifest_valid = self._manifest_is_pinned()
        lock_valid = self._lockfile_is_pinned()
        executable = self.executable
        if not manifest_valid:
            problem = "not_installed" if not self._root.exists() else "manifest_mismatch"
            return self._status(
                installed=False,
                usable=False,
                pnpm_path=pnpm_path,
                pnpm_version=pnpm_version,
                problem=problem,
            )
        if not lock_valid:
            return self._status(
                installed=True,
                usable=False,
                pnpm_path=pnpm_path,
                pnpm_version=pnpm_version,
                problem="lockfile_missing_or_mismatched",
            )
        try:
            resolved = executable.resolve(strict=True)
        except (FileNotFoundError, OSError):
            return self._status(
                installed=True,
                usable=False,
                pnpm_path=pnpm_path,
                pnpm_version=pnpm_version,
                problem="executable_missing",
            )
        if not resolved.is_relative_to(self._root.resolve()):
            return self._status(
                installed=True,
                usable=False,
                pnpm_path=pnpm_path,
                pnpm_version=pnpm_version,
                executable=str(resolved),
                problem="executable_outside_managed_root",
            )
        try:
            actual = self._command_version((str(executable),), cwd=self._root)
        except DshToolchainUnavailableError:
            return self._status(
                installed=True,
                usable=False,
                pnpm_path=pnpm_path,
                pnpm_version=pnpm_version,
                executable=str(executable),
                problem="executable_unavailable",
            )
        if actual != DSH_VERSION:
            return self._status(
                installed=True,
                usable=False,
                pnpm_path=pnpm_path,
                pnpm_version=pnpm_version,
                executable=str(executable),
                actual_version=actual,
                problem="version_mismatch",
            )
        return self._status(
            installed=True,
            usable=True,
            pnpm_path=pnpm_path,
            pnpm_version=pnpm_version,
            executable=str(executable),
            actual_version=actual,
        )

    def install(self) -> DshToolchainStatus:
        """Atomically install the pinned official npm package with a pnpm lock."""

        pnpm = self.require_pnpm()
        with self._install_lock():
            current = self.status()
            if current.usable:
                return current
            parent = self._root.parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._clean_abandoned_staging(parent)
            staging = Path(tempfile.mkdtemp(prefix=f".{DSH_VERSION}.install-", dir=parent))
            backup: Path | None = None
            try:
                self._write_install_manifest(staging)
                environment = self._installation_environment()
                self._runner(
                    (
                        *pnpm,
                        "install",
                        "--lockfile-only",
                        "--ignore-scripts",
                        "--config.auto-install-peers=true",
                    ),
                    staging,
                    environment,
                )
                lockfile = staging / "pnpm-lock.yaml"
                if not self._lockfile_is_pinned(lockfile):
                    raise DshToolchainInstallError(
                        "pnpm did not create a lockfile pinned to the supported DSH package"
                    )
                self._runner(
                    (
                        *pnpm,
                        "install",
                        "--frozen-lockfile",
                        "--ignore-scripts",
                        "--config.auto-install-peers=true",
                    ),
                    staging,
                    environment,
                )
                staged_executable = staging / "node_modules" / ".bin" / "dsh"
                resolved = staged_executable.resolve(strict=True)
                if not resolved.is_relative_to(staging.resolve()):
                    raise DshToolchainInstallError(
                        "installed DSH executable escapes the managed toolchain directory"
                    )
                actual = self._command_version((str(staged_executable),), cwd=staging)
                if actual != DSH_VERSION:
                    raise DshToolchainVersionMismatchError(
                        f"expected DSH {DSH_VERSION}, got {actual}"
                    )
                self._write_receipt(staging, actual)
                if self._root.exists():
                    backup = parent / f".{DSH_VERSION}.backup-{uuid.uuid4().hex}"
                    os.replace(self._root, backup)
                try:
                    os.replace(staging, self._root)
                    installed = self.status()
                    if not installed.usable:
                        raise DshToolchainInstallError(
                            f"installed DSH toolchain failed verification: {installed.problem}"
                        )
                except BaseException:
                    if self._root.exists():
                        shutil.rmtree(self._root)
                    if backup is not None and backup.exists():
                        os.replace(backup, self._root)
                    raise
                if backup is not None and backup.exists():
                    shutil.rmtree(backup)
                return installed
            except DshToolchainError:
                raise
            except Exception as error:
                raise DshToolchainInstallError("could not install the DSH toolchain") from error
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
                if backup is not None and backup.exists() and self._root.exists():
                    shutil.rmtree(backup)

    def require_command(self, explicit: str | Path | None = None) -> tuple[str, ...]:
        """Return an exact-version DSH executable, managed unless explicitly set."""

        if explicit is not None and str(explicit).strip():
            executable = self._resolve_program(str(explicit))
            actual = self._command_version((executable,), cwd=self._root)
            if actual != DSH_VERSION:
                raise DshToolchainVersionMismatchError(
                    f"expected DSH {DSH_VERSION}, got {actual}"
                )
            return (executable,)
        state = self.status()
        if state.problem == "version_mismatch":
            raise DshToolchainVersionMismatchError(
                f"expected DSH {DSH_VERSION}, got {state.actual_version or 'unknown'}"
            )
        if not state.usable or state.executable is None:
            raise DshToolchainUnavailableError(
                "managed DSH toolchain is not installed; run "
                "`agentengine plugin toolchain install`"
            )
        return (state.executable,)

    def resolve_module_entry(self, package_name: str) -> Path:
        """Resolve one dependency from the pinned DSH installation.

        Provider sidecars may need the exact Cordis implementation owned by
        their DSH host.  Resolution is anchored at the installed DSH package,
        never at a source checkout or the caller's ambient ``node_modules``.
        The result is fenced to the managed toolchain root.
        """

        self._validate_package_name(package_name)
        self.require_command()
        dsh_manifest = self._root / "node_modules" / DSH_PACKAGE / "package.json"
        try:
            dsh_manifest = dsh_manifest.resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise DshToolchainUnavailableError(
                "managed DSH package manifest is unavailable"
            ) from error
        node = self._resolve_program("node")
        script = (
            "const {createRequire}=require('node:module');"
            f"const resolve=createRequire({json.dumps(str(dsh_manifest))}).resolve;"
            f"process.stdout.write(resolve({json.dumps(package_name)}));"
        )
        try:
            result = self._invoke((node, "-e", script), cwd=self._root)
        except DshToolchainError as error:
            raise DshToolchainUnavailableError(
                f"managed DSH dependency is unavailable: {package_name}"
            ) from error
        raw = result.stdout.strip()
        try:
            resolved = Path(raw).resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise DshToolchainUnavailableError(
                f"managed DSH dependency is unavailable: {package_name}"
            ) from error
        if not resolved.is_relative_to(self._root):
            raise DshToolchainUnavailableError(
                f"managed DSH dependency escapes the toolchain root: {package_name}"
            )
        return resolved

    def require_pnpm(self) -> tuple[str, ...]:
        command = self._resolve_pnpm_command()
        result = self._runner(
            (*command, "--version"),
            self._pnpm_probe_cwd(),
            self._pnpm_environment(),
        )
        match = _VERSION.search(result.stdout or result.stderr)
        if match is None:
            raise DshToolchainUnavailableError("pnpm did not report a parseable version")
        if match.group(1) != PNPM_VERSION:
            raise DshToolchainVersionMismatchError(
                f"expected pnpm {PNPM_VERSION}, got {match.group(1)}"
            )
        return command

    def _status(
        self,
        *,
        installed: bool,
        usable: bool,
        pnpm_path: str | None,
        pnpm_version: str | None,
        problem: str | None = None,
        executable: str | None = None,
        actual_version: str | None = None,
    ) -> DshToolchainStatus:
        return DshToolchainStatus(
            installed=installed,
            usable=usable,
            root=str(self._root),
            executable=executable,
            actual_version=actual_version,
            lockfile_present=(self._root / "pnpm-lock.yaml").is_file(),
            pnpm_available=pnpm_path is not None,
            pnpm_version=pnpm_version,
            problem=problem,
        )

    def _inspect_pnpm(self) -> tuple[str | None, str | None]:
        try:
            command = self.require_pnpm()
            result = self._runner(
                (*command, "--version"),
                self._root,
                self._pnpm_environment(),
            )
        except DshToolchainError:
            return None, None
        match = _VERSION.search(result.stdout or result.stderr)
        return " ".join(command), match.group(1) if match else None

    def _resolve_pnpm_command(self) -> tuple[str, ...]:
        if self._pnpm_command is not None:
            command = self._pnpm_command
        else:
            configured = os.environ.get(PNPM_BIN_ENV, "").strip()
            if configured:
                command = (configured,)
            else:
                corepack = shutil.which("corepack")
                command = (
                    (corepack, f"pnpm@{PNPM_VERSION}")
                    if corepack is not None
                    else ("pnpm",)
                )
        executable = self._resolve_program(command[0])
        return (executable, *command[1:])

    def _pnpm_probe_cwd(self) -> Path:
        # require_pnpm() must also work before the managed toolchain root
        # exists (first install), so the version probe cannot unconditionally
        # use self._root as cwd.
        if self._root.exists():
            return self._root
        self._base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._base_dir

    def _manifest_is_pinned(self, path: Path | None = None) -> bool:
        manifest_path = path or self._root / "package.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("private") is True
            and payload.get("packageManager") == f"pnpm@{PNPM_VERSION}"
            and isinstance(payload.get("dependencies"), dict)
            and payload["dependencies"].get(DSH_PACKAGE) == DSH_VERSION
        )

    def _lockfile_is_pinned(self, path: Path | None = None) -> bool:
        lockfile = path or self._root / "pnpm-lock.yaml"
        try:
            if lockfile.stat().st_size > _MAX_MANIFEST_BYTES:
                return False
            value = lockfile.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeError):
            return False
        return DSH_PACKAGE in value and DSH_VERSION in value

    def _command_version(self, command: Sequence[str], *, cwd: Path) -> str:
        result = self._invoke((*command, "--version"), cwd=cwd)
        match = _VERSION.search(result.stdout or result.stderr)
        if match is None:
            raise DshToolchainUnavailableError("DSH did not report a parseable version")
        return match.group(1)

    def _invoke(self, command: Sequence[str], *, cwd: Path) -> CommandResult:
        try:
            return self._runner(tuple(command), cwd, dsh_subprocess_environment())
        except DshToolchainError:
            raise
        except Exception as error:
            raise DshToolchainUnavailableError("toolchain command could not be executed") from error

    @staticmethod
    def _resolve_program(value: str) -> str:
        candidate = Path(value).expanduser()
        if candidate.is_absolute() or len(candidate.parts) > 1:
            if not candidate.is_file():
                raise DshToolchainUnavailableError(f"required executable is unavailable: {value}")
            return str(candidate.resolve())
        resolved = shutil.which(value)
        if resolved is None:
            raise DshToolchainUnavailableError(f"required executable is unavailable: {value}")
        return resolved

    @staticmethod
    def _validate_package_name(name: str) -> None:
        if len(name) > 214 or not _PACKAGE_NAME.fullmatch(name):
            raise DshToolchainUnavailableError("invalid managed DSH package name")

    def _write_install_manifest(self, root: Path) -> None:
        self._write_json(
            root / "package.json",
            {
                "name": "agentengine-managed-dsh-toolchain",
                "private": True,
                "packageManager": f"pnpm@{PNPM_VERSION}",
                "dependencies": {DSH_PACKAGE: DSH_VERSION},
            },
        )

    def _write_receipt(self, root: Path, actual: str) -> None:
        self._write_json(
            root / "toolchain.json",
            {
                "schemaVersion": 1,
                "package": DSH_PACKAGE,
                "requestedVersion": DSH_VERSION,
                "actualVersion": actual,
                "packageManager": f"pnpm@{PNPM_VERSION}",
            },
        )

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _installation_environment() -> dict[str, str]:
        environment = dsh_subprocess_environment()
        environment.update(
            {
                "CI": "1",
                "COREPACK_ENABLE_PROJECT_SPEC": "0",
                "NPM_CONFIG_AUDIT": "false",
                "NPM_CONFIG_FUND": "false",
            }
        )
        return environment

    @staticmethod
    def _pnpm_environment() -> dict[str, str]:
        environment = dsh_subprocess_environment()
        environment["COREPACK_ENABLE_PROJECT_SPEC"] = "0"
        return environment

    @contextmanager
    def _install_lock(self) -> Iterator[None]:
        parent = self._root.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = parent / f".{DSH_VERSION}.install.lock"
        deadline = time.monotonic() + self._lock_timeout_seconds
        while True:
            try:
                lock.mkdir(mode=0o700)
                self._write_json(lock / "owner.json", {"pid": os.getpid()})
                break
            except FileExistsError:
                if self._lock_is_stale(lock):
                    try:
                        shutil.rmtree(lock)
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise DshToolchainInstallError(
                        "another DSH toolchain installation is still in progress"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            shutil.rmtree(lock, ignore_errors=True)

    @staticmethod
    def _lock_is_stale(lock: Path) -> bool:
        try:
            age = time.time() - lock.stat().st_mtime
        except FileNotFoundError:
            return False
        if age <= _STALE_LOCK_SECONDS:
            return False
        try:
            payload = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
            pid = int(payload.get("pid", -1))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return True
        if pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        return False

    @staticmethod
    def _clean_abandoned_staging(parent: Path) -> None:
        for candidate in parent.glob(f".{DSH_VERSION}.install-*"):
            if candidate.is_dir() and candidate.parent == parent:
                shutil.rmtree(candidate)

    def _assert_safe_managed_root(self, root: Path) -> None:
        if root.name != DSH_VERSION or root.parent.name != "dsh":
            raise ValueError("managed DSH root must end in dsh/<pinned-version>")
        if root in {Path(root.anchor), Path.home().resolve(), self._base_dir}:
            raise ValueError("managed DSH root is too broad")

    @staticmethod
    def _run_command(
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                env=dict(environment),
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DshToolchainUnavailableError("toolchain command did not complete") from error
        if completed.returncode != 0:
            diagnostic = _redact_diagnostic(completed.stderr or completed.stdout)
            raise DshToolchainInstallError(
                f"toolchain command failed with exit code {completed.returncode}"
                + (f": {diagnostic[-2000:]}" if diagnostic else "")
            )
        return CommandResult(stdout=completed.stdout, stderr=completed.stderr)


class DshPluginDeveloper:
    """Create, validate, and pack standard DSH npm bundle projects."""

    def __init__(
        self,
        *,
        toolchain: DshToolchainManager | None = None,
        explicit_dsh: str | Path | None = None,
        bridge_factory: Callable[..., DshProfilePluginBridge] = DshProfilePluginBridge,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self._toolchain = toolchain or DshToolchainManager(command_runner=command_runner)
        self._explicit_dsh = explicit_dsh
        self._bridge_factory = bridge_factory
        self._runner = command_runner or DshToolchainManager._run_command

    def create(self, target: Path, *, package_name: str | None = None) -> DshPluginCreateResult:
        target = target.expanduser().resolve()
        if target.exists():
            raise DshPluginSourceError("plugin target already exists")
        name = package_name or self._default_package_name(target.name)
        self._validate_package_name(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(mode=0o755)
        try:
            package = {
                "name": name,
                "version": "0.1.0",
                "description": "A DeepSeek Harness Cordis plugin bundle.",
                "type": "module",
                "main": "index.js",
                "types": "index.d.ts",
                "files": ["index.js", "index.d.ts", "cordis.patch.yml"],
                "engines": {"node": ">=22.19.0"},
                "peerDependencies": {"@deepseek-ai/cordis": CORDIS_VERSION_RANGE},
                "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
            }
            (target / "package.json").write_text(
                json.dumps(package, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (target / "cordis.patch.yml").write_text(
                "- insert:\n"
                "    - id: plugin-main\n"
                f"      name: {json.dumps(name)}\n",
                encoding="utf-8",
            )
            symbol = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-") or "plugin"
            (target / "index.js").write_text(
                f"export const name = {json.dumps(symbol)}\n\n"
                "/** @param {import('@deepseek-ai/cordis').Context} ctx */\n"
                "export function apply(ctx) {\n"
                "  // Register resources through Cordis effects so unload is reversible.\n"
                "  ctx.effect(() => () => {})\n"
                "}\n",
                encoding="utf-8",
            )
            (target / "index.d.ts").write_text(
                "import type { Context } from '@deepseek-ai/cordis'\n\n"
                "export declare const name: string\n"
                "export declare function apply(ctx: Context): void\n",
                encoding="utf-8",
            )
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return DshPluginCreateResult(
            package_name=name,
            target=str(target),
            entrypoint="index.js",
            bundle_patch="cordis.patch.yml",
        )

    def validate(self, source: str | Path) -> DshPluginValidationResult:
        local_source, _package = self._resolve_local_bundle(source)
        source_value = str(local_source) if local_source is not None else str(source).strip()
        command = self._toolchain.require_command(self._explicit_dsh)
        stage = "start"
        with tempfile.TemporaryDirectory(prefix="agentengine-dsh-validate-") as directory:
            root = Path(directory)
            try:
                with self._bridge_factory(
                    dsh_home=root / "home",
                    profile="validate",
                    dsh_command=command,
                    cwd=(local_source.parent if local_source is not None else Path.cwd()),
                ) as bridge:
                    stage = "install"
                    installed = bridge.install_plugin(
                        source_value,
                        accept_host_permissions=True,
                    )
                    stage = "project"
                    projection = bridge.project_profile()
                    stage = "disable"
                    bridge.set_enabled(installed.name, enabled=False)
                    stage = "enable"
                    bridge.set_enabled(installed.name, enabled=True)
                    stage = "uninstall"
                    bridge.uninstall_plugin(installed.name)
                    return DshPluginValidationResult(
                        package_name=installed.name,
                        package_version=installed.version,
                        host_version=projection.host_version,
                        profile_digest=projection.config_digest,
                    )
            except DshToolchainError:
                raise
            except DshBridgeError as error:
                raise DshPluginValidationError(stage) from error
            except Exception as error:
                raise DshPluginValidationError(stage) from error

    def pack(
        self,
        source: Path,
        *,
        output_dir: Path | None = None,
    ) -> DshPluginPackResult:
        local_source, package = self._resolve_local_bundle(source)
        assert local_source is not None
        if not local_source.is_dir():
            raise DshPluginSourceError("only a DSH bundle source directory can be packed")
        output = (output_dir or local_source / "dist").expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        before = {item.resolve() for item in output.glob("*.tgz") if item.is_file()}
        pnpm = self._toolchain.require_pnpm()
        environment = dsh_subprocess_environment()
        # Keep the pinned pnpm selected above even when an ancestor workspace
        # declares another package manager through Corepack.
        environment["COREPACK_ENABLE_PROJECT_SPEC"] = "0"
        try:
            self._runner(
                (*pnpm, "pack", "--pack-destination", str(output)),
                local_source,
                environment,
            )
        except DshToolchainError as error:
            raise DshPluginPackError("pnpm could not pack the DSH bundle") from error
        except Exception as error:
            raise DshPluginPackError("pnpm could not pack the DSH bundle") from error
        created = [
            item.resolve()
            for item in output.glob("*.tgz")
            if item.is_file() and item.resolve() not in before
        ]
        if len(created) != 1 or not created[0].is_relative_to(output):
            raise DshPluginPackError("pnpm pack did not produce exactly one bounded npm tarball")
        return DshPluginPackResult(
            package_name=str(package["name"]),
            package_version=str(package["version"]),
            artifact=str(created[0]),
        )

    @classmethod
    def _resolve_local_bundle(
        cls,
        source: str | Path,
    ) -> tuple[Path | None, dict[str, Any]]:
        raw = str(source).strip()
        candidate = Path(raw).expanduser()
        looks_local = isinstance(source, Path) or candidate.is_absolute() or raw.startswith(".")
        if not candidate.exists():
            if looks_local:
                raise DshPluginSourceError("local DSH plugin source does not exist")
            if not raw or raw.startswith("-") or any(char in raw for char in "\r\n\0"):
                raise DshPluginSourceError("invalid DSH plugin source")
            return None, {}
        root = candidate.resolve()
        if root.is_file() and root.name.endswith(".tgz"):
            return root, {}
        if root.is_file() and root.name == "package.json":
            root = root.parent
        if not root.is_dir():
            raise DshPluginSourceError("local DSH plugin source must be a directory")
        manifest_path = root / "package.json"
        try:
            if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
                raise DshPluginSourceError("DSH plugin package.json is too large")
            package = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise DshPluginSourceError("DSH plugin package.json is missing") from None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DshPluginSourceError("DSH plugin package.json is invalid") from error
        if not isinstance(package, dict):
            raise DshPluginSourceError("DSH plugin package.json must be an object")
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str) or not version.strip():
            raise DshPluginSourceError("DSH plugin package name/version is invalid")
        cls._validate_package_name(name)
        dsh = package.get("dsh")
        bundle = dsh.get("bundle") if isinstance(dsh, dict) else None
        patch = bundle.get("patch") if isinstance(bundle, dict) else None
        if not isinstance(patch, str) or not patch.strip():
            raise DshPluginSourceError("package.json must declare dsh.bundle.patch")
        relative = Path(patch)
        target = (root / relative).resolve()
        if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(root):
            raise DshPluginSourceError("dsh.bundle.patch must stay inside the package")
        if not target.is_file():
            raise DshPluginSourceError("declared DSH bundle patch does not exist")
        return root, package

    @staticmethod
    def _default_package_name(target_name: str) -> str:
        value = re.sub(r"[^a-z0-9._-]+", "-", target_name.casefold()).strip("-._")
        if not value:
            value = "plugin"
        return value if value.startswith("dsh-") else f"dsh-{value}"

    @staticmethod
    def _validate_package_name(name: str) -> None:
        if len(name) > 214 or not _PACKAGE_NAME.fullmatch(name):
            raise DshPluginSourceError("plugin name must be a valid lowercase npm package name")


__all__ = [
    "CORDIS_VERSION_RANGE",
    "DSH_PACKAGE",
    "DSH_PACKAGE_SPEC",
    "DSH_VERSION",
    "PNPM_BIN_ENV",
    "PNPM_VERSION",
    "TOOLCHAIN_HOME_ENV",
    "CommandResult",
    "DshPluginCreateResult",
    "DshPluginDeveloper",
    "DshPluginPackError",
    "DshPluginPackResult",
    "DshPluginSourceError",
    "DshPluginValidationError",
    "DshPluginValidationResult",
    "DshToolchainError",
    "DshToolchainInstallError",
    "DshToolchainManager",
    "DshToolchainStatus",
    "DshToolchainUnavailableError",
    "DshToolchainVersionMismatchError",
]


def _redact_diagnostic(value: str) -> str:
    value = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[redacted]@", value, flags=re.I)
    return re.sub(
        r"((?:token|password|authorization|_authToken)\s*[:=]\s*)[^\s]+",
        r"\1[redacted]",
        value,
        flags=re.I,
    ).strip()
