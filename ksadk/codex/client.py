"""CodexRuntimeAdapter 的 codex 后端接口 (goal-09)。

重托管模式:CodexRuntimeAdapter 对执行生命周期负责,不把 cancel/进程管理薄委托给上层。
``CodexClient`` 是 codex 后端(thread/turn 生命周期)的最小接口,方法集对齐 OpenAI
官方 ``openai-codex`` SDK 的**真实**线程模型(``thread_start`` / ``thread.turn`` /
``handle.stream`` / ``handle.interrupt`` / ``thread_resume``):

- 生产实现 ``AsyncCodexClient``:基于 ``AsyncCodex``。**lazy import**,缺依赖时显式
  报"请安装 ksadk[codex]",不静默失败;构造时对真实 SDK 方法做 ``hasattr`` 校验,
  版本漂移时 fail-fast,而非运行期 AttributeError。
- 测试实现:用 fake(见 tests/runners/test_codex_runtime.py / test_adapter_contract.py),
  不需要真 CLI 二进制。

诚实边界:本模块的 SDK **方法面**已对安装的 ``openai-codex==0.147.0`` 实证(方法存在性 +
协程/asyncgen 形态);Notification → RuntimeEvent 的**字段级** phase 映射需在接真实 codex
后端时按实况对齐(结构已按生成的 payload 类型映射,见 ``_notification_to_event_dict``)。
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ksadk.model_proxy.cache import CapabilityCache, credential_scope
from ksadk.model_proxy.detect import (
    CODEX_DIRECT_REQUIRED_TOOL_TYPES,
    CODEX_OPTIONAL_TOOL_TYPES,
    ModelCapabilities,
    probe_responses_capability,
)

# 探测缓存单例:能力判定跨 client 共享,按 (model, base, credential_scope) 长缓存
_CAPABILITY_CACHE = CapabilityCache(ttl=3600)


class CodexCapabilityUnavailableError(RuntimeError):
    """A caller-required Codex capability cannot be preserved by this route."""


class CodexPluginBootstrapError(CodexCapabilityUnavailableError):
    """A pinned Codex plugin marketplace could not be admitted or installed."""


_CODEX_PLUGIN_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CODEX_PLUGIN_NAME = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
_CODEX_MARKETPLACE_MANIFEST = Path(".agents/plugins/marketplace.json")
_CODEX_MARKETPLACE_MAX_FILES = 20_000
_CODEX_MARKETPLACE_MAX_BYTES = 256 * 1024 * 1024
_CODEX_MARKETPLACE_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def codex_marketplace_tree_digest(marketplace_path: str | Path) -> str:
    """Hash one immutable local marketplace using the snapshot tree algorithm.

    The format deliberately matches ``ksadk.plugins.codex_manifest``: sorted
    relative POSIX path, NUL, decimal byte length, NUL, bytes, NUL. Directories
    are ignored and symlinks are rejected, so a launch cannot substitute files
    behind the digest-pinned build artifact.
    """

    root = Path(marketplace_path).expanduser()
    if not root.is_absolute():
        raise ValueError("Codex plugin marketplace_path must be absolute")
    if root.is_symlink():
        raise ValueError("Codex plugin marketplace root must not be a symlink")
    if not root.is_dir():
        raise ValueError("Codex plugin marketplace_path must name an existing directory")
    manifest_path = root / _CODEX_MARKETPLACE_MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(
            "Codex plugin marketplace is missing a regular .agents/plugins/marketplace.json"
        )

    files: list[Path] = []
    for candidate in sorted(
        root.rglob("*"),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            raise ValueError(f"Codex plugin marketplace must not contain symlinks: {relative}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError("Codex plugin marketplace may contain regular files only: " + relative)
        files.append(candidate)
    if len(files) > _CODEX_MARKETPLACE_MAX_FILES:
        raise ValueError(
            f"Codex plugin marketplace exceeds {_CODEX_MARKETPLACE_MAX_FILES} regular files"
        )

    digest = hashlib.sha256()
    total_bytes = 0
    for candidate in files:
        relative = candidate.relative_to(root).as_posix()
        try:
            raw = candidate.read_bytes()
        except OSError as exc:
            raise ValueError(f"Codex plugin marketplace file cannot be read: {relative}") from exc
        total_bytes += len(raw)
        if total_bytes > _CODEX_MARKETPLACE_MAX_BYTES:
            raise ValueError(
                f"Codex plugin marketplace exceeds {_CODEX_MARKETPLACE_MAX_BYTES} content bytes"
            )
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(candidate.stat().st_mode) & 0o111:03o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(str(len(raw)).encode("ascii"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True)
class CodexPluginBootstrap:
    """Digest-pinned launch contract for one build-local Codex marketplace."""

    marketplace_path: str
    marketplace_name: str
    plugin_names: tuple[str, ...]
    snapshot_digest: str

    def __post_init__(self) -> None:
        root = Path(self.marketplace_path).expanduser()
        if not root.is_absolute():
            raise ValueError("Codex plugin marketplace_path must be absolute")
        if not self.marketplace_name or len(self.marketplace_name) > 256:
            raise ValueError("Codex plugin marketplace_name must be 1..256 characters")
        if _CODEX_PLUGIN_NAME.fullmatch(self.marketplace_name) is None:
            raise ValueError("Codex plugin marketplace_name has an unsupported format")
        if not self.plugin_names:
            raise ValueError("Codex plugin plugin_names must not be empty")
        if any(
            not isinstance(name, str)
            or len(name) > 256
            or _CODEX_PLUGIN_NAME.fullmatch(name) is None
            for name in self.plugin_names
        ):
            raise ValueError("Codex plugin plugin_names contain an unsupported name")
        if tuple(sorted(set(self.plugin_names))) != self.plugin_names:
            raise ValueError("Codex plugin plugin_names must be unique and lock-sorted")
        if _CODEX_PLUGIN_DIGEST.fullmatch(self.snapshot_digest) is None:
            raise ValueError("Codex plugin snapshot_digest must be a lowercase sha256 digest")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CodexPluginBootstrap":
        expected = {
            "marketplace_path",
            "marketplace_name",
            "plugin_names",
            "snapshot_digest",
        }
        supplied = set(value)
        if supplied != expected:
            missing = sorted(expected - supplied)
            unexpected = sorted(supplied - expected)
            details: list[str] = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unexpected:
                details.append("unexpected " + ", ".join(unexpected))
            raise ValueError("invalid Codex plugin bootstrap config: " + "; ".join(details))
        raw_names = value["plugin_names"]
        if not isinstance(raw_names, (list, tuple)):
            raise ValueError("Codex plugin plugin_names must be an array")
        return cls(
            marketplace_path=str(value["marketplace_path"]),
            marketplace_name=str(value["marketplace_name"]),
            plugin_names=tuple(raw_names),
            snapshot_digest=str(value["snapshot_digest"]),
        )

    @property
    def root(self) -> Path:
        return Path(self.marketplace_path).expanduser()

    @property
    def manifest_path(self) -> Path:
        return self.root / _CODEX_MARKETPLACE_MANIFEST

    def verify(self) -> None:
        """Verify bytes and the small manifest projection consumed at launch."""

        try:
            observed_digest = codex_marketplace_tree_digest(self.root)
        except ValueError as exc:
            raise CodexPluginBootstrapError("Codex plugin marketplace cannot be verified") from exc
        if observed_digest != self.snapshot_digest:
            raise CodexPluginBootstrapError(
                "Codex plugin marketplace digest mismatch: "
                f"expected {self.snapshot_digest}, observed {observed_digest}"
            )
        try:
            raw = self.manifest_path.read_bytes()
        except OSError as exc:
            raise CodexPluginBootstrapError(
                "Codex plugin marketplace manifest cannot be read"
            ) from exc
        if len(raw) > _CODEX_MARKETPLACE_MAX_MANIFEST_BYTES:
            raise CodexPluginBootstrapError("Codex plugin marketplace manifest is too large")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexPluginBootstrapError(
                "Codex plugin marketplace manifest is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(payload, dict) or payload.get("name") != self.marketplace_name:
            raise CodexPluginBootstrapError(
                "Codex plugin marketplace manifest name does not match launch config"
            )
        raw_plugins = payload.get("plugins")
        if not isinstance(raw_plugins, list):
            raise CodexPluginBootstrapError(
                "Codex plugin marketplace manifest plugins must be an array"
            )
        manifest_names = [item.get("name") for item in raw_plugins if isinstance(item, dict)]
        missing = [name for name in self.plugin_names if manifest_names.count(name) != 1]
        if missing:
            raise CodexPluginBootstrapError(
                "Codex plugin marketplace does not contain each pinned plugin exactly once: "
                + ", ".join(missing)
            )


@dataclass(frozen=True)
class _CapabilityRoute:
    use_proxy: bool
    disabled_tool_types: frozenset[str] = frozenset()
    unavailable_required_tool_types: frozenset[str] = frozenset()

    def require_available(self) -> None:
        if self.unavailable_required_tool_types:
            names = ", ".join(sorted(self.unavailable_required_tool_types))
            raise CodexCapabilityUnavailableError(
                f"required Codex capabilities unavailable on selected route: {names}"
            )


@dataclass
class _PendingApproval:
    approval_id: str
    thread_id: str
    method: str
    params: dict[str, Any]
    resolved: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] = field(default_factory=dict)


@dataclass
class _PendingInteraction:
    interaction_id: str
    thread_id: str
    method: str
    params: dict[str, Any]
    resolved: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] = field(default_factory=dict)


def _is_openai_official(base: str) -> bool:
    """OpenAI 官方 base_url(直连,不探测不代理)。"""
    from urllib.parse import urlsplit

    host = (urlsplit(base).hostname or "").lower()
    return host == "api.openai.com" or host.endswith(".openai.com")


def _upgrade_http_to_https(upstream: str) -> str:
    """http 非回环自动升级 https(凭证安全 + 兼容历史 http .env;星流等支持 https)。

    ProxyConfig 强制非回环 https(凭证不裸奔);历史 .env 常写 http://kspmas,
    这里 upgrade 让 codex 代理对它可用,不改通用模板(ADK 等用 http 本就 OK)。
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(upstream)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        return urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))
    return upstream


