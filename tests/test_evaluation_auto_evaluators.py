import pytest

from ksadk.evaluation import DataPolicy, EvalCase, EvaluationConfig, TargetRun, evaluators
from ksadk.evaluation.evaluators import evaluate_case


def test_reference_match_scores_expected_output_without_assertions():
    metrics = evaluate_case(
        EvalCase(id="echo", turns=[{"input": "ping", "expectedOutput": "pong"}]),
        TargetRun(status="PASSED", output="pong"),
        ["reference_match@v1"],
        EvaluationConfig(evaluators=["reference_match@v1"]),
    )

    assert len(metrics) == 1
    assert metrics[0].name == "response_match"
    assert metrics[0].status == "PASS"
    assert metrics[0].score == 1.0
    assert metrics[0].evidence["threshold"] == 0.8


def test_default_evaluators_skip_reference_match():
    metrics = evaluate_case(
        EvalCase(id="echo", turns=[{"input": "ping", "expectedOutput": "pong"}]),
        TargetRun(status="PASSED", output="pong"),
        [],
    )

    assert metrics == []


def test_default_evaluators_preserve_cases_without_reference_output():
    metrics = evaluate_case(
        EvalCase(id="echo", input="ping"),
        TargetRun(status="PASSED", output="pong"),
        [],
    )

    assert metrics == []


def test_evaluator_dispatch_preserves_requested_order():
    metrics = evaluate_case(
        EvalCase(
            id="echo",
            turns=[{"input": "ping", "expectedOutput": "pong"}],
            assertions=[{"type": "response.contains", "value": "pong"}],
        ),
        TargetRun(status="PASSED", output="pong"),
        ["reference_match@v1", "response_contract@v1"],
    )

    assert [metric.name for metric in metrics] == ["response_match", "response_contract"]


def test_evaluator_dispatch_rejects_unknown_evaluator_before_execution():
    with pytest.raises(ValueError, match="不支持的评估器: unknown@v1"):
        evaluate_case(
            EvalCase(id="echo", input="ping"),
            TargetRun(status="PASSED", output="pong"),
            ["unknown@v1"],
        )


def test_evaluator_failure_does_not_skip_later_evaluators(monkeypatch):
    def fail(_: object) -> list[object]:
        raise RuntimeError("private evaluator detail")

    monkeypatch.setitem(
        evaluators._EVALUATOR_REGISTRY,
        "response_contract@v1",
        evaluators._EvaluatorDefinition("response_contract@v1", "response_contract", fail),
    )

    metrics = evaluate_case(
        EvalCase(id="echo", turns=[{"input": "ping", "expectedOutput": "pong"}]),
        TargetRun(status="PASSED", output="pong"),
        ["response_contract@v1", "reference_match@v1"],
    )

    assert [(metric.name, metric.status) for metric in metrics] == [
        ("response_contract", "ERROR"),
        ("response_match", "PASS"),
    ]
    assert metrics[0].evidence == {
        "evaluator": "response_contract@v1",
        "reason": "评估器执行失败",
        "errorType": "RuntimeError",
    }


def test_llm_judge_requires_explicit_data_egress_policy(monkeypatch):
    monkeypatch.setattr(
        "ksadk.evaluation.evaluators._run_llm_judge",
        lambda *_args: pytest.fail("Judge must not run with local_only"),
    )

    metrics = evaluate_case(
        EvalCase(id="echo", turns=[{"input": "ping", "expectedOutput": "pong"}]),
        TargetRun(status="PASSED", output="pong"),
        ["llm_judge@v1"],
        EvaluationConfig(
            evaluators=["llm_judge@v1"],
            data_policy=DataPolicy.LOCAL_ONLY,
        ),
    )

    assert len(metrics) == 1
    assert metrics[0].name == "response_quality"
    assert metrics[0].status == "UNAVAILABLE"
    assert metrics[0].evidence["reason"] == "Judge 需要 dataPolicy=full_trace"


def test_llm_judge_scores_with_configured_judge(monkeypatch):
    monkeypatch.setenv("KSADK_EVAL_JUDGE_API_KEY", "test-key")
    monkeypatch.setattr(
        "ksadk.evaluation.evaluators._run_llm_judge",
        lambda **_kwargs: 0.9,
    )

    metrics = evaluate_case(
        EvalCase(id="echo", turns=[{"input": "ping", "expectedOutput": "pong"}]),
        TargetRun(status="PASSED", output="pong"),
        ["llm_judge@v1"],
        EvaluationConfig(
            evaluators=["llm_judge@v1"],
            data_policy=DataPolicy.FULL_TRACE,
            judge_model="judge-model",
            judge_api_base="https://judge.example.invalid/v1",
        ),
    )

    assert metrics[0].name == "response_quality"
    assert metrics[0].status == "PASS"
    assert metrics[0].score == 0.9


def test_llm_judge_requires_explicit_judge_api_base(monkeypatch):
    monkeypatch.setenv("KSADK_EVAL_JUDGE_API_KEY", "test-key")
    monkeypatch.setattr(
        "ksadk.evaluation.evaluators._run_llm_judge",
        lambda **_kwargs: pytest.fail("Judge must not run without an API base"),
    )

    metrics = evaluate_case(
        EvalCase(id="echo", turns=[{"input": "ping", "expectedOutput": "pong"}]),
        TargetRun(status="PASSED", output="pong"),
        ["llm_judge@v1"],
        EvaluationConfig(
            evaluators=["llm_judge@v1"],
            data_policy=DataPolicy.FULL_TRACE,
            judge_model="judge-model",
        ),
    )

    assert metrics[0].status == "UNAVAILABLE"
    assert metrics[0].evidence["reason"] == "未配置 judgeApiBase"


def test_llm_judge_does_not_persist_judge_error_message(monkeypatch):
    monkeypatch.setenv("KSADK_EVAL_JUDGE_API_KEY", "test-key")
    monkeypatch.setattr(
        "ksadk.evaluation.evaluators._run_llm_judge",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("remote detail")),
    )

    metrics = evaluate_case(
        EvalCase(id="echo", turns=[{"input": "ping", "expectedOutput": "pong"}]),
        TargetRun(status="PASSED", output="pong"),
        ["llm_judge@v1"],
        EvaluationConfig(
            evaluators=["llm_judge@v1"],
            data_policy=DataPolicy.FULL_TRACE,
            judge_model="judge-model",
            judge_api_base="https://judge.example.invalid/v1",
        ),
    )

    assert metrics[0].status == "ERROR"
    assert metrics[0].evidence == {
        "evaluator": "llm_judge@v1",
        "reason": "Judge 执行失败",
        "errorType": "RuntimeError",
    }
