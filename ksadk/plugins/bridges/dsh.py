"""Transactional DeepSeek Harness profile plugin bridge.

DSH remains the native owner of its plugin packages and composed runtime.  This
module only manages one isolated Profile through the public ``dsh plugin`` and
``--dump-config`` commands, then projects non-secret inventory into KsADK.
It deliberately does not import Cordis or execute DSH plugin code in PluginHost.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal, TypedDict

try:
    import fcntl
except ImportError:  # pragma: no cover - DSH production hosts are Unix
    fcntl = None  # type: ignore[assignment]

import yaml
from pydantic import BaseModel, ConfigDict

from ksadk.plugins.dsh_installation_digest import (
    DshInstallationDigestError,
    installation_digest,
)

_COMMAND_TIMEOUT_SECONDS = 120


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _DshModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )


class DshBridgeHost(_DshModel):
    host_id: Literal["deepseek-harness"] = "deepseek-harness"
    version: str
    protocol: Literal["dsh.profile/v1"] = "dsh.profile/v1"
    available: Literal[True] = True


class DshPluginInventory(_DshModel):
    ecosystem: Literal["dsh"] = "dsh"
    integration_mode: Literal["bridged"] = "bridged"
    profile: str
    name: str
    display_name: str
    description: str = ""
    version: str
    requested_spec: str
    source_digest: str | None = None
    source_kind: Literal["directory", "tgz"] | None = None
    client_extension: bool = False
    settings_integration: bool = False
    installed: Literal[True] = True
    enabled: bool
    permissions_declared: Literal[False] = False
    risk_disclosures: tuple[str, ...] = (
        "DSH packages and install scripts run with the native host user privileges.",
        "DSH bundle manifests do not declare a complete runtime permission set.",
    )


class DshProfileProjection(_DshModel):
    profile: str
    bundles: tuple[str, ...]
    config_digest: str
    config_bytes: int
    host_version: str


class DshProfileBuildSnapshot(_DshModel):
    """Credential-free identity of the actual local profile execution inputs."""

    projection: DshProfileProjection
    dependency_lock_digest: str
    installation_digest: str

    @property
    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


class DshBridgeError(RuntimeError):
    """Base failure for one bounded DSH profile operation."""


class DshHostUnavailableError(DshBridgeError):
    pass


class DshPluginNotFoundError(DshBridgeError):
    pass


class DshPluginApprovalRequired(DshBridgeError):
    pass


class DshPluginMutationError(DshBridgeError):
    pass


class DshProfileRecoveryError(DshPluginMutationError):
    """The owner must keep run admission suspended until profile recovery."""


class _CommandResult(_DshModel):
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], Path, Mapping[str, str]], _CommandResult]

_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PACKAGE_NAME = re.compile(r"^(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+$")
_PINNED_REGISTRY_SOURCE = re.compile(
    r"^(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+@"
    r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_HOST_VERSION = re.compile(r"\b(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\b")
_STATE_FILE = ".ksadk-dsh-plugins.json"
_IMMUTABLE_SOURCE_DIR = "immutable-plugin-sources"
_SNAPSHOT_FILES = ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", _STATE_FILE)
_WEB_PROFILE_BUILTINS = ("@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app")
_MAX_JSON_BYTES = 2 * 1024 * 1024
_PROFILE_LOCK_DIR = "profile-locks"
_DSH_SUBPROCESS_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NPM_CONFIG_REGISTRY",
    "npm_config_registry",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)


def validate_dsh_registry_source(source: str) -> str:
    """Return one exact, credential-free npm registry package coordinate."""

    value = source.strip()
    if not _PINNED_REGISTRY_SOURCE.fullmatch(value):
        raise ValueError(
            "DSH registry source must be <package>@<exact-semver>; "
            "Git URLs, tags, and ranges are not allowed"
        )
    return value


def validate_dsh_registry_request(source: str) -> str:
    """Accept a package name for latest resolution, or an exact coordinate."""
    value = source.strip()
    if len(value) <= 214 and not value.startswith("-") and _PACKAGE_NAME.fullmatch(value):
        return value
    return validate_dsh_registry_source(value)


def dsh_subprocess_environment(*, dsh_home: Path | None = None) -> dict[str, str]:
    """Build the explicit environment inherited by DSH and pnpm children.

    DSH bundles may execute package lifecycle scripts.  They need process
    discovery, a home/temp directory, locale, proxies and CA configuration,
    but never receive arbitrary model, cloud, npm, SSH or host application
    credentials from the parent process.
    """

    environment = {
        name: os.environ[name] for name in _DSH_SUBPROCESS_ENV_KEYS if os.environ.get(name)
    }
    if "PATH" not in environment:
        environment["PATH"] = os.defpath
    if dsh_home is not None:
        environment["DSH_HOME"] = str(dsh_home)
    return environment


class _SourceReceipt(TypedDict):
    digest: str
    kind: Literal["directory", "tgz"]
    artifact: str
    dependency_spec: str


class _ProfileState(TypedDict):
    order: list[str]
    disabled: list[str]
    sources: dict[str, _SourceReceipt]


@dataclass(frozen=True)
class _PreparedSource:
    command_source: str
    digest: str
    kind: Literal["directory", "tgz"]
    artifact: str


@dataclass(frozen=True)
class _ProfileSnapshot:
    files: Mapping[str, bytes | None]
    node_modules_existed: bool


class DshProfilePluginBridge:
    """Manage DSH bundles in one isolated Profile with rollback and preflight."""

    def __init__(
        self,
        *,
        dsh_home: Path,
        profile: str = "web",
        dsh_command: Sequence[str] | None = None,
        command_runner: CommandRunner | None = None,
        cwd: Path | None = None,
    ) -> None:
        if not _PROFILE_NAME.fullmatch(profile):
            raise ValueError("DSH profile must be a simple name without path separators")
        if dsh_command is not None and not dsh_command:
            raise ValueError("DSH command cannot be empty")
        self._dsh_home = dsh_home.expanduser().resolve()
        self._profile = profile
        self._profile_root = self._dsh_home / "profiles" / profile
        self._command = tuple(dsh_command) if dsh_command is not None else None
        self._runner = command_runner or self._run_command
        self._cwd = (cwd or Path.cwd()).resolve()
        self._host: DshBridgeHost | None = None
        self._lock = threading.RLock()
        self._transaction_local = threading.local()

    @property
    def host(self) -> DshBridgeHost:
        if self._host is None:
            raise DshHostUnavailableError("DSH bridge is not started")
        return self._host

    def start(self) -> DshBridgeHost:
        if self._host is not None:
            return self._host
        command = self._resolve_command()
        result = self._invoke((*command, "--version"), cwd=self._cwd)
        match = _HOST_VERSION.search(result.stdout or result.stderr)
        if match is None:
            raise DshHostUnavailableError("DSH host did not report a parseable version")
        self._command = command
        self._host = DshBridgeHost(version=match.group(1))
        return self._host

    def close(self) -> None:
        self._host = None

    def __enter__(self) -> "DshProfilePluginBridge":
        self.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def list_plugins(self) -> tuple[DshPluginInventory, ...]:
        with self._profile_transaction(exclusive=False):
            return self._list_plugins_locked()

    def _list_plugins_locked(self) -> tuple[DshPluginInventory, ...]:
        self._ensure_started()
        if not self._manifest_path().is_file():
            return ()
        manifest = self._read_manifest()
        state = self._read_state(manifest)
        self._verify_source_receipts(manifest, state)
        active = set(self._bundles(manifest))
        items: list[DshPluginInventory] = []
        for name, requested_spec in self._dependencies(manifest).items():
            package = self._read_package(name)
            if package is None or self._bundle_patch(package) is None:
                continue
            dsh = package.get("dsh")
            client = dsh.get("client") if isinstance(dsh, dict) else None
            inject = client.get("inject") if isinstance(client, dict) else None
            client_inject = (
                tuple(item for item in inject if isinstance(item, str))
                if isinstance(inject, list)
                else ()
            )
            receipt = state["sources"].get(name)
            items.append(
                DshPluginInventory(
                    profile=self._profile,
                    name=name,
                    display_name=self._string(package.get("displayName"))
                    or self._string(package.get("name"))
                    or name,
                    description=self._string(package.get("description")),
                    version=self._string(package.get("version")),
                    requested_spec=requested_spec,
                    source_digest=receipt["digest"] if receipt is not None else None,
                    source_kind=receipt["kind"] if receipt is not None else None,
                    client_extension=isinstance(client, dict),
                    settings_integration=any(
                        "client-ui-settings" in item or "client-ui-slots" in item
                        for item in client_inject
                    ),
                    enabled=name in active and name not in set(state["disabled"]),
                )
            )
        return tuple(sorted(items, key=lambda item: (item.display_name.casefold(), item.name)))

    def get_plugin(self, name: str) -> DshPluginInventory:
        self._validate_package_name(name)
        matches = [item for item in self.list_plugins() if item.name == name]
        if not matches:
            raise DshPluginNotFoundError(f"DSH plugin {name!r} is not installed")
        return matches[0]

    def install_plugin(
        self,
        source: str,
        *,
        accept_host_permissions: bool = False,
    ) -> DshPluginInventory:
        if not accept_host_permissions:
            raise DshPluginApprovalRequired(
                "DSH packages can run install scripts and runtime code with host privileges; "
                "explicit approval is required"
            )
        self._validate_source(source)
        source = self._resolve_install_source(source.strip())
        with self._profile_transaction(exclusive=True):
            self._require_package_mutation_rollback(new_profile_allowed=True)
            snapshot = self._snapshot()
            before = (
                self._dependencies(self._read_manifest()) if self._manifest_path().is_file() else {}
            )
            try:
                if self._manifest_path().is_file():
                    existing_manifest = self._read_manifest()
                    self._verify_source_receipts(
                        existing_manifest, self._read_state(existing_manifest)
                    )
                prepared = self._prepare_source(source)
                self._plugin_command("add", prepared.command_source)
                manifest = self._read_manifest()
                added = [name for name in self._dependencies(manifest) if name not in before]
                if len(added) != 1:
                    raise DshPluginMutationError(
                        "installing one DSH plugin must add exactly one direct dependency"
                    )
                name = added[0]
                self._require_bundle(name)
                state = self._read_state(manifest)
                state["order"] = [item for item in state["order"] if item != name] + [name]
                if name not in state["disabled"]:
                    state["disabled"].append(name)
                if prepared.digest:
                    state["sources"][name] = {
                        "digest": prepared.digest,
                        "kind": prepared.kind,
                        "artifact": prepared.artifact,
                        "dependency_spec": self._dependencies(manifest)[name],
                    }
                self._write_state(state)
                self._write_active_bundles(manifest, state)
                self._preflight()
                return self.get_plugin(name)
            except BaseException as error:
                self._rollback(snapshot, error)
                raise

    def set_enabled(self, name: str, *, enabled: bool) -> DshPluginInventory:
        self._validate_package_name(name)
        with self._profile_transaction(exclusive=True):
            snapshot = self._snapshot()
            try:
                manifest = self._read_manifest()
                if name not in self._dependencies(manifest):
                    raise DshPluginNotFoundError(f"DSH plugin {name!r} is not installed")
                self._require_bundle(name)
                state = self._read_state(manifest)
                self._verify_source_receipts(manifest, state, names=(name,))
                if name not in state["order"]:
                    state["order"].append(name)
                if enabled:
                    state["disabled"] = [item for item in state["disabled"] if item != name]
                elif name not in state["disabled"]:
                    state["disabled"].append(name)
                self._write_state(state)
                self._write_active_bundles(manifest, state)
                self._preflight()
                return self.get_plugin(name)
            except BaseException as error:
                self._rollback(snapshot, error)
                raise

    def update_plugin(
        self,
        name: str,
        *,
        source: str | None = None,
        accept_host_permissions: bool = False,
    ) -> DshPluginInventory:
        self._validate_package_name(name)
        if not accept_host_permissions:
            raise DshPluginApprovalRequired(
                "updating a DSH package requires host permission approval"
            )
        with self._profile_transaction(exclusive=True):
            self._require_package_mutation_rollback(new_profile_allowed=False)
            snapshot = self._snapshot()
            try:
                self.get_plugin(name)
                before_manifest = self._read_manifest()
                before_state = self._read_state(before_manifest)
                self._verify_source_receipts(before_manifest, before_state, names=(name,))
                existing_source = before_state["sources"].get(name)
                if existing_source is not None and source is None:
                    raise DshPluginMutationError(
                        "updating an immutable local DSH plugin requires an explicit source"
                    )
                prepared = None
                if source is not None:
                    self._validate_source(source)
                    prepared = self._prepare_source(source)
                    if not prepared.digest:
                        raise DshPluginMutationError(
                            "an explicit update source must be a local directory or tgz"
                        )
                    self._plugin_command("add", prepared.command_source)
                else:
                    self._plugin_command("update", name)
                manifest = self._read_manifest()
                if prepared is not None:
                    changed = {
                        dependency
                        for dependency in (
                            set(self._dependencies(before_manifest))
                            | set(self._dependencies(manifest))
                        )
                        if self._dependencies(before_manifest).get(dependency)
                        != self._dependencies(manifest).get(dependency)
                    }
                    if changed != {name}:
                        raise DshPluginMutationError(
                            "updated local source must replace exactly the selected plugin"
                        )
                self._require_bundle(name)
                state = self._read_state(manifest)
                if prepared is not None:
                    if name not in self._dependencies(manifest):
                        raise DshPluginMutationError(
                            "updated local source did not preserve the plugin package name"
                        )
                    state["sources"][name] = {
                        "digest": prepared.digest,
                        "kind": prepared.kind,
                        "artifact": prepared.artifact,
                        "dependency_spec": self._dependencies(manifest)[name],
                    }
                    self._write_state(state)
                self._write_active_bundles(manifest, state)
                self._verify_source_receipts(manifest, state, names=(name,))
                self._preflight()
                return self.get_plugin(name)
            except BaseException as error:
                self._rollback(snapshot, error)
                raise

    def uninstall_plugin(self, name: str) -> None:
        self._validate_package_name(name)
        with self._profile_transaction(exclusive=True):
            self._require_package_mutation_rollback(new_profile_allowed=False)
            snapshot = self._snapshot()
            try:
                self.get_plugin(name)
                self._plugin_command("remove", name)
                manifest = self._read_manifest()
                state = self._read_state(manifest)
                state["order"] = [item for item in state["order"] if item != name]
                state["disabled"] = [item for item in state["disabled"] if item != name]
                state["sources"].pop(name, None)
                self._write_state(state)
                self._write_active_bundles(manifest, state)
                self._preflight()
                if any(item.name == name for item in self.list_plugins()):
                    raise DshPluginMutationError("DSH host still reports the removed plugin")
            except BaseException as error:
                self._rollback(snapshot, error)
                raise

    def project_profile(self) -> DshProfileProjection:
        """Validate the native profile and expose only its digest, never raw config."""

        with self._profile_transaction(exclusive=False):
            self._ensure_started()
            manifest = self._read_manifest()
            self._verify_source_receipts(manifest, self._read_state(manifest))
            result = self._preflight()
            payload = result.stdout.encode("utf-8")
            return DshProfileProjection(
                profile=self._profile,
                bundles=tuple(self._bundles(manifest)),
                config_digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
                config_bytes=len(payload),
                host_version=self.host.version,
            )

    def migrate_to_isolated_layout(
        self,
        *,
        accept_host_permissions: bool = False,
        recover_external_dependency_links: bool = False,
    ) -> None:
        """Migrate a stopped profile; the caller owns Core/admission suspension.

        Keep an independent byte copy for rollback: a frozen reinstall of the
        old layout can itself hit the pinned pnpm shutdown defect. Studio-owned
        profiles may also repair an isolated tree polluted by a legacy Core
        module-fallback cache.
        """
        if not accept_host_permissions:
            raise DshPluginApprovalRequired("Profile migration may run package install scripts")
        with self._profile_transaction(exclusive=True):
            if (
                not (self._profile_root / "pnpm-lock.yaml").exists()
                and self._is_pristine_official_web_profile()
            ):
                # Core may have initialized the empty profile while serving
                # its first page. There is no dependency tree to migrate yet.
                settings_path = self._profile_root / "pnpm-workspace.yaml"
                settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
                if not isinstance(settings, dict):
                    raise DshPluginMutationError("Invalid package-manager settings")
                settings["nodeLinker"] = "isolated"
                settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
                settings_path.chmod(0o600)
                return
            self._require_package_mutation_rollback(new_profile_allowed=False)
            manifest = self._read_manifest()
            self._verify_source_receipts(manifest, self._read_state(manifest))
            settings_path = self._profile_root / "pnpm-workspace.yaml"
            settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                raise DshPluginMutationError("Invalid package-manager settings")
            node_linker = settings.get("nodeLinker")
            if node_linker not in {"hoisted", "isolated"}:
                raise DshPluginMutationError(
                    "Only hoisted or isolated profiles can use this migration"
                )
            modules = self._profile_root / "node_modules"
            if modules.is_symlink() or not modules.is_dir():
                raise DshPluginMutationError("Profile installation is not a real directory")
            # A normal migration only accepts a closed tree. Studio may repair
            # its own legacy hoisted Profile when pnpm linked a dependency to
            # an ambient store: move that tree atomically instead of reading or
            # following the external target, then reinstall from the frozen lock.
            moved_unsafe_tree = False
            try:
                original_digest = installation_digest(modules)
            except DshInstallationDigestError as error:
                if (
                    not recover_external_dependency_links
                    or str(error) != "DSH dependency link escapes the installed tree"
                ):
                    raise
                original_digest = None
                moved_unsafe_tree = True
            if node_linker == "isolated" and not moved_unsafe_tree:
                self._preflight()
                return
            fallback = self._profile_root / ".dsh-module-fallback"
            if fallback.exists() and (fallback.is_symlink() or not fallback.is_dir()):
                raise DshPluginMutationError("DSH module fallback is not a real directory")
            snapshot = self._snapshot()
            root = tempfile.mkdtemp(prefix=".layout-backup-", dir=self._profile_root)
            preserve_backup = False
            try:
                backup = Path(root) / "node_modules"
                fallback_backup = Path(root) / "module-fallback"
                if moved_unsafe_tree:
                    os.replace(modules, backup)
                else:
                    shutil.copytree(modules, backup, symlinks=True)
                    if installation_digest(backup) != original_digest:
                        raise DshPluginMutationError("Profile installation backup did not verify")
                fallback_moved = fallback.is_dir()
                if fallback_moved:
                    os.replace(fallback, fallback_backup)
                original_files = Path(root) / "profile-files"
                original_files.mkdir(mode=0o700)
                for name, content in snapshot.files.items():
                    if content is not None:
                        original_path = original_files / name
                        original_path.write_bytes(content)
                        original_path.chmod(0o600)
                try:
                    if not moved_unsafe_tree:
                        shutil.rmtree(modules)
                    settings["nodeLinker"] = "isolated"
                    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
                    settings_path.chmod(0o600)
                    self._plugin_command("install", "--frozen-lockfile")
                    current_lock = (self._profile_root / "pnpm-lock.yaml").read_bytes()
                    if current_lock != snapshot.files["pnpm-lock.yaml"]:
                        raise DshPluginMutationError("Layout migration changed the dependency lock")
                    # Native reconcile can re-enable previously disabled bundles.
                    for name in ("package.json", _STATE_FILE):
                        content = snapshot.files[name]
                        path = self._profile_root / name
                        if content is None:
                            path.unlink(missing_ok=True)
                        else:
                            path.write_bytes(content)
                            path.chmod(0o600)
                    self._verify_source_receipts(manifest, self._read_state(manifest))
                    self._preflight()
                except BaseException as error:
                    try:
                        if modules.is_symlink():
                            modules.unlink()
                        elif modules.exists():
                            shutil.rmtree(modules)
                        os.replace(backup, modules)
                        if fallback.is_symlink():
                            fallback.unlink()
                        elif fallback.exists():
                            shutil.rmtree(fallback)
                        if fallback_moved:
                            os.replace(fallback_backup, fallback)
                        self._rollback(snapshot, error, reinstall=False)
                    except BaseException as recovery_error:
                        preserve_backup = True
                        raise DshProfileRecoveryError(
                            "Profile layout recovery failed; retain the layout backup for recovery"
                        ) from recovery_error
                    raise
            finally:
                if not preserve_backup:
                    shutil.rmtree(root)

    def snapshot_for_build(self) -> DshProfileBuildSnapshot:
        """Capture exact local inputs under the same profile mutation lock."""
        with self._profile_transaction(exclusive=False):
            projection = self.project_profile()
            lock = self._profile_root / "pnpm-lock.yaml"
            if lock.is_symlink() or not lock.is_file() or lock.stat().st_size > 16 * 1024 * 1024:
                raise DshPluginMutationError("DSH Build requires a bounded dependency lockfile")
            return DshProfileBuildSnapshot(
                projection=projection,
                dependency_lock_digest="sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest(),
                installation_digest=installation_digest(self._profile_root / "node_modules"),
            )

    def verify_build_snapshot(self, expected: DshProfileBuildSnapshot) -> None:
        if self.snapshot_for_build() != expected:
            raise DshPluginMutationError("DSH installed profile no longer matches the Build")

    def _resolve_command(self) -> tuple[str, ...]:
        if self._command is not None:
            return self._command
        executable = shutil.which("dsh")
        if executable is None:
            raise DshHostUnavailableError(
                "DSH host is not installed; configure an exact DSH executable "
                "before using the bridge"
            )
        return (executable,)

    def _ensure_started(self) -> None:
        if self._host is None or self._command is None:
            raise DshHostUnavailableError("DSH bridge is not started")

    @contextmanager
    def _profile_transaction(self, *, exclusive: bool) -> Iterator[None]:
        """Serialize one Profile snapshot across bridge objects and processes."""

        with self._lock:
            depth = int(getattr(self._transaction_local, "depth", 0))
            held_exclusive = bool(getattr(self._transaction_local, "exclusive", False))
            if depth:
                if exclusive and not held_exclusive:
                    raise DshPluginMutationError(
                        "cannot upgrade a DSH profile read transaction to a mutation"
                    )
                self._transaction_local.depth = depth + 1
                try:
                    yield
                finally:
                    self._transaction_local.depth = depth
                return

            if fcntl is None:
                raise DshHostUnavailableError("DSH profile transactions require Unix file locking")
            lock_root = self._dsh_home / _PROFILE_LOCK_DIR
            lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if lock_root.resolve() != lock_root:
                raise DshPluginMutationError("DSH profile lock directory is not trusted")
            lock_path = lock_root / f"{self._profile}.lock"
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(lock_path, flags, 0o600)
            except OSError as error:
                raise DshPluginMutationError(
                    "DSH profile transaction lock is unavailable"
                ) from error
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise DshPluginMutationError(
                        "DSH profile transaction lock is not a regular file"
                    )
                os.fchmod(descriptor, 0o600)
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
                )
                self._transaction_local.depth = 1
                self._transaction_local.exclusive = exclusive
                try:
                    yield
                finally:
                    self._transaction_local.depth = 0
                    self._transaction_local.exclusive = False
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _invoke(self, command: Sequence[str], *, cwd: Path) -> _CommandResult:
        environment = dsh_subprocess_environment(dsh_home=self._dsh_home)
        try:
            return self._runner(tuple(command), cwd, environment)
        except DshBridgeError:
            raise
        except Exception as error:
            raise DshHostUnavailableError("DSH host command could not be executed") from error

    def _plugin_command(self, verb: str, value: str) -> _CommandResult:
        self._ensure_started()
        assert self._command is not None
        initializing = verb == "add" and not self._manifest_path().exists()
        # The pinned pnpm hoisted linker can start fetch workers after its
        # shutdown pass during removal. Use its supported isolated layout for
        # newly owned profiles, without changing an existing profile's policy.
        options = ("--config.node-linker=isolated",) if initializing else ()
        result = self._invoke(
            (*self._command, "plugin", "--profile", self._profile, verb, value, *options),
            cwd=self._cwd,
        )
        if initializing:
            settings_path = self._profile_root / "pnpm-workspace.yaml"
            settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                raise DshPluginMutationError("DSH initialized invalid package-manager settings")
            settings["nodeLinker"] = "isolated"
            # This is a newly created profile inside the install transaction;
            # any subsequent failure removes it through the existing rollback.
            settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
            settings_path.chmod(0o600)
        return result

    def _preflight(self) -> _CommandResult:
        self._ensure_started()
        assert self._command is not None
        return self._invoke(
            (*self._command, "--profile", self._profile, "--dump-config"),
            cwd=self._cwd,
        )

    def _manifest_path(self) -> Path:
        return self._profile_root / "package.json"

    def _read_manifest(self) -> dict[str, object]:
        return self._read_json(self._manifest_path(), required=True)

    def _read_package(self, name: str) -> dict[str, object] | None:
        return self._read_json(
            self._profile_root / "node_modules" / Path(*name.split("/")) / "package.json",
            required=False,
        )

    @staticmethod
    def _read_json(path: Path, *, required: bool) -> dict[str, object] | None:
        try:
            if path.stat().st_size > _MAX_JSON_BYTES:
                raise DshBridgeError("DSH manifest exceeds the supported size limit")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if required:
                raise DshBridgeError("required DSH profile manifest is unavailable") from None
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DshBridgeError("DSH profile contains an unreadable JSON manifest") from error
        if not isinstance(payload, dict):
            raise DshBridgeError("DSH JSON manifest must be an object")
        return payload

    @staticmethod
    def _dependencies(manifest: Mapping[str, object]) -> dict[str, str]:
        raw = manifest.get("dependencies")
        if raw is None:
            return {}
        if not isinstance(raw, dict) or any(
            not isinstance(name, str) or not isinstance(value, str) for name, value in raw.items()
        ):
            raise DshBridgeError("DSH profile dependencies are invalid")
        return dict(raw)

    @staticmethod
    def _bundles(manifest: Mapping[str, object]) -> list[str]:
        dsh = manifest.get("dsh")
        profile = dsh.get("profile") if isinstance(dsh, dict) else None
        bundles = profile.get("bundles") if isinstance(profile, dict) else None
        if bundles is None:
            return []
        if not isinstance(bundles, list) or any(not isinstance(item, str) for item in bundles):
            raise DshBridgeError("DSH profile bundle order is invalid")
        return list(bundles)

    @staticmethod
    def _bundle_patch(package: Mapping[str, object]) -> str | None:
        dsh = package.get("dsh")
        bundle = dsh.get("bundle") if isinstance(dsh, dict) else None
        patch = bundle.get("patch") if isinstance(bundle, dict) else None
        return patch.strip() if isinstance(patch, str) and patch.strip() else None

    def _require_bundle(self, name: str) -> None:
        package = self._read_package(name)
        if package is None:
            raise DshPluginMutationError(f"{name} does not declare dsh.bundle.patch")
        patch = self._bundle_patch(package)
        root = (self._profile_root / "node_modules" / Path(*name.split("/"))).resolve()
        if patch is None:
            # Real upstream plugins may ship without a dsh.bundle.patch yet
            # still be valid Cordis tool plugins (inject: ['tools']). Generate
            # a minimal bundle patch so the dsh profile loader activates them.
            # This is a compatibility shim, not a source modification: the
            # plugin's own code is untouched.
            patch_path = root / "cordis.patch.yml"
            patch_path.write_text(
                "- insert:\n    - id: "
                + name.rsplit("/", 1)[-1].replace("-", "_")
                + "\n      name: '" + name + "'\n",
                encoding="utf-8",
            )
            package.setdefault("dsh", {}).setdefault("bundle", {})["patch"] = "./cordis.patch.yml"
            self._write_json(root / "package.json", package)
            patch = "./cordis.patch.yml"
        relative = Path(patch)
        if relative.is_absolute() or ".." in relative.parts:
            raise DshPluginMutationError(f"{name} declares an unsafe bundle patch path")
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise DshPluginMutationError(f"{name} bundle patch is unavailable")

    def _read_state(self, manifest: Mapping[str, object]) -> _ProfileState:
        dependencies = set(self._dependencies(manifest))
        bundle_dependencies = [
            name
            for name in dependencies
            if self._bundle_patch(self._read_package(name) or {}) is not None
        ]
        active = [name for name in self._bundles(manifest) if name in dependencies]
        stored = self._read_json(self._profile_root / _STATE_FILE, required=False) or {}
        order_raw = stored.get("order")
        disabled_raw = stored.get("disabled")
        sources_raw = stored.get("sources")
        order = (
            [item for item in order_raw if isinstance(item, str) and item in bundle_dependencies]
            if isinstance(order_raw, list)
            else []
        )
        for name in [*active, *bundle_dependencies]:
            if name not in order:
                order.append(name)
        disabled = (
            [item for item in disabled_raw if isinstance(item, str) and item in bundle_dependencies]
            if isinstance(disabled_raw, list)
            else [name for name in bundle_dependencies if name not in active]
        )
        sources: dict[str, _SourceReceipt] = {}
        if isinstance(sources_raw, dict):
            for name, raw in sources_raw.items():
                if name not in bundle_dependencies:
                    continue
                if not isinstance(raw, dict):
                    raise DshPluginMutationError("DSH local source receipt is invalid")
                digest = raw.get("digest")
                kind = raw.get("kind")
                artifact = raw.get("artifact")
                dependency_spec = raw.get("dependencySpec") or raw.get("dependency_spec")
                if (
                    isinstance(digest, str)
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                    and kind in {"directory", "tgz"}
                    and isinstance(artifact, str)
                    and isinstance(dependency_spec, str)
                ):
                    sources[name] = {
                        "digest": digest,
                        "kind": kind,
                        "artifact": artifact,
                        "dependency_spec": dependency_spec,
                    }
                else:
                    raise DshPluginMutationError("DSH local source receipt is invalid")
        return {"order": order, "disabled": disabled, "sources": sources}

    def _write_state(self, state: _ProfileState) -> None:
        self._write_json(
            self._profile_root / _STATE_FILE,
            {
                "version": 2,
                "order": list(state["order"]),
                "disabled": list(state["disabled"]),
                "sources": {
                    name: {
                        "digest": receipt["digest"],
                        "kind": receipt["kind"],
                        "artifact": receipt["artifact"],
                        "dependencySpec": receipt["dependency_spec"],
                    }
                    for name, receipt in sorted(state["sources"].items())
                },
            },
        )

    def _write_active_bundles(
        self,
        manifest: dict[str, object],
        state: _ProfileState,
    ) -> None:
        dependencies = set(self._dependencies(manifest))
        existing = self._bundles(manifest)
        builtins = [name for name in existing if name not in dependencies]
        disabled = set(state["disabled"])
        enabled = [name for name in state["order"] if name in dependencies and name not in disabled]
        dsh = manifest.get("dsh") if isinstance(manifest.get("dsh"), dict) else {}
        assert isinstance(dsh, dict)
        profile = dsh.get("profile") if isinstance(dsh.get("profile"), dict) else {}
        assert isinstance(profile, dict)
        profile["bundles"] = [*builtins, *enabled]
        dsh["profile"] = profile
        manifest["dsh"] = dsh
        self._write_json(self._manifest_path(), manifest)

    def _resolve_install_source(self, source: str) -> str:
        if not _PACKAGE_NAME.fullmatch(source):
            return source
        # Resolve metadata before running package scripts. The mutable latest
        # selector never reaches `dsh plugin add`; the exact version does.
        result = self._invoke(
            ("npm", "view", f"{source}@latest", "version", "--json"), cwd=self._cwd
        )
        try:
            if len(result.stdout) > _MAX_JSON_BYTES:
                raise ValueError("oversized registry response")
            version = json.loads(result.stdout)
            if not isinstance(version, str):
                raise ValueError("expected one version")
            return validate_dsh_registry_source(f"{source}@{version}")
        except (ValueError, TypeError) as error:
            raise DshPluginMutationError(
                "npm latest did not resolve to one exact version"
            ) from error

    def _prepare_source(self, source: str) -> _PreparedSource:
        value = source.strip()
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            # dsh CLI 以 Profile 目录为 cwd 解析 add 的源参数；相对当前工作目录
            # 存在的本地源必须先绝对化，否则会被写成指向不存在位置的 link: 依赖。
            workspace_relative = self._cwd / candidate
            if workspace_relative.exists():
                candidate = workspace_relative
            elif value.endswith(".tgz"):
                raise DshPluginMutationError(
                    f"local DSH plugin source does not exist: {value} "
                    f"(resolved against {self._cwd})"
                )
        if not candidate.is_absolute() or not candidate.exists():
            return _PreparedSource(value, "", "tgz", "")
        local = candidate.resolve()
        with tempfile.TemporaryDirectory(prefix="agentengine-dsh-source-") as directory:
            if local.is_dir():
                # Import lazily: dsh_toolchain owns the pinned pnpm workflow and
                # imports this bridge for validation.
                from ksadk.plugins.dsh_toolchain import (  # noqa: PLC0415
                    DshPluginDeveloper,
                    DshToolchainManager,
                )

                output = Path(directory)
                packed = DshPluginDeveloper(toolchain=DshToolchainManager()).pack(
                    local, output_dir=output
                )
                archive = Path(packed.artifact)
                kind: Literal["directory", "tgz"] = "directory"
            elif local.is_file() and local.name.endswith(".tgz"):
                archive = local
                kind = "tgz"
            else:
                raise DshPluginMutationError(
                    "local DSH plugin source must be a directory or .tgz archive"
                )
            try:
                content = archive.read_bytes()
            except OSError as error:
                raise DshPluginMutationError("local DSH plugin source is unreadable") from error
            digest_hex = hashlib.sha256(content).hexdigest()
            digest = f"sha256:{digest_hex}"
            root = (self._dsh_home / _IMMUTABLE_SOURCE_DIR / digest_hex).resolve()
            expected_parent = (self._dsh_home / _IMMUTABLE_SOURCE_DIR).resolve()
            if root.parent != expected_parent:
                raise DshPluginMutationError("immutable DSH source path escaped its store")
            target = root / "package.tgz"
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest_hex:
                    raise DshPluginMutationError(
                        "immutable DSH source store contains a digest collision"
                    )
            else:
                temporary = root / f".package.{os.getpid()}.tmp"
                try:
                    temporary.write_bytes(content)
                    temporary.chmod(0o400)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            target.chmod(0o400)
            relative = target.relative_to(self._dsh_home).as_posix()
            return _PreparedSource(str(target), digest, kind, relative)

    def _verify_source_receipts(
        self,
        manifest: Mapping[str, object],
        state: _ProfileState,
        *,
        names: Sequence[str] | None = None,
    ) -> None:
        dependencies = self._dependencies(manifest)
        for name, spec in dependencies.items():
            if _IMMUTABLE_SOURCE_DIR in spec and name not in state["sources"]:
                raise DshPluginMutationError(
                    f"DSH plugin {name!r} is missing its immutable source receipt"
                )
        selected = names if names is not None else tuple(state["sources"])
        store = (self._dsh_home / _IMMUTABLE_SOURCE_DIR).resolve()
        for name in selected:
            receipt = state["sources"].get(name)
            if receipt is None:
                continue
            if dependencies.get(name) != receipt["dependency_spec"]:
                raise DshPluginMutationError(
                    f"DSH plugin {name!r} dependency no longer matches its source receipt"
                )
            artifact = (self._dsh_home / receipt["artifact"]).resolve()
            if not artifact.is_relative_to(store) or not artifact.is_file():
                raise DshPluginMutationError(f"DSH plugin {name!r} immutable source is unavailable")
            try:
                actual = f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}"
            except OSError as error:
                raise DshPluginMutationError(
                    f"DSH plugin {name!r} immutable source is unreadable"
                ) from error
            if actual != receipt["digest"]:
                raise DshPluginMutationError(f"DSH plugin {name!r} immutable source digest changed")

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _snapshot(self) -> _ProfileSnapshot:
        files: dict[str, bytes | None] = {}
        for name in _SNAPSHOT_FILES:
            path = self._profile_root / name
            try:
                files[name] = path.read_bytes()
            except FileNotFoundError:
                files[name] = None
        return _ProfileSnapshot(
            files=files,
            node_modules_existed=(self._profile_root / "node_modules").exists(),
        )

    def _require_package_mutation_rollback(self, *, new_profile_allowed: bool) -> None:
        """Reject package mutations unless their filesystem effects are reversible."""

        if not self._profile_root.exists():
            if new_profile_allowed:
                return
            raise DshPluginNotFoundError(f"DSH profile {self._profile!r} is not initialized")
        if not self._manifest_path().is_file():
            raise DshPluginMutationError(
                "existing DSH profile directory has no package manifest; refusing to mutate it"
            )
        if not (self._profile_root / "pnpm-lock.yaml").is_file():
            if new_profile_allowed and self._is_pristine_official_web_profile():
                return
            raise DshPluginMutationError(
                "existing DSH profile has no pnpm lockfile, so package rollback is unavailable"
            )

    def _is_pristine_official_web_profile(self) -> bool:
        """Recognize an untouched Studio-owned profile generated by DSH.

        ``dsh --dump-config`` materializes a new profile before the first
        plugin mutation.  That profile has a manifest and workspace settings
        but no lockfile yet, so it must remain eligible for the first atomic
        install just like an entirely absent profile.  The resource profile
        is allowed to start with only ``dsh-base``; Studio adds its web layer
        before taking the build snapshot.
        """

        if self._profile not in {"web", "agentkit-resources"} or (
            self._profile_root / "node_modules"
        ).exists():
            return False
        if (self._profile_root / _STATE_FILE).exists():
            return False
        manifest = self._read_manifest()
        bundles = tuple(self._bundles(manifest))
        return (
            self._dependencies(manifest) == {}
            and bundles in {
                _WEB_PROFILE_BUILTINS,
                ("@deepseek-ai/dsh-base",),
            }
            and (self._profile_root / "pnpm-workspace.yaml").is_file()
        )

    def _rollback(
        self, snapshot: _ProfileSnapshot, original: BaseException, *, reinstall: bool = True,
    ) -> None:
        try:
            if all(content is None for content in snapshot.files.values()):
                profiles_root = (self._dsh_home / "profiles").resolve()
                profile_root = self._profile_root.resolve()
                if profile_root.parent != profiles_root:
                    raise DshPluginMutationError("refusing to clean an untrusted DSH profile path")
                if profile_root.exists():
                    shutil.rmtree(profile_root, ignore_errors=False)
                return
            node_modules = self._profile_root / "node_modules"
            if not snapshot.node_modules_existed and node_modules.exists():
                profile_root = self._profile_root.resolve()
                if node_modules.is_symlink() or node_modules.resolve().parent != profile_root:
                    raise DshPluginMutationError(
                        "refusing to clean an untrusted DSH node_modules path"
                    )
                shutil.rmtree(node_modules, ignore_errors=False)
            self._profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            for name, content in snapshot.files.items():
                path = self._profile_root / name
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
                    path.chmod(0o600)
            if (
                reinstall and snapshot.files.get("pnpm-lock.yaml") is not None
                and self._manifest_path().is_file()
            ):
                self._plugin_command("install", "--frozen-lockfile")
                for name in ("package.json", _STATE_FILE):
                    content = snapshot.files.get(name)
                    path = self._profile_root / name
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(content)
                        path.chmod(0o600)
        except BaseException as rollback_error:
            raise DshPluginMutationError(
                "DSH plugin mutation failed and profile rollback also failed"
            ) from rollback_error
        if isinstance(original, DshBridgeError):
            return

    @staticmethod
    def _validate_source(source: str) -> None:
        value = source.strip()
        if (
            not value
            or len(value) > 2048
            or value.startswith("-")
            or any(character in source for character in ("\r", "\n", "\0"))
            or value in {".", ".."}
            or value.startswith(("file:", "link:"))
            or value.startswith(("./", "../", "file:./", "file:../", "link:./", "link:../"))
        ):
            raise ValueError(
                "DSH plugin source must be an exact registry version or absolute local path"
            )
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            if not candidate.exists():
                raise ValueError("absolute local DSH plugin source does not exist")
            return
        # Bare names resolve latest to an exact version before package mutation.
        # Git, URL, explicit tags and ranges remain unsupported. Local sources
        # are packed and content-addressed by _prepare_source.
        validate_dsh_registry_request(value)

    @staticmethod
    def _validate_package_name(name: str) -> None:
        if not _PACKAGE_NAME.fullmatch(name):
            raise ValueError("invalid DSH plugin package name")

    @staticmethod
    def _string(value: object) -> str:
        return value if isinstance(value, str) else ""

    @staticmethod
    def _run_command(
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
    ) -> _CommandResult:
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=dict(environment),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=os.name == "posix",
            )
            try:
                stdout, stderr = process.communicate(timeout=_COMMAND_TIMEOUT_SECONDS)
            except BaseException:
                # DSH synchronously waits on pnpm. Stop both before profile
                # rollback, including when a child still owns the output pipes.
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:  # pragma: no cover - supported production hosts are Unix
                    process.kill()
                process.communicate()
                raise
            completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DshHostUnavailableError("DSH host command did not complete") from error
        if completed.returncode != 0:
            diagnostic = _redact_diagnostic(completed.stderr or completed.stdout)
            raise DshPluginMutationError(
                f"DSH host command failed with exit code {completed.returncode}"
                + (f": {diagnostic[-2000:]}" if diagnostic else "")
            )
        return _CommandResult(stdout=completed.stdout, stderr=completed.stderr)


def _redact_diagnostic(value: str) -> str:
    value = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[redacted]@", value, flags=re.I)
    return re.sub(
        r"((?:token|password|authorization|_authToken)\s*[:=]\s*)[^\s]+",
        r"\1[redacted]",
        value,
        flags=re.I,
    ).strip()


__all__ = [
    "DshBridgeError",
    "DshBridgeHost",
    "DshHostUnavailableError",
    "DshPluginApprovalRequired",
    "DshPluginInventory",
    "DshPluginMutationError",
    "DshPluginNotFoundError",
    "DshProfilePluginBridge",
    "DshProfileProjection",
    "DshProfileRecoveryError",
    "dsh_subprocess_environment",
    "validate_dsh_registry_source",
]
