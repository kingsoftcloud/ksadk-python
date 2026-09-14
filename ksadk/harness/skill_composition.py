"""Revision/Bundle skillRefs → SkillRuntime 的装配层（打通 Skill 主链路）。

链路各段此前已就位、但中间断开：

- Revision 编译：``compile_revision`` 产出 ``spec.capabilities.skill_bindings``
  （``skill://name@version`` 固定引用，load_policy=on_demand）；
- 引擎侧：``ManagedLangGraphEngine(skill_runtime=...)`` + SkillDisclosureBridge
  把 L0 目录与 L1/L2/L3 披露工具接入默认 Agent Loop。

本模块补上中间一环：把 Revision 的 Skill 引用**解析为本地 Skill 内容目录**并
构建 :class:`~ksadk.harness.skill_runtime.SkillRuntime`：

1. ``explicit_roots``：调用方直接给定 ``{ref: 目录}``（Revision/Bundle 构建
   方自己解包的场景）；
2. 本地 Skill 目录（``local_dir``，默认 ``KSADK_LOCAL_SKILLS_DIR``）：
   按 ``name@version`` / ``name`` 子目录或 SKILL.md frontmatter name 匹配；
3. ``ksadk.skills.PackageStore`` 缓存（默认 ``KSADK_SKILL_CACHE_DIR``）：
   命中已校验的解包目录。

解析失败的 **required** 绑定在装配期即报错（不等编译期）；optional 绑定降级
并给出 warnings。产出经 :func:`compose_engine` 可直接构造接好 Skill 的引擎。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ksadk.harness.skill_runtime import LocalSkillSource, SkillRuntime
from ksadk.harness.spec import HarnessSpec

#: ``skill://name@version`` 引用格式。
_SKILL_REF_RE = re.compile(r"^skill://(?P<name>[^@/]+)(?:@(?P<version>[^/]+))?$")

#: 可解析的本地 Skill 目录名候选后缀顺序。
_LOCAL_DIR_SUFFIXES = ("@{version}", "-{version}", "")


class SkillResolutionError(RuntimeError):
    """required Skill 绑定无法解析为本地内容目录。"""


def parse_skill_ref(ref: str) -> tuple[str, str]:
    """解析 ``skill://name@version`` → ``(name, version)``。version 可空。"""
    match = _SKILL_REF_RE.match(ref.strip())
    if not match:
        raise SkillResolutionError(f"Skill 引用格式不受支持（期望 skill://name@version）: {ref!r}")
    return match.group("name"), match.group("version") or ""


def resolve_skill_roots(
    spec: HarnessSpec,
    *,
    explicit_roots: dict[str, str | Path] | None = None,
    local_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
) -> tuple[dict[str, Path], list[str]]:
    """把 spec 的 skill_bindings 解析为 ``{ref: 本地内容目录}``。

    返回 ``(roots, warnings)``。required 绑定解析失败抛
    :class:`SkillResolutionError`；optional 失败记入 warnings 降级。
    """
    roots: dict[str, Path] = {}
    warnings: list[str] = []
    explicit = {ref: Path(p).resolve() for ref, p in (explicit_roots or {}).items()}
    local_root = _resolve_local_root(local_dir)
    store = _build_store(cache_dir)

    for binding in spec.capabilities.skill_bindings:
        ref = binding.capability_ref
        if binding.load_policy == "explicit":
            # explicit：模型完全不可见，不进目录也不需要内容目录。
            continue
        root = explicit.get(ref) or _find_local_root(local_root, ref) or _find_cached_root(
            store, ref
        )
        if root is None:
            message = f"Skill {ref!r} 未解析到本地内容目录"
            if binding.required:
                raise SkillResolutionError(message)
            warnings.append(f"{message}（optional，降级跳过）")
            continue
        if not (root / "SKILL.md").is_file():
            message = f"Skill {ref!r} 目录缺少 SKILL.md: {root}"
            if binding.required:
                raise SkillResolutionError(message)
            warnings.append(message)
            continue
        roots[ref] = root
    return roots, warnings


