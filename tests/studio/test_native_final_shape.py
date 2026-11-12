"""A final-answer shape failure may request repair, never semantic success."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ksadk.harness.native.completion import AcceptanceItem, TaskContract, reduce_completion
from ksadk.harness.native.ports import VerificationRequest
from ksadk.harness.native.state import RunScope
from ksadk.harness.native_runtime import TaskContractInput
from ksadk.studio.native_final_shape import (
    BROWSER_VERIFIER_REF,
    BROWSER_VERIFIER_VERSION,
    EVIDENCE_VERIFIER_REF,
    VERIFIER_REF,
    VERIFIER_VERSION,
    StudioFinalShapeVerifier,
    _written_web_entry,
    requested_exact_workspace_file,
    requested_final_shape,
)
from ksadk.studio.native_product import _goal_contract

SCOPE = RunScope(tenant_id="tenant", workspace_id="workspace", user_id="user", agent_id="agent")


def test_explicit_json_goal_pins_submission_protocol_only_for_current_goal():
    structured = _goal_contract(
        TaskContractInput(
            goal="分析文件。最后只返回 JSON 对象，键为 incidents 和 total_cents。不能修改文件。",
            session_id="session",
            parent_run_id=None,
            parent_goal=None,
            parent_contract=None,
        )
    )
    assert structured.final_submission_schema["required"] == ["output"]
    assert structured.final_submission_payload_key == "output"
    following = _goal_contract(
        TaskContractInput(
            goal="解释结果。",
            session_id="session",
            parent_run_id="parent",
            parent_goal="分析文件。",
            parent_contract=structured,
        )
    )
    assert following.final_submission_schema is None
    assert following.items[0].verifier_ref == EVIDENCE_VERIFIER_REF


def test_browser_entry_requires_observed_change_or_successful_write_receipt(tmp_path):
    entry = tmp_path / "app" / "index.html"
    entry.parent.mkdir()
    entry.write_text("<p>page</p>")
    proposed = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "write-1",
                "function": {
                    "name": "write_workspace_file",
                    "arguments": json.dumps({"path": "app/index.html", "content": "<p>page</p>"}),
                },
            }
        ],
    }

    def store(messages, changed=()):
        return SimpleNamespace(
            load=lambda *_: SimpleNamespace(messages=messages, workspace_modified_paths=changed)
        )

    failed = {"role": "tool", "tool_call_id": "write-1", "content": '{"status":"error"}'}
    assert _written_web_entry(store((proposed, failed)), SCOPE, "run", tmp_path) is None
    succeeded = {"role": "tool", "tool_call_id": "write-1", "content": '{"status":"ok"}'}
    assert (
        _written_web_entry(store((proposed, succeeded)), SCOPE, "run", tmp_path) == "app/index.html"
    )
    assert (
        _written_web_entry(store((), ("app/index.html",)), SCOPE, "run", tmp_path)
        == "app/index.html"
    )


@pytest.mark.parametrize(
    ("page_errors", "network_failures", "expected_summary"),
    [
        (("script error",), (), "Browser reported an error"),
        (
            ("ReferenceError: Three is not defined",),
            ("network_denied: https://example.invalid/runtime.js",),
            "Browser blocked a page request under the locked network policy",
        ),
        (
            ("error: Failed to load resource: the server responded with a status of 404",),
            ("asset_http_error:404: /missing/game.js",),
            "/missing/game.js",
        ),
    ],
)
@pytest.mark.asyncio
async def test_browser_failure_binds_evidence_to_observed_screenshot(
    tmp_path, page_errors, network_failures, expected_summary
):
    (tmp_path / "index.html").write_text("<p>broken</p>")

    class Browser:
        async def verify_page(self, **_kwargs):
            return SimpleNamespace(
                status="ok",
                status_code=200,
                controls=(),
                page_errors=page_errors,
                console=(),
                network_failures=network_failures,
                screenshot=SimpleNamespace(content_ref="sha256:" + "b" * 64),
            )

    store = SimpleNamespace(
        load=lambda *_: SimpleNamespace(messages=(), workspace_modified_paths=("index.html",))
    )
    browser_request = request("Build a working page.", "Done.").model_copy(
        update={
            "contract": TaskContract(
                revision="goal-v1",
                items=(
                    AcceptanceItem(
                        acceptance_id="current_user_goal",
                        verifier_ref=BROWSER_VERIFIER_REF,
                        verifier_version=BROWSER_VERIFIER_VERSION,
                        verification_method="deterministic",
                        description="Build a working page.",
                    ),
                ),
            ),
        }
    )
    verifier = StudioFinalShapeVerifier(
        SCOPE, build_id="build-1", workspace_root=tmp_path, store=store, browser=Browser()
    )
    result = await verifier.verify(browser_request)
    assert result.report is not None
    assert result.report.items[0].status == "failed"
    assert result.report.items[0].evidence_refs == ("sha256:" + "b" * 64,)
    assert expected_summary in result.report.items[0].safe_summary
    assert "example.invalid" not in result.report.items[0].safe_summary


@pytest.mark.asyncio
async def test_headless_pointer_lock_denial_does_not_claim_game_failure(tmp_path):
    (tmp_path / "index.html").write_text("<button>New Game</button>")

    class Browser:
        async def verify_page(self, **_kwargs):
            return SimpleNamespace(
                status="ok",
                status_code=200,
                controls=(),
                page_errors=("The root document of this element is not valid for pointer lock.",),
                console=(),
                network_failures=(),
                screenshot=None,
            )

    state = SimpleNamespace(
        messages=(),
        workspace_modified_paths=("index.html",),
        unresolved_interaction_observation=None,
        last_web_mutation_tool_call=1,
        last_browser_observation={"entry_path": "index.html", "tool_call": 2},
    )
    contract = TaskContract(
        revision="goal-v1",
        items=(
            AcceptanceItem(
                acceptance_id="current_user_goal",
                verifier_ref=BROWSER_VERIFIER_REF,
                verifier_version=BROWSER_VERIFIER_VERSION,
                verification_method="deterministic",
                description="Build a working page.",
            ),
        ),
    )
    result = await StudioFinalShapeVerifier(
        SCOPE,
        build_id="build-1",
        workspace_root=tmp_path,
        store=SimpleNamespace(load=lambda *_: state),
        browser=Browser(),
    ).verify(request("Build a working page.", "Done.").model_copy(update={"contract": contract}))
    assert result.report is not None
    assert result.report.items[0].status == "not_run"


@pytest.mark.asyncio
async def test_final_browser_check_rejects_unrechecked_interaction_failure(tmp_path):
    (tmp_path / "index.html").write_text("<button>Start</button>")

    class Browser:
        async def verify_page(self, **_kwargs):
            return SimpleNamespace(
                status="ok",
                status_code=200,
                controls=(),
                page_errors=(),
                console=(),
                network_failures=(),
                screenshot=None,
            )

    issue = {
        "tool_name": "browser_inspect_workspace",
        "entry_path": "index.html",
        "tool_call": 7,
        "error": "uncaught TypeError after Start",
        "evidence_ref": "sha256:" + "c" * 64,
    }
    store = SimpleNamespace(
        load=lambda *_: SimpleNamespace(
            messages=(),
            workspace_modified_paths=("index.html",),
            unresolved_interaction_observation=issue,
        )
    )
    browser_request = request("Build a working page.", "Done.").model_copy(
        update={
            "contract": TaskContract(
                revision="goal-v1",
                items=(
                    AcceptanceItem(
                        acceptance_id="current_user_goal",
                        verifier_ref=BROWSER_VERIFIER_REF,
                        verifier_version=BROWSER_VERIFIER_VERSION,
                        verification_method="deterministic",
                        description="Build a working page.",
                    ),
                ),
            ),
        }
    )
    result = await StudioFinalShapeVerifier(
        SCOPE, build_id="build-1", workspace_root=tmp_path, store=store, browser=Browser()
    ).verify(browser_request)
    assert result.report is not None
    assert result.report.items[0].status == "failed"
    assert result.report.items[0].evidence_refs == (issue["evidence_ref"],)
    assert "successfully rechecked" in result.report.items[0].safe_summary


@pytest.mark.asyncio
async def test_final_web_delivery_requires_current_run_browser_observation(tmp_path):
    (tmp_path / "index.html").write_text("<p>ready</p>")

    class Browser:
        async def verify_page(self, **_kwargs):
            return SimpleNamespace(
                status="ok",
                status_code=200,
                controls=(),
                page_errors=(),
                console=(),
                network_failures=(),
                screenshot=None,
            )

    state = SimpleNamespace(
        messages=(),
        workspace_modified_paths=("index.html",),
        unresolved_interaction_observation=None,
        last_web_mutation_tool_call=4,
        last_browser_observation=None,
    )
    store = SimpleNamespace(load=lambda *_: state)
    browser_request = request("Build a working page.", "Done.").model_copy(
        update={
            "contract": TaskContract(
                revision="goal-v1",
                items=(
                    AcceptanceItem(
                        acceptance_id="current_user_goal",
                        verifier_ref=BROWSER_VERIFIER_REF,
                        verifier_version=BROWSER_VERIFIER_VERSION,
                        verification_method="deterministic",
                        description="Build a working page.",
                    ),
                ),
            ),
        }
    )
    verifier = StudioFinalShapeVerifier(
        SCOPE, build_id="build-1", workspace_root=tmp_path, store=store, browser=Browser()
    )
    missing = await verifier.verify(browser_request)
    assert missing.report is not None
    assert missing.report.items[0].status == "failed"
    assert "browser tool" in missing.report.items[0].safe_summary
    state.last_browser_observation = {"entry_path": "index.html", "tool_call": 3}
    stale = await verifier.verify(browser_request)
    assert stale.report is not None and stale.report.items[0].status == "failed"
    state.last_browser_observation = {"entry_path": "index.html", "tool_call": 5}
    current = await verifier.verify(browser_request)
    assert current.report is not None and current.report.items[0].status == "not_run"
    state.last_browser_observation = {"entry_path": "autotest.html", "tool_call": 5}
    checkpoint = await verifier.verify(
        browser_request.model_copy(update={"purpose": "checkpoint", "final_text": ""})
    )
    assert checkpoint.report is not None
    assert checkpoint.report.items[0].status == "failed"
    assert "delivered web page" in checkpoint.report.items[0].safe_summary
    state.last_browser_observation = {"entry_path": "index.html", "tool_call": 5}
    visual_verifier = StudioFinalShapeVerifier(
        SCOPE,
        build_id="build-1",
        workspace_root=tmp_path,
        store=store,
        browser=Browser(),
        require_visual_feedback=True,
    )
    missing_visual = await visual_verifier.verify(browser_request)
    assert missing_visual.report is not None
    assert missing_visual.report.items[0].status == "failed"
    assert "visual-model description" in missing_visual.report.items[0].safe_summary
    state.last_browser_observation["vision_description_status"] = "available"
    visual_observed = await visual_verifier.verify(browser_request)
    assert visual_observed.report is not None
    assert visual_observed.report.items[0].status == "not_run"


def request(goal: str, answer: str) -> VerificationRequest:
    return VerificationRequest(
        scope=SCOPE,
        run_id="run",
        candidate_id="candidate",
        final_text=answer,
        contract=TaskContract(
            revision="goal-v1",
            items=(
                AcceptanceItem(
                    acceptance_id="current_user_goal",
                    verifier_ref=VERIFIER_REF,
                    verifier_version=VERIFIER_VERSION,
                    verification_method="deterministic",
                    description=goal,
                ),
            ),
        ),
        subject_context_digest="sha256:" + "a" * 64,
    )


def test_shape_recognition_is_narrow_and_does_not_infer_answer_values():
    assert requested_final_shape("Read the file. Return only the total.") == "number"
    assert (
        requested_final_shape(
            "Read the bound Skill instructions, then return only the instruction sentence "
            "from SKILL.md."
        )
        == "sentence"
    )
    assert requested_final_shape("Return the result with a short explanation.") is None
    assert requested_final_shape("Return only the total and explain it.") is None
    assert requested_final_shape("Return only the sentence and explain it.") is None
    assert requested_final_shape("Read the canvas. Reply with only those digits.") == "digits"
    assert requested_final_shape("计算 37 × 24，只回复数字。") == "number"
    assert (
        requested_final_shape('从 JSON {"ok":true} 提取 ok，只回复 JSON 布尔值。') == "json_boolean"
    )
    assert requested_final_shape("解释 JSON 布尔值的用法。") is None
    assert requested_final_shape("2024 年是闰年吗？只回复 是 或 否。") == ("是", "否")
    assert requested_final_shape("选择方案，只输出 左 或 右。") == ("左", "右")
    assert requested_final_shape("只回复 是 或 否，并解释原因。") is None


@pytest.mark.parametrize(
    "goal,shape",
    [
        ("Read the sources. Return only a JSON object.", "json_object"),
        ("Read the sources. Reply with only a JSON array.", "json_array"),
        ("最后只返回 JSON 对象，键为 incidents 和 total_cents。不能修改文件。", "json_object"),
        ("仅输出 JSON 数组。", "json_array"),
        ('最后只返回 JSON 对象，格式为 {"records":数组,"count":整数}。', "json_object"),
        ('仅输出 JSON 数组，格式是 [{"label":字符串}]。', "json_array"),
    ],
)
def test_structured_json_shape_is_an_explicit_output_clause(goal, shape):
    assert requested_final_shape(goal) == shape
    assert requested_final_shape("解释‘只返回 JSON 对象’这句话。") is None
    assert requested_final_shape("不要只返回 JSON 对象。") is None
    assert requested_final_shape("最后只返回 JSON 对象并解释原因。") is None
    assert requested_final_shape('解释“只返回 JSON 对象，格式为 {"x":1}”这句话。') is None
    assert requested_final_shape('不要只返回 JSON 对象，格式为 {"x":1}。') is None
    assert requested_final_shape('只返回 JSON 对象，格式为 {"x":1}，再解释原因。') is None
    assert requested_final_shape('只返回 JSON 对象，格式为 [{"x":1}]。') is None
    assert requested_final_shape('只返回 JSON 数组，格式为 {"x":1}。') is None


@pytest.mark.asyncio
async def test_json_shape_rejects_prose_and_fences_but_does_not_verify_semantics():
    verifier = StudioFinalShapeVerifier(SCOPE, build_id="build")
    goal = "最后只返回 JSON 对象，键为 incidents 和 total_cents。不能修改文件。"
    for bad in (
        'Here is the result: {"total_cents": 73}',
        '```json\n{"total_cents":73}\n```',
        '[{"total_cents":73}]',
        '{"total_cents":NaN}',
    ):
        result = await verifier.verify(request(goal, bad))
        assert result.report.items[0].status == "failed"
    result = await verifier.verify(request(goal, '{"total_cents":999}'))
    assert result.report.items[0].status == "not_run"
    # JSON shape alone proves no sum, citation, key completeness or user goal.
    assert "semantic" in result.report.items[0].safe_summary.lower()


@pytest.mark.asyncio
async def test_long_goal_keeps_an_explicit_json_clause_before_source_instructions():
    goal = (
        "最后只返回 JSON 对象，键为 incidents 和 total_cents。" + "必须保留准确的来源引用。" * 150
    )
    contract = _goal_contract(
        TaskContractInput(
            goal=goal,
            session_id="session",
            parent_run_id=None,
            parent_goal=None,
            parent_contract=None,
        )
    )
    assert len(contract.items[0].description) <= 1024
    assert requested_final_shape(contract.items[0].description) == "json_object"
    result = await StudioFinalShapeVerifier(SCOPE, build_id="build").verify(
        request(goal, '```json\n{"total_cents":73}\n```').model_copy(update={"contract": contract})
    )
    assert result.report.items[0].status == "failed"


@pytest.mark.asyncio
async def test_long_goal_keeps_its_final_shape_clause_in_the_pinned_contract():
    goal = "Inspect the files. " + ("Context details. " * 100) + "Return only the total."
    contract = _goal_contract(
        TaskContractInput(
            goal=goal,
            session_id="session",
            parent_run_id=None,
            parent_goal=None,
            parent_contract=None,
        )
    )
    assert len(contract.items[0].description) == 1024
    assert contract.items[0].description.endswith("Return only the total.")
    verifier = StudioFinalShapeVerifier(SCOPE, build_id="build-1")
    bad = await verifier.verify(
        request(goal, "The total is 42").model_copy(update={"contract": contract})
    )
    assert bad.report is not None
    assert bad.report.items[0].status == "failed"


@pytest.mark.asyncio
async def test_numeric_shape_failure_repairs_but_shape_success_is_unverified():
    verifier = StudioFinalShapeVerifier(SCOPE, build_id="build-1")
    bad = await verifier.verify(request("Return only the total.", "19 + 23 = 42"))
    assert bad.report.items[0].status == "failed"
    assert "42" not in bad.report.items[0].safe_summary
    assert (
        reduce_completion(
            request("Return only the total.", "19 + 23 = 42").contract, bad.candidate, bad.report
        ).action
        == "repair"
    )

    good_request = request("Return only the total.", "42")
    good = await verifier.verify(good_request)
    assert good.report.items[0].status == "not_run"
    decision = reduce_completion(good_request.contract, good.candidate, good.report)
    assert decision.action == "finalize" and decision.outcome == "unverified"


@pytest.mark.asyncio
async def test_exact_digits_shape_rejects_preamble_without_claiming_visual_truth():
    verifier = StudioFinalShapeVerifier(SCOPE, build_id="build-1")
    goal = "Read the canvas. Reply with only those digits."
    bad = await verifier.verify(request(goal, "The four digits are: 6428"))
    assert bad.report is not None
    assert bad.report.items[0].status == "failed"
    good = await verifier.verify(request(goal, "6428"))
    assert good.report.items[0].status == "not_run"


@pytest.mark.asyncio
async def test_explicit_chinese_json_boolean_rejects_markdown_without_claiming_truth():
    verifier = StudioFinalShapeVerifier(SCOPE, build_id="build-1")
    goal = '从 JSON {"ok":true} 提取 ok，只回复 JSON 布尔值。'
    for bad_text in ("`true`", "```json\ntrue\n```", "The answer is true"):
        bad = await verifier.verify(request(goal, bad_text))
        assert bad.report is not None
        assert bad.report.items[0].status == "failed"
        assert (
            reduce_completion(request(goal, bad_text).contract, bad.candidate, bad.report).action
            == "repair"
        )
    good = await verifier.verify(request(goal, "true"))
    assert good.report is not None
    assert good.report.items[0].status == "not_run"
    decision = reduce_completion(request(goal, "true").contract, good.candidate, good.report)
    assert decision.action == "finalize" and decision.outcome == "unverified"


@pytest.mark.asyncio
async def test_explicit_two_choice_final_rejects_punctuation_without_claiming_truth():
    verifier = StudioFinalShapeVerifier(SCOPE, build_id="build-1")
    goal = "2024 年是闰年吗？只回复 是 或 否。"
    bad_request = request(goal, "是。")
    bad = await verifier.verify(bad_request)
    assert bad.report is not None
    assert bad.report.items[0].status == "failed"
    assert reduce_completion(bad_request.contract, bad.candidate, bad.report).action == "repair"
    good_request = request(goal, "是")
    good = await verifier.verify(good_request)
    assert good.report is not None
    assert good.report.items[0].status == "not_run"
    assert (
        reduce_completion(good_request.contract, good.candidate, good.report).outcome
        == "unverified"
    )


@pytest.mark.asyncio
async def test_sentence_shape_rejects_preamble_and_unknown_goal_remains_unverified():
    verifier = StudioFinalShapeVerifier(SCOPE, build_id="build-1")
    goal = "Return only the instruction sentence from SKILL.md."
    bad = await verifier.verify(request(goal, "The sentence is:\n\n> Be concise."))
    assert bad.report.items[0].status == "failed"
    good = await verifier.verify(request(goal, "Be concise."))
    assert good.report.items[0].status == "not_run"
    unknown = await verifier.verify(request("Explain the Skill.", "Explanation"))
    assert unknown.report is None
    with pytest.raises(ValueError, match="scope_mismatch"):
        await verifier.verify(
            request(goal, "Be concise.").model_copy(
                update={"scope": SCOPE.model_copy(update={"tenant_id": "other"})}
            )
        )


@pytest.mark.asyncio
async def test_exact_workspace_file_is_observed_by_host_and_repairs_mismatch(tmp_path):
    goal = '在工作区创建 baseline/config.json，内容恰好是 {"enabled":true}。完成后回复路径。'
    requirement = requested_exact_workspace_file(goal)
    assert requirement is not None
    assert requirement.relative_path == "baseline/config.json"
    assert requirement.content == '{"enabled":true}'
    assert (
        requested_exact_workspace_file("在工作区创建 ../outside.json，内容恰好是 secret。") is None
    )
    assert requested_exact_workspace_file("请写一个 JSON 文件。") is None
    assert (
        requested_exact_workspace_file(
            '不要在工作区创建 baseline/config.json，内容恰好是 {"enabled":true}。'
        )
        is None
    )

    contract = _goal_contract(
        TaskContractInput(
            goal=goal,
            session_id="session",
            parent_run_id=None,
            parent_goal=None,
            parent_contract=None,
        )
    )
    assert [item.acceptance_id for item in contract.items] == [
        "current_user_goal",
        "exact_workspace_file",
    ]
    verifier = StudioFinalShapeVerifier(SCOPE, build_id="build-1", workspace_root=tmp_path)
    candidate_request = request(goal, "baseline/config.json").model_copy(
        update={"contract": contract}
    )
    path = tmp_path / "baseline" / "config.json"
    path.parent.mkdir()
    path.write_text('{"enabled": true}')
    mismatch = await verifier.verify(candidate_request)
    assert mismatch.report is not None
    assert [item.status for item in mismatch.report.items] == ["failed", "failed"]
    assert "UTF-8 hex" in mismatch.report.items[1].safe_summary
    assert reduce_completion(contract, mismatch.candidate, mismatch.report).action == "repair"

    path.write_text('{"enabled":true}')
    match = await verifier.verify(candidate_request)
    assert match.report is not None
    assert [item.status for item in match.report.items] == ["not_run", "passed"]
    decision = reduce_completion(contract, match.candidate, match.report)
    assert (decision.action, decision.outcome) == ("finalize", "partial")


@pytest.mark.asyncio
async def test_exact_workspace_file_cannot_follow_symlink_outside_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.json").write_text('{"enabled":true}')
    (root / "baseline").symlink_to(outside, target_is_directory=True)
    goal = '在工作区创建 baseline/config.json，内容恰好是 {"enabled":true}。'
    contract = _goal_contract(
        TaskContractInput(
            goal=goal,
            session_id="session",
            parent_run_id=None,
            parent_goal=None,
            parent_contract=None,
        )
    )
    verifier = StudioFinalShapeVerifier(SCOPE, build_id="build-1", workspace_root=root)
    observed = await verifier.verify(
        request(goal, "done").model_copy(update={"contract": contract})
    )
    assert observed.report is not None
    assert [item.status for item in observed.report.items] == ["failed", "failed"]
