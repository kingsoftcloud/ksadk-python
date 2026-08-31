"""真实模型长任务 + Memory 标注评测（长任务方案 §11 P2，真实模型 E2E）。

:mod:`ksadk.harness.evaluation` 用确定性 reasoner 做基线；本模块把
reasoner 换成**真实模型**（LiteLLM，Anthropic 兼容协议），跑两类评测：

1. **跨压缩长任务评测**：固定数据集（``FIXED_DATASET``）+ 真实模型摘要/
   回答，度量真实压缩保真（fact_retention / goal_retention）与
   Actual↔Manifest Token 闭环（usage 由真实模型响应回填）；
2. **Memory 标注评测**：固定对话集（含记忆意图/偏好纠错/闲聊噪声），
   对比两套标注器与人工金标的 precision / recall / F1：

   - 规则标注器：:func:`ksadk.memory.extraction.propose_memory_candidates`
     （生产路径，纯确定性）；
   - 模型标注器：LLM 按 JSON schema 标注（content/memory_type/operation/
     confidence 档位），评测模型标注质量本身。

运行方式（需要真实模型端点，不进默认测试套件）::

    KSADK_REAL_MODEL_EVAL=1 python -m ksadk.harness.real_model_eval

模型配置（环境变量）：

- ``KSADK_EVAL_LITELLM_MODEL``：LiteLLM 模型名（默认 ``anthropic/glm-5.3``）；
- ``KSADK_EVAL_BASE_URL`` / ``KSADK_EVAL_API_KEY``：端点与凭证
  （默认回落 ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN``）。
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Sequence

from ksadk.harness.evaluation import (
    DEFAULT_GATE_THRESHOLDS,
    FIXED_DATASET,
    LongTaskCase,
    evaluate_long_task,
    release_gate,
)
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn

#: 默认 LiteLLM 模型（经 Anthropic 兼容代理的 GLM）。
DEFAULT_EVAL_MODEL = "anthropic/glm-5.3"

_ANNOTATION_SYSTEM_PROMPT = """你是记忆标注器。阅读一段对话事件流，标注值得写入长期记忆的条目。

标注规则：
- 只标注：用户显式记忆请求、用户偏好及其纠正、工具/系统确认的稳定事实；
- 忽略：闲聊、临时性问题、与记忆无关的指令；
- 每条输出：content（记忆内容，简洁陈述句）、memory_type（profile 或 fact）、
  operation（add 或 update）、confidence（0-1 的一位小数，显式意图≥0.9，
  隐式偏好≤0.8，工具事实≤0.8）。

