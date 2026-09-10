"""Provider 模型能力声明与运行时模式选择。

外部兼容性验证的实测结果写入能力声明，而不是只留在
报告里。Reasoner 在发起调用前查询声明：某模型被实测证明流式 Tool Call
不可靠（例如 qwen3-8-max 流式/非流式行为不一致）时，运行时自动选择
该模型受支持的模式，而不是把失败留给生产流量。

声明来源（优先级从高到低）：

1. 进程内 ``declare_model_capability``（宿主显式注入）；
2. ``KSADK_MODEL_CAPABILITY_FILE`` 指向的 JSON 文件（由矩阵 CLI
   ``--declare-out`` 产出，进发布工件）；
3. 未声明 → 保持现有行为（环境变量/构造参数决定），不猜测。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 与 reasoner.py 保持一致的流式开关语义。
_ENV_STREAMING = "KSADK_MODEL_STREAMING"
_TRUTHY = {"1", "true", "yes", "on"}
_ENV_CAPABILITY_FILE = "KSADK_MODEL_CAPABILITY_FILE"


@dataclass(frozen=True)
class ModelCapabilityDeclaration:
    """一个模型标识的实测能力。未记录的维度保持 ``None``（不猜测）。"""

    model_id: str
    supports_basic_chat: bool | None = None
    supports_tool_calling: bool | None = None
    supports_streaming: bool | None = None
    supports_streaming_tool_calls: bool | None = None
    usage_reported: bool | None = None
    #: 声明来源（矩阵端点/版本或 "manual"），用于审计，不含 Secret。
    source: str = ""

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("model_id must not be empty")


@dataclass
class ModelCapabilityStore:
    """线程安全的能力声明注册表。"""

    declarations: dict[str, ModelCapabilityDeclaration] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def declare(self, declaration: ModelCapabilityDeclaration) -> None:
        with self._lock:
            self.declarations[declaration.model_id] = declaration

    def lookup(self, model_id: str) -> ModelCapabilityDeclaration | None:
        with self._lock:
            return self.declarations.get(model_id)

    def all(self) -> list[ModelCapabilityDeclaration]:  # noqa: A003 - registry API
        with self._lock:
            return list(self.declarations.values())

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schemaVersion": 1,
                "declarations": [asdict(item) for item in self.declarations.values()],
            }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ModelCapabilityStore":
        raw = payload.get("declarations") or []
        if not isinstance(raw, list):
            raise ValueError("capability file 'declarations' must be a list")
        store = cls()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("each declaration must be an object")
            known = {f for f in ModelCapabilityDeclaration.__dataclass_fields__}
            store.declare(
                ModelCapabilityDeclaration(
                    **{key: value for key, value in item.items() if key in known}
                )
            )
        return store

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)


def capabilities_from_matrix_report(
    report: dict[str, Any], *, source: str = ""
) -> list[ModelCapabilityDeclaration]:
    """把模型矩阵报告的结果行转成能力声明。

    只读实测布尔值；矩阵的 errors/usage 明细留在报告里，声明保持最小。
    """

    results = report.get("results")
    if not isinstance(results, list):
        raise ValueError("matrix report has no 'results' list")
    endpoint = report.get("endpoint") or ""
    effective_source = source or f"model-matrix:{endpoint}"
    declarations: list[ModelCapabilityDeclaration] = []
    for item in results:
        if not isinstance(item, dict) or not item.get("model_id"):
            continue

        def _flag(name: str) -> bool | None:
            value = item.get(name)
            return bool(value) if isinstance(value, bool) else None

        declarations.append(
            ModelCapabilityDeclaration(
                model_id=str(item["model_id"]),
                supports_basic_chat=_flag("basic_chat"),
                supports_tool_calling=_flag("tool_calling"),
                supports_streaming=_flag("stream_text"),
                supports_streaming_tool_calls=_flag("stream_tool_calling"),
                usage_reported=_flag("usage_reported"),
                source=effective_source,
            )
        )
    return declarations


def apply_matrix_report(
    report: dict[str, Any],
    store: ModelCapabilityStore,
    *,
    source: str = "",
) -> int:
    count = 0
    for declaration in capabilities_from_matrix_report(report, source=source):
        store.declare(declaration)
        count += 1
    return count


def _alias_keys(model_id: str) -> tuple[str, ...]:
    """一个标识的查找别名：原始、去 provider 前缀、去版本引用。"""

    keys = [model_id]
    if "/" in model_id:
        keys.append(model_id.rsplit("/", 1)[-1])
    return tuple(dict.fromkeys(keys))


def resolve_streaming_mode(
    *,
    requested: bool,
    model_id: str,
    store: ModelCapabilityStore | None,
    allow_upgrade: bool = False,
) -> bool:
    """按能力声明裁决流式请求。

    - 未声明该模型 → 返回 ``requested``（保持现有行为，不猜测）；
    - 声明 ``supports_streaming_tool_calls=False`` / ``supports_streaming=False``
      → 请求流式时降级为非流式；
    - ``allow_upgrade`` 且声明 ``supports_tool_calling=False`` 而流式
      Tool Call 可用（实测存在此类模型：非流式不发起 Tool Call，流式
      正常）→ 把默认非流式升级为流式；
    - 非流式请求（显式关闭流式）永不升级。
    """

    if store is None:
        return requested
    for key in _alias_keys(model_id):
        declaration = store.lookup(key)
        if declaration is None:
            continue
        if requested:
            if declaration.supports_streaming_tool_calls is False:
                return False
            if declaration.supports_streaming is False:
                return False
            return True
        if (
            allow_upgrade
            and declaration.supports_tool_calling is False
            and declaration.supports_streaming_tool_calls is True
            and declaration.supports_streaming is not False
        ):
            return True
        return False
    return requested


_GLOBAL_STORE: ModelCapabilityStore | None = None
_GLOBAL_LOCK = threading.Lock()


def load_capability_store(
    path: str | Path | None = None,
) -> ModelCapabilityStore | None:
    """从 JSON 文件装配全局能力声明（缺失/无效文件返回 ``None``）。"""

    resolved = Path(path or os.getenv(_ENV_CAPABILITY_FILE, "").strip() or "")
    if not str(resolved) or not resolved.is_file():
        return None
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"invalid model capability file: {resolved}: {exc}"
        ) from exc
    return ModelCapabilityStore.from_dict(payload)


def global_capability_store() -> ModelCapabilityStore | None:
    """惰性装配进程级能力声明（``KSADK_MODEL_CAPABILITY_FILE``）。"""

    global _GLOBAL_STORE
    with _GLOBAL_LOCK:
        if _GLOBAL_STORE is None:
            configured = os.getenv(_ENV_CAPABILITY_FILE, "").strip()
            if not configured:
                return None
            _GLOBAL_STORE = load_capability_store(configured)
        return _GLOBAL_STORE


def reset_global_capability_store() -> None:
    """测试钩子：清空进程级缓存。"""

    global _GLOBAL_STORE
    with _GLOBAL_LOCK:
        _GLOBAL_STORE = None


__all__ = [
    "ModelCapabilityDeclaration",
    "ModelCapabilityStore",
    "apply_matrix_report",
    "capabilities_from_matrix_report",
    "global_capability_store",
    "load_capability_store",
    "reset_global_capability_store",
    "resolve_streaming_mode",
]