def _route_for_capabilities(
    caps: ModelCapabilities,
    *,
    required_tool_types: set[str] | frozenset[str] = frozenset(),
) -> _CapabilityRoute:
    """Split protocol requirements from optional tool degradation.

    The current Studio/Codex launch contract has no user-facing required-tool
    declaration, so callers pass the default empty set.  This explicit input is
    the fail-closed seam for a future ``web_search=required`` contract.
    """

    native_ready = caps.responses_supported is True and CODEX_DIRECT_REQUIRED_TOOL_TYPES.issubset(
        caps.tool_types
    )
    use_proxy = not native_ready
    disabled = (
        CODEX_OPTIONAL_TOOL_TYPES
        if use_proxy
        else CODEX_OPTIONAL_TOOL_TYPES.difference(caps.tool_types)
    )
    required = frozenset(required_tool_types)
    unavailable = (required.difference(caps.tool_types)) | required.intersection(disabled)
    return _CapabilityRoute(
        use_proxy=use_proxy,
        disabled_tool_types=frozenset(disabled),
        unavailable_required_tool_types=frozenset(unavailable),
    )


def _probe_capability_route(
    model: str,
    base: str,
    key: str,
    *,
    required_tool_types: set[str] | frozenset[str] = frozenset(),
) -> _CapabilityRoute:
    """Probe once and return protocol plus optional-tool routing decisions.

    纯文本 Responses 成功不代表能接收 Codex 0.147 的完整
    ``additional_tools`` 方言。namespace/custom 缺失或未知走兼容代理；仅缺
    web_search 时保留原生 Responses，并在 Codex 生成请求前禁用该可选工具。
    """

    def probe(m: str, b: str):
        import httpx

        with httpx.Client() as client:
            return probe_responses_capability(client, b, key, m, timeout=15.0)

    caps = _CAPABILITY_CACHE.get_or_probe(model, base, credential_scope(key), probe)
    return _route_for_capabilities(caps, required_tool_types=required_tool_types)