只输出 JSON 数组，不要其他文字。没有可标注条目时输出 []。
"""


def _env_model_config() -> tuple[str, str, str]:
    model = os.getenv("KSADK_EVAL_LITELLM_MODEL", DEFAULT_EVAL_MODEL)
    base_url = os.getenv("KSADK_EVAL_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL", "")
    api_key = os.getenv("KSADK_EVAL_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN", "")
    return model, base_url, api_key


class RealModelReasoner(HarnessReasoner):
    """LiteLLM 真实模型 reasoner（含 usage 回填，供 Actual↔Manifest 闭环）。"""

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        env_model, env_url, env_key = _env_model_config()
        self._model = model or env_model
        self._base_url = base_url or env_url
        self._api_key = api_key or env_key

    async def complete(self, *, model, prompt, messages, tools):
        from litellm import acompletion

        # 评测代理（Anthropic 兼容）存在瞬态故障与 TPM 限流（APIConnectionError
        # 'id' / 偶发 BadRequestError / 429）；指数退避重试，覆盖一个限流窗口。
        last_error: Exception | None = None
        response = None
        delays = (2.0, 5.0, 10.0, 20.0, 30.0)
        for attempt in range(len(delays) + 1):
            try:
                response = await acompletion(
                    model=self._model,
                    messages=list(messages),
                    tools=[tool.openai_schema for tool in tools],
                    tool_choice="auto" if tools else None,
                    base_url=self._base_url or None,
                    api_key=self._api_key or None,
                )
                break
            except Exception as exc:  # noqa: BLE001 - 代理瞬态故障/限流重试
                last_error = exc
                if attempt >= len(delays):
                    break
                await asyncio.sleep(delays[attempt])
        if response is None:
            raise RuntimeError(
                f"eval model {self._model!r} 重试后仍失败: {last_error}"
            ) from last_error
        choice = (response.choices or [None])[0]
        if choice is None:
            raise RuntimeError(f"eval model {self._model!r} returned no choices")
        message = choice.message
        calls = []
        for index, call in enumerate(getattr(message, "tool_calls", None) or []):
            function = getattr(call, "function", None)
            name = str(getattr(function, "name", "") or "").strip()
            raw = getattr(function, "arguments", None) or "{}"
            arguments = json.loads(raw) if isinstance(raw, str) else raw
            if name and isinstance(arguments, dict):
                from ksadk.harness.reasoner import HarnessToolCall

                calls.append(
                    HarnessToolCall(
                        call_id=str(getattr(call, "id", "") or f"tool-call-{index}"),
                        name=name,
                        arguments=dict(arguments),
                    )
                )
        usage = getattr(response, "usage", None)
        usage_payload = None
        if usage is not None:
            usage_payload = {
                "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            }
        content = getattr(message, "content", None)
        return HarnessReasoningTurn(
            final_text=str(content) if content is not None else None,
            tool_calls=tuple(calls),
            usage=usage_payload,
        )


# ---------------------------------------------------------------------------
# 跨压缩长任务：真实模型评测
# ---------------------------------------------------------------------------


def evaluate_long_task_with_real_model(
    dataset: Sequence[LongTaskCase] = FIXED_DATASET,
    *,
    reasoner: HarnessReasoner | None = None,
) -> dict[str, Any]:
    """固定数据集 + 真实模型执行，返回评测报告 + 门禁结果。"""
    from ksadk.harness.context_engine import HarnessContextEngine
    from ksadk.harness.engine.langgraph import ManagedLangGraphEngine

    effective = reasoner or RealModelReasoner()

    def factory() -> Any:
        return ManagedLangGraphEngine(
            reasoner=effective,
            context_engine=HarnessContextEngine(),
        )

    report = evaluate_long_task(dataset, engine_factory=factory)
    report["gate"] = release_gate(report, thresholds=DEFAULT_GATE_THRESHOLDS)
    return report


# ---------------------------------------------------------------------------
# Memory 标注评测
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldenAnnotation:
    """人工金标：一条应被标注出的记忆。"""

    content: str
    memory_type: str  # profile / fact
    operation: str  # add / update
    confidence_min: float  # 金标置信度下限（归档用）


@dataclass(frozen=True)
class AnnotationCase:
    """一个标注用例：对话事件流 + 金标。"""

    case_id: str
    events: tuple[dict[str, Any], ...]
    golden: tuple[GoldenAnnotation, ...]


def _event(event_type: str, author: str, text: str, seq: int) -> dict[str, Any]:
    return {
        "id": f"evt_{seq}",
        "seq_id": seq,
        "event_type": event_type,
        "author": author,
        "text": text,
    }


#: 固定标注评测集（v1，冻结）：4 个用例，混合记忆意图 / 偏好纠错 / 噪声。
MEMORY_ANNOTATION_DATASET: tuple[AnnotationCase, ...] = (
    AnnotationCase(
        case_id="explicit-requests",
        events=(
            _event("user_message", "user", "请记住：我的报销必须走对公转账。", 1),
            _event("user_message", "user", "顺便记住我的工号是 KS-04217。", 2),
            _event("user_message", "user", "今天天气怎么样？", 3),
        ),
        golden=(
            GoldenAnnotation("我的报销必须走对公转账", "profile", "add", 0.9),
            GoldenAnnotation("我的工号是 KS-04217", "profile", "add", 0.9),
        ),
    ),
    AnnotationCase(
        case_id="preference-correction",
        events=(
            _event("user_message", "user", "以后报表不要用英文，请改成中文。", 1),
            _event("user_message", "user", "帮我算一下 Q3 的数。", 2),
        ),
        golden=(
            GoldenAnnotation("报表使用中文", "profile", "update", 0.9),
        ),
    ),
    AnnotationCase(
        case_id="tool-facts-and-noise",
        events=(
            _event(
                "tool_result",
                "tool",
                "查询成功：供应商 VEN-3101 名称华信科技，状态已认证。",
                1,
            ),
            _event("user_message", "user", "嗯，知道了。", 2),
            _event("user_message", "user", "讲个笑话吧。", 3),
        ),
        golden=(
            GoldenAnnotation("供应商 VEN-3101 名称华信科技", "fact", "add", 0.7),
        ),
    ),
    AnnotationCase(
        case_id="no-memory-at-all",
        events=(
            _event("user_message", "user", "这个函数为什么会报空指针？", 1),
            _event("assistant_message", "assistant", "因为入参为 None。", 2),
        ),
        golden=(),
    ),
)


def _large_memory_annotation_dataset() -> tuple[AnnotationCase, ...]:
    """构建冻结的 100 条 Memory 金标集。

    保留 4 条人工基线，并为显式记忆、明确纠错、已核实工具事实和纯噪声
    各补 24 条模板化金标。模板中的标识和值均固定，保证跨版本可重复比较。
    这份集合用于规模回归；默认真实模型 smoke 仍只跑上面的 4 条，避免 CI
    或本地开发意外产生 100 次模型调用。
    """

    cases = list(MEMORY_ANNOTATION_DATASET)
    for index in range(24):
        suffix = f"{index:02d}"
        cases.extend(
            (
                AnnotationCase(
                    case_id=f"large-explicit-{suffix}",
                    events=(
                        _event(
                            "user_message",
                            "user",
                            f"请记住：我的部门编号是 DEPT-{suffix}。",
                            1,
                        ),
                    ),
                    golden=(
                        GoldenAnnotation(
                            f"我的部门编号是 DEPT-{suffix}", "profile", "add", 0.9
                        ),
                    ),
                ),
                AnnotationCase(
                    case_id=f"large-correction-{suffix}",
                    events=(
                        _event(
                            "user_message",
                            "user",
                            f"以后报表{suffix}不要用英文，请改成中文。",
                            1,
                        ),
                    ),
                    golden=(
                        GoldenAnnotation(
                            f"报表{suffix}改为中文", "profile", "update", 0.9
                        ),
                    ),
                ),
                AnnotationCase(
                    case_id=f"large-tool-fact-{suffix}",
                    events=(
                        _event(
                            "tool_result",
                            "tool",
                            f"查询成功：供应商 VEN-{suffix} 名称供应商{suffix}，状态已认证。",
                            1,
                        ),
                    ),
                    golden=(
                        GoldenAnnotation(
                            f"供应商 VEN-{suffix} 名称供应商{suffix}", "fact", "add", 0.7
                        ),
                    ),
                ),
                AnnotationCase(
                    case_id=f"large-noise-{suffix}",
                    events=(
                        _event(
                            "user_message",
                            "user",
                            f"请解释第 {index + 1} 个临时计算步骤。",
                            1,
                        ),
                        _event("assistant_message", "assistant", "这是一次临时回答。", 2),
                    ),
                    golden=(),
                ),
            )
        )
    return tuple(cases)


#: 规模化 Memory 金标集（v1，冻结）：100 条，四类场景各 25 条。
LARGE_MEMORY_ANNOTATION_DATASET: tuple[AnnotationCase, ...] = (
    _large_memory_annotation_dataset()
)


def _normalize(text: str) -> str:
    return re.sub(r"[\s。.，,！!？?]", "", text)


#: 语义等价判定阈值（宽松内容匹配：子串直接命中，或归一化后相似度 ≥ 0.6）。
_MATCH_SIMILARITY = 0.6


def _match(annotation_content: str, golden: GoldenAnnotation) -> bool:
    """宽松内容匹配：子串命中，或归一化后 difflib 相似度达标。

    标注器措辞与金标不同（"用户的…" vs "我的…"）不应算 miss。
    """
    a = _normalize(annotation_content)
    g = _normalize(golden.content)
    if not a or not g:
        return False
    if g in a or a in g:
        return True
    return difflib.SequenceMatcher(None, a, g).ratio() >= _MATCH_SIMILARITY


def score_annotations(
    predicted: Sequence[dict[str, Any]], golden: Sequence[GoldenAnnotation]
) -> dict[str, float]:
    """predicted vs 金标：precision / recall / F1（宽松内容匹配）。"""
    matched_golden: set[int] = set()
    true_positives = 0
    for pred in predicted:
        for index, gold in enumerate(golden):
            if index in matched_golden:
                continue
            if _match(str(pred.get("content") or ""), gold):
                matched_golden.add(index)
                true_positives += 1
                break
    predicted_count = len([p for p in predicted if str(p.get("content") or "").strip()])
    precision = true_positives / predicted_count if predicted_count else 1.0
    recall = true_positives / len(golden) if golden else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def rule_based_annotate(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """规则标注器（生产路径 propose_memory_candidates 的 dict 适配）。"""
    from types import SimpleNamespace

    from ksadk.memory.extraction import propose_memory_candidates

    # extraction 走属性访问（event.text/event_type/author/id），dict → SimpleNamespace。
    objects = [SimpleNamespace(**dict(event)) for event in events]
    candidates = propose_memory_candidates(objects)  # type: ignore[arg-type]
    return [
        {
            "content": c.content,
            "memory_type": c.memory_type,
            "operation": c.operation,
            "confidence": c.confidence,
        }
        for c in candidates
    ]


async def model_annotate(
    events: Sequence[dict[str, Any]],
    *,
    reasoner: RealModelReasoner | None = None,
) -> list[dict[str, Any]]:
    """模型标注器：LLM 按 JSON schema 输出记忆条目。"""
    effective = reasoner or RealModelReasoner()
    lines = []
    for event in events:
        author = str(event.get("author") or event.get("event_type") or "?")
        text = str(event.get("text") or event.get("content") or "").strip()
        if text:
            lines.append(f"[{author}] {text}")
    turn = await effective.complete(
        model="",
        prompt="",
        messages=[
            {"role": "system", "content": _ANNOTATION_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ],
        tools=[],
    )
    raw = (turn.final_text or "").strip()
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def evaluate_memory_annotation(
    *,
    dataset: Sequence[AnnotationCase] = MEMORY_ANNOTATION_DATASET,
    include_model: bool = True,
    reasoner: RealModelReasoner | None = None,
) -> dict[str, Any]:
    """Memory 标注评测：规则 vs 模型标注器，各自对金标打分。"""
    cases: list[dict[str, Any]] = []

    async def _run() -> None:
        for case in dataset:
            rule_preds = rule_based_annotate(case.events)
            entry: dict[str, Any] = {
                "case_id": case.case_id,
                "rule_based": score_annotations(rule_preds, case.golden),
                "golden_count": len(case.golden),
            }
            if include_model:
                model_preds = await model_annotate(case.events, reasoner=reasoner)
                entry["model_based"] = score_annotations(model_preds, case.golden)
                entry["model_predictions"] = [
                    str(p.get("content") or "")[:120] for p in model_preds
                ]
            cases.append(entry)

    asyncio.run(_run())
    result: dict[str, Any] = {"cases": cases}
    for annotator in ("rule_based", "model_based"):
        entries = [c for c in cases if annotator in c]
        if entries:
            result[annotator] = {
                key: sum(c[annotator][key] for c in entries) / len(entries)
                for key in ("precision", "recall", "f1")
            }
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_full_eval(
    *, output_path: str = "", memory_dataset: str = "smoke"
) -> dict[str, Any]:
    model, base_url, _key = _env_model_config()
    print(f"[real-model-eval] model={model} base_url={base_url or '(default)'}")
    print("[real-model-eval] 1/2 跨压缩长任务（固定数据集，真实模型）…")
    long_task = evaluate_long_task_with_real_model()
    print(f"  gate_passed={long_task['gate']['passed']}")
    for violation in long_task["gate"]["violations"]:
        metric, actual, threshold = (
            violation["metric"],
            violation["actual"],
            violation["threshold"],
        )
        print(f"  VIOLATION: {metric}={actual:.3f} < {threshold}")
    print("  aggregate:", json.dumps(long_task["aggregate"], ensure_ascii=False))

    print("[real-model-eval] 2/2 Memory 标注评测（规则 vs 模型标注器）…")
    annotation_dataset = (
        LARGE_MEMORY_ANNOTATION_DATASET
        if memory_dataset == "full"
        else MEMORY_ANNOTATION_DATASET
    )
    annotation = evaluate_memory_annotation(dataset=annotation_dataset)
    for annotator in ("rule_based", "model_based"):
        if annotator in annotation:
            print(f"  {annotator}: {json.dumps(annotation[annotator], ensure_ascii=False)}")

    report = {
        "model": model,
        "long_task": long_task,
        "memory_annotation": annotation,
    }
    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(f"[real-model-eval] report written to {output_path}")
    return report


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="ksadk.harness.real_model_eval")
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--memory-dataset",
        choices=("smoke", "full"),
        default="smoke",
        help="smoke 跑 4 条；full 跑冻结的 100 条 Memory 金标集",
    )
    args = parser.parse_args()
    run_full_eval(output_path=args.out, memory_dataset=args.memory_dataset)


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_EVAL_MODEL",
    "MEMORY_ANNOTATION_DATASET",
    "AnnotationCase",
    "GoldenAnnotation",
    "RealModelReasoner",
    "evaluate_long_task_with_real_model",
    "evaluate_memory_annotation",
    "model_annotate",
    "rule_based_annotate",
    "run_full_eval",
    "score_annotations",
]