def build_skill_runtime(
    spec: HarnessSpec,
    *,
    explicit_roots: dict[str, str | Path] | None = None,
    local_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
) -> SkillRuntime | None:
    """解析 skill_bindings 并构建 SkillRuntime；无绑定或全降级时返回 None。"""
    roots, _warnings = resolve_skill_roots(
        spec, explicit_roots=explicit_roots, local_dir=local_dir, cache_dir=cache_dir
    )
    if not roots:
        return None
    return SkillRuntime(LocalSkillSource(skill_roots={ref: str(p) for ref, p in roots.items()}))


def compose_engine(
    spec: HarnessSpec,
    *,
    reasoner: Any | None = None,
    explicit_roots: dict[str, str | Path] | None = None,
    local_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
    **engine_kwargs: Any,
) -> Any:
    """一步装配：Revision spec → Skill 解析 → ManagedLangGraphEngine。

    其余引擎参数（context_engine / memory_runtime / event_sink …）经
    ``engine_kwargs`` 透传。返回 ``(engine, warnings)`` 前调用方通常只需要
    engine；warnings 见 :func:`resolve_skill_roots`，此处经
    ``engine.skill_warnings`` 附带。
    """
    from ksadk.harness.engine.langgraph import ManagedLangGraphEngine

    roots, warnings = resolve_skill_roots(
        spec, explicit_roots=explicit_roots, local_dir=local_dir, cache_dir=cache_dir
    )
    skill_runtime = (
        SkillRuntime(LocalSkillSource(skill_roots={ref: str(p) for ref, p in roots.items()}))
        if roots
        else None
    )
    engine = ManagedLangGraphEngine(
        reasoner=reasoner, skill_runtime=skill_runtime, **engine_kwargs
    )
    engine.skill_warnings = warnings  # type: ignore[attr-defined]
    return engine


# ------------------------------------------------------------- 内部解析


def _resolve_local_root(local_dir: str | Path | None) -> Path | None:
    if local_dir is not None:
        return Path(local_dir)
    env = os.getenv("KSADK_LOCAL_SKILLS_DIR", "").strip()
    return Path(env) if env else None


def _build_store(cache_dir: str | Path | None) -> Any | None:
    if cache_dir is not None:
        from ksadk.skills.package_store import PackageStore

        return PackageStore(cache_dir=cache_dir)
    env = os.getenv("KSADK_SKILL_CACHE_DIR", "").strip()
    if not env:
        return None
    from ksadk.skills.package_store import PackageStore

    return PackageStore(cache_dir=env)


def _find_local_root(local_root: Path | None, ref: str) -> Path | None:
    if local_root is None or not local_root.is_dir():
        return None
    name, version = parse_skill_ref(ref)
    for suffix in _LOCAL_DIR_SUFFIXES:
        dirname = suffix.format(version=version) if version else ("" if suffix else "")
        if not version and suffix:
            continue
        candidate = local_root / f"{name}{dirname}"
        if (candidate / "SKILL.md").is_file():
            return candidate
    # frontmatter name 兜底：目录名任意但 SKILL.md name 匹配。
    from ksadk.skills.loader import load_local_skill

    for child in sorted(local_root.iterdir()):
        if child.is_dir() and (child / "SKILL.md").is_file():
            try:
                if load_local_skill(child).name == name:
                    return child
            except Exception:  # noqa: BLE001 - 坏包跳过
                continue
    return None


def _find_cached_root(store: Any | None, ref: str) -> Path | None:
    if store is None:
        return None
    from ksadk.skills.models import SkillRef

    name, version = parse_skill_ref(ref)
    try:
        package = store.get_cached(
            SkillRef(skill_id=name, version=version, name=name)
        )
    except Exception:  # noqa: BLE001 - 缓存损坏视为未命中
        return None
    return package.root_dir if package is not None else None


__all__ = [
    "SkillResolutionError",
    "build_skill_runtime",
    "compose_engine",
    "parse_skill_ref",
    "resolve_skill_roots",
]
