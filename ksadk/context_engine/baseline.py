"""Shadow 基线采集器（评测方案第 10 节阶段 1：建立 Baseline）。

当前处于 shadow 阶段：shadow plan 不改运行行为，本采集器只把每次 turn 的可观测信号
落盘成结构化记录，作为后续 A/B 对比的基准。对齐评测方案：

- §3 A/B 记录字段（commit / runner_type / model / policy_version / prompt_hash ...）
- §5.4 效率指标（input/output token、PTL rate、compaction、accounting accuracy）
- §7.8 采集指标（prompt/history/memory/tool token、prompt_hash、capability hash、
  planned/projected/actual accuracy）

采集器只读 shadow plan dict 与 runtime usage/事件，不接触模型正文、凭证或敏感内容
（安全要求 §19：默认只记录 hash、长度、类型和脱敏摘要）。输出 JSONL，每行一条 turn 记录。
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


def _safe_commit() -> str:
    """取当前 git commit（脱敏：只取短 hash，失败时返回占位符，不抛异常）。"""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("KSADK_EVAL_COMMIT", "unknown")


def _ksadk_version() -> str:
    try:
        from ksadk.version import VERSION

        return str(VERSION)
    except Exception:  # noqa: BLE001
        return "unknown"


@dataclass
class BaselineTurnRecord:
    """单次 turn 的基线记录（评测方案 §12.4 单 Case 记录 + §7.8 采集指标）。"""

    # 版本与环境（§3）
    ksadk_commit: str = ""
    ksadk_version: str = ""
    context_policy_version: str = ""
    runner_type: str = ""
    deployment_mode: str = ""
    integration_mode: str = ""
    accounting_accuracy: str = ""
    capability_hash: str = ""
    model: str = ""
    execution_target: str = ""

    # 上下文计划标识与 hash（§7.8）
    plan_id: str = ""
    prompt_content_hash: str = ""
    prompt_stable_prefix_hash: str = ""
    tokenizer: str = ""

    # token 分类（§7.8 prompt/history/memory/tool token）
    tokens_by_kind: dict[str, int] = field(default_factory=dict)
    prompt_tokens_by_section: dict[str, int] = field(default_factory=dict)
    planned_input_tokens: int = 0

    # runtime 实际 usage（§5.4，runtime_reported 时才有）
    runtime_reported_input_tokens: int | None = None
    runtime_output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_status: str = ""
    unexpected_break: bool = False

    # 效率与稳定性（§5.4）
    compaction_triggered: bool = False
    compaction_trigger: str = ""
    prompt_too_long: bool = False
    retry_attempts: int = 0
    turn_latency_ms: int | None = None
    time_to_first_token_ms: int | None = None

    # 可诊断性（§5.4 opaque request rate）
    capability_mismatch: bool = False

    # 元数据
    session_id: str = ""
    invocation_id: str = ""
    recorded_at: str = ""


class BaselineCollector:
    """进程内基线采集器：从 shadow plan dict + runtime usage 累积 turn 记录。

    用法：在 conversation runtime 旁路（shadow plan 已挂在 prepared.shadow_context_plan）
    调用 ``record_turn(plan, usage=..., latency_ms=...)``；运行结束后 ``dump(path)`` 落盘。
    采集器本身不挂任何 span/不进决策路径，纯旁路只读。
    """

    def __init__(self, *, execution_target: str = "local") -> None:
        self._records: list[BaselineTurnRecord] = []
        self._records_lock = threading.RLock()
        self._commit = _safe_commit()
        self._version = _ksadk_version()
        self._execution_target = execution_target

    @property
    def records(self) -> list[BaselineTurnRecord]:
        with self._records_lock:
            return list(self._records)

    def record_turn(
        self,
        plan: Mapping[str, Any] | None,
        *,
        session_id: str = "",
        invocation_id: str = "",
        model: str = "",
        usage: Mapping[str, Any] | None = None,
        compaction_triggered: bool = False,
        compaction_trigger: str = "",
        prompt_too_long: bool = False,
        retry_attempts: int = 0,
        turn_latency_ms: int | None = None,
        time_to_first_token_ms: int | None = None,
        capability_mismatch: bool = False,
    ) -> BaselineTurnRecord:
        """从 shadow plan dict 构造一条 turn 记录并累积。

        ``plan`` 为 None（如未生成 shadow plan）时仍记录一条最小记录，标注
        ``accounting_accuracy=opaque``，便于统计 opaque request rate（§5.4）。
        """
        record = BaselineTurnRecord(
            ksadk_commit=self._commit,
            ksadk_version=self._version,
            execution_target=self._execution_target,
            session_id=session_id,
            invocation_id=invocation_id,
            model=str(model or ""),
            recorded_at=_utc_now_iso(),
            compaction_triggered=compaction_triggered,
            compaction_trigger=compaction_trigger,
            prompt_too_long=prompt_too_long,
            retry_attempts=retry_attempts,
            turn_latency_ms=turn_latency_ms,
            time_to_first_token_ms=time_to_first_token_ms,
            capability_mismatch=capability_mismatch,
        )
        if isinstance(plan, Mapping) and plan:
            record.context_policy_version = str(plan.get("policy_version") or "")
            record.runner_type = str(plan.get("runtime_type") or "")
            record.deployment_mode = str(plan.get("deployment_mode") or "local")
            record.integration_mode = str(plan.get("integration_mode") or "")
            record.accounting_accuracy = str(plan.get("accounting_accuracy") or "opaque")
            record.capability_hash = str(plan.get("capability_hash") or "")
            record.plan_id = str(plan.get("plan_id") or "")
            record.prompt_content_hash = str(plan.get("prompt_content_hash") or "")
            record.prompt_stable_prefix_hash = str(plan.get("prompt_stable_prefix_hash") or "")
            record.tokenizer = str(plan.get("tokenizer") or "")
            tbk = plan.get("tokens_by_kind")
            if isinstance(tbk, Mapping):
                record.tokens_by_kind = {str(k): int(v or 0) for k, v in tbk.items()}
            tbs = plan.get("prompt_tokens_by_section")
            if isinstance(tbs, Mapping):
                record.prompt_tokens_by_section = {str(k): int(v or 0) for k, v in tbs.items()}
            record.planned_input_tokens = int(plan.get("planned_input_tokens") or 0)
        else:
            record.accounting_accuracy = "opaque"

        if isinstance(usage, Mapping) and usage:
            record.runtime_reported_input_tokens = _opt_int(
                usage.get("input_tokens") or usage.get("prompt_tokens")
            )
            record.runtime_output_tokens = _opt_int(
                usage.get("output_tokens") or usage.get("completion_tokens")
            )
            details = usage.get("input_token_details") or usage.get("input_tokens_details")
            if isinstance(details, Mapping):
                record.cache_read_tokens = _opt_int(
                    details.get("cached_tokens")
                    or details.get("cached")
                    or details.get("cache_read")
                )
            record.cache_read_tokens = (
                _opt_int(usage.get("cache_read_input_tokens")) or record.cache_read_tokens
            )
            record.cache_creation_tokens = _opt_int(usage.get("cache_creation_input_tokens"))

        # cache_status/unexpected_break 由 span 路径（_set_prompt_cache_attributes）同源诊断
        # 并写入 trace；baseline 只记录 raw cache tokens，不重复跑 registry（避免与 span 路径
        # 共享 registry 时的记录顺序污染）。summary 的 unexpected_cache_break_count 据此如实
        # 为 0；完整诊断看 trace。如需 baseline 独立诊断，后续 PR 用独立 registry。
        with self._records_lock:
            self._records.append(record)
            if _flush_each_turn_enabled():
                self.dump(os.environ.get(_BASELINE_PATH_ENV, _DEFAULT_BASELINE_PATH))
        return record

    def summary(self) -> dict[str, Any]:
        """汇总指标（评测方案 §12.2 Scorecard 的基线版）。"""
        with self._records_lock:
            if not self._records:
                return {"turn_count": 0}
            total = len(self._records)
            planned = [r.planned_input_tokens for r in self._records if r.planned_input_tokens]
            reported = [
                r.runtime_reported_input_tokens
                for r in self._records
                if r.runtime_reported_input_tokens is not None
            ]
            latencies = [r.turn_latency_ms for r in self._records if r.turn_latency_ms is not None]
            return {
                "turn_count": total,
                "ptl_rate": _ratio(sum(1 for r in self._records if r.prompt_too_long), total),
                "compaction_count": sum(1 for r in self._records if r.compaction_triggered),
                "ptl_recovery_count": sum(
                    1
                    for r in self._records
                    if r.prompt_too_long and r.retry_attempts >= 1 and not _is_failed(r)
                ),
                "opaque_request_rate": _ratio(
                    sum(1 for r in self._records if r.accounting_accuracy == "opaque"), total
                ),
                "capability_mismatch_count": sum(1 for r in self._records if r.capability_mismatch),
                "unexpected_cache_break_count": sum(1 for r in self._records if r.unexpected_break),
                "planned_input_tokens": _stats(planned),
                "runtime_reported_input_tokens": _stats(reported),
                "turn_latency_ms": _stats(latencies),
                "runner_type_breakdown": _count_by([r.runner_type for r in self._records]),
                "accounting_accuracy_breakdown": _count_by(
                    [r.accounting_accuracy for r in self._records]
                ),
                "stable_prefix_hash_changes": _count_distinct(
                    [
                        r.prompt_stable_prefix_hash
                        for r in self._records
                        if r.prompt_stable_prefix_hash
                    ]
                ),
            }

    def dump(self, path: str | Path) -> Path:
        """落盘 JSONL（每行一条 turn 记录）+ 末尾一条 ``__summary__`` 汇总。"""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = out.with_name(f".{out.name}.{os.getpid()}.tmp")
        with self._records_lock:
            with temporary.open("w", encoding="utf-8") as fh:
                for record in self._records:
                    fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
                fh.write(json.dumps({"__summary__": self.summary()}, ensure_ascii=False) + "\n")
            os.replace(temporary, out)
        return out

    def clear(self) -> None:
        with self._records_lock:
            self._records.clear()


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _stats(values: list[int]) -> dict[str, float]:
    if not values:
        return {"count": 0}
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    p95_idx = min(n - 1, int(n * 0.95))
    return {
        "count": n,
        "mean": round(sum(values) / n, 2),
        "median": sorted_vals[n // 2],
        "p95": sorted_vals[p95_idx],
    }


def _count_by(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _count_distinct(values: list[str]) -> int:
    return len(set(values))


def _is_failed(record: BaselineTurnRecord) -> bool:
    # 没有 runtime output 且无 planned token 视为失败 turn（粗略，供 PTL recovery 统计）。
    return record.runtime_output_tokens is None and record.planned_input_tokens == 0


def _utc_now_iso() -> str:
    # 不能用 datetime.now()（脚本环境可能受限）；用 time + 手动格式化 UTC。
    t = time.time()
    secs = int(t)
    millis = int((t - secs) * 1000)
    g = time.gmtime(secs)
    return (
        f"{g.tm_year:04d}-{g.tm_mon:02d}-{g.tm_mday:02d}T"
        f"{g.tm_hour:02d}:{g.tm_min:02d}:{g.tm_sec:02d}.{millis:03d}Z"
    )


# ---------------------------------------------------------------------------
# 进程级单例 + env-gated 采集挂载
# ---------------------------------------------------------------------------

_BASELINE_COLLECT_ENV = "KSADK_BASELINE_COLLECT"
_BASELINE_PATH_ENV = "KSADK_BASELINE_PATH"
_DEFAULT_BASELINE_PATH = "/tmp/ksadk-context-baseline.jsonl"


def _flush_each_turn_enabled() -> bool:
    return str(os.environ.get("KSADK_BASELINE_FLUSH_EACH_TURN", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


_singleton_lock = threading.Lock()
_singleton: BaselineCollector | None = None
_atexit_registered = False


def baseline_collection_enabled() -> bool:
    """是否启用基线采集（env ``KSADK_BASELINE_COLLECT=1/true/on``）。"""
    return str(os.environ.get(_BASELINE_COLLECT_ENV, "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _baseline_path() -> str:
    return str(os.environ.get(_BASELINE_PATH_ENV, "") or _DEFAULT_BASELINE_PATH)


def get_baseline_collector() -> BaselineCollector | None:
    """返回进程级采集器单例；未启用（env 未开）时返回 None。

    首次启用时注册 atexit dump，保证进程结束自动落盘（评测方案 §10 阶段 1）。
    """
    global _singleton, _atexit_registered
    if not baseline_collection_enabled():
        return None
    with _singleton_lock:
        if _singleton is None:
            execution_target = str(os.environ.get("KSADK_BASELINE_EXECUTION_TARGET", "runtime"))
            _singleton = BaselineCollector(execution_target=execution_target)
            if not _atexit_registered:
                atexit.register(_atexit_dump)
                _atexit_registered = True
    return _singleton


def _atexit_dump() -> None:
    global _singleton
    if _singleton is None or not _singleton.records:
        return
    try:
        _singleton.dump(_baseline_path())
    except Exception:  # noqa: BLE001
        # 采集不能影响进程退出/线上行为。
        pass


def reset_baseline_collector_for_tests() -> None:
    """测试用：重置单例与 atexit 标记。"""
    global _singleton, _atexit_registered
    with _singleton_lock:
        _singleton = None
        _atexit_registered = False


def record_baseline_turn(
    plan: Mapping[str, Any] | None,
    **kwargs: Any,
) -> None:
    """env-gated 旁路采集入口：未启用时 no-op，启用时委托给单例 ``record_turn``。

    供 conversation runtime 旁路调用：传入 ``prepared.shadow_context_plan`` 与
    usage/compaction/PTL/latency 等真实信号。不抛异常、不进决策路径、不改线上行为。
    """
    collector = get_baseline_collector()
    if collector is None:
        return
    try:
        collector.record_turn(plan, **kwargs)
    except Exception:  # noqa: BLE001
        # 采集失败绝不影响主链路。
        pass