def _probe_requires_proxy(model: str, base: str, key: str) -> bool:
    """Compatibility predicate for callers/tests that only need protocol choice."""

    return _probe_capability_route(model, base, key).use_proxy


class CodexClient(ABC):
    """codex 后端(thread/turn 生命周期)的最小接口(对齐真实 SDK 线程模型)。"""

    async def bootstrap_plugins(self, config: CodexPluginBootstrap) -> None:
        """Install one admitted plugin set before creating the first thread.

        Custom clients must opt into this capability explicitly. Failing
        closed prevents a plugin-bearing launch from silently running without
        its locked native Codex capabilities.
        """

        del config
        raise CodexPluginBootstrapError(
            "connected Codex client does not support native plugin bootstrap"
        )

    @abstractmethod
    async def start_thread(self, config: Optional[dict[str, Any]] = None) -> str:
        """新建 thread(真实 SDK ``thread_start``),返回 thread_id。"""
        raise NotImplementedError

    @abstractmethod
    def run_turn(
        self,
        thread_id: str,
        prompt: Any,
        *,
        config: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """在 thread 上跑一个 turn(``thread.turn`` + ``handle.stream``),
        产出规范化事件 dict(``{"method": ..., "params": ...}``)。"""
        raise NotImplementedError

    async def compact_thread(self, thread_id: str) -> dict[str, Any]:
        raise RuntimeError("connected Codex client does not support context compaction")

    def run_goal(
        self,
        thread_id: str,
        objective: str,
        *,
        config: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one persisted Codex goal as a logical notification stream."""

        raise RuntimeError("connected Codex client does not support goal operations")

    @abstractmethod
    async def interrupt_active_turn(self, thread_id: str) -> bool:
        """interrupt 该 thread 当前活跃 turn(真实 SDK ``handle.interrupt``);
        无活跃 turn 返回 False。"""
        raise NotImplementedError

    @abstractmethod
    async def resume_thread(self, thread_id: str, config: Optional[dict[str, Any]] = None) -> str:
        """恢复 thread(真实 SDK ``thread_resume``),返回 thread_id。"""
        raise NotImplementedError

    async def resolve_approval(self, approval_id: str, decision: str) -> bool:
        """Resolve one live native approval request.

        Older/custom clients may not expose interactive approvals; returning
        ``False`` keeps that capability boundary explicit without breaking the
        existing client protocol.
        """

        return False

    async def resolve_interaction(
        self,
        interaction_id: str,
        data: dict[str, Any],
    ) -> bool:
        """Resolve one live structured user-input request."""

        return False

    async def pause_goal(self, thread_id: str) -> bool:
        """Pause an active native goal when supported."""

        return False

    async def cancel_goal(self, thread_id: str) -> bool:
        """Cancel an active native goal operation when supported."""

        return False

    @abstractmethod
    async def close(self) -> None:
        """释放后端资源(真实 SDK ``close``)。"""
        raise NotImplementedError


def _missing_codex_error() -> RuntimeError:
    return RuntimeError(
        "CodexRuntimeAdapter 需要 openai-codex SDK;请安装 ksadk[codex]:"
        " pip install 'ksadk[codex]'(openai-codex 为可选 extra,不进默认依赖)"
    )




def _coerce_enum(enum_type: Any, value: Any) -> Any:
    if value is None or isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        supported = ", ".join(member.value for member in enum_type)
        raise ValueError(
            f"unsupported {enum_type.__name__} {value!r}; expected {supported}"
        ) from exc


__all__ = [
    "AsyncCodexClient",
    "CodexClient",
    "CodexPluginBootstrap",
    "CodexPluginBootstrapError",
    "codex_marketplace_tree_digest",
]


from ksadk.codex.async_client import AsyncCodexClient  # noqa: E402
