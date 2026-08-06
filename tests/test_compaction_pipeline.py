from __future__ import annotations

from ksadk.conversations.compaction_pipeline import (
    SnipResult,
    build_working_set_metadata,
    candidate_tokens,
    microcompact_cold_groups,
    run_pipeline,
    snip_redundant_groups,
)
from ksadk.sessions.base import SessionEvent


def _tool_call_event(
    seq: int,
    name: str,
    arguments: str | dict,
    invocation_id: str = "inv1",
    run_id: str | None = None,
) -> SessionEvent:
    """构造真实 runtime 形态的 tool_call event。

    真实 runtime 把 tool_name/tool_args 放在 metadata(不是 content),content.role="model",
    content.text 是工具名字符串。run_id 关联配对的 tool_result(同一 call/result 共享 run_id)。
    这里严格对齐,确保 L2 在生产 event 上生效。
    """
    metadata = {
        "tool_name": name,
        "tool_args": arguments if isinstance(arguments, dict) else {"_raw": arguments},
    }
    if run_id is not None:
        metadata["run_id"] = run_id
    return SessionEvent(
        id=f"tc-{seq}",
        seq_id=seq,
        event_type="tool_call",
        author="runner",
        invocation_id=invocation_id,
        content={"role": "model", "text": name},
        metadata=metadata,
    )


def _tool_result_event(
    seq: int, ok: bool = True, error_type: str = "", run_id: str | None = None
) -> SessionEvent:
    """构造真实 runtime 形态的 tool_result event。

    真实 runtime 的失败状态在 metadata.tool_receipt.status,不在 content.ok。
    run_id 关联配对的 tool_call(与 _tool_call_event 共享同一 run_id)。
    """
    content = {"role": "tool", "text": "result content"}
    metadata = {"tool_name": "some_tool"}
    if run_id is not None:
        metadata["run_id"] = run_id
    if not ok:
        metadata["tool_receipt"] = {"status": "failed"}
        if error_type:
            metadata["error_type"] = error_type
    return SessionEvent(
        id=f"tr-{seq}",
        seq_id=seq,
        event_type="tool_result",
        author="tool",
        content=content,
        metadata=metadata,
    )


def _assistant_event(seq: int, text: str) -> SessionEvent:
    return SessionEvent(
        id=f"a-{seq}",
        seq_id=seq,
        event_type="assistant_message",
        author="assistant",
        content={"role": "assistant", "text": text},
    )


class TestSnipDoesNotMutateTranscript:
    """Codex 纪律:L2 只作用于 candidate 投影,绝不改原 SessionEvent 实例和 transcript。"""

    def test_snip_returns_new_groups_not_original(self):
        original_call = _tool_call_event(1, "search", "query=a")
        original_result = _tool_result_event(2)
        groups = [[original_call, original_result]]
        original_call_id_before = id(original_call)
        original_result_content_before = dict(original_result.content)

        result = snip_redundant_groups(groups)

        # 原 SessionEvent 实例 id 不变(没被替换)。
        assert id(groups[0][0]) == original_call_id_before
        # 原 content 不变。
        assert groups[0][1].content == original_result_content_before
        # 返回的是新 list(浅拷贝),不是原 groups 对象。
        assert result.groups is not groups

    def test_snip_does_not_call_service_or_delete_events(self):
        # L2 是纯函数,不持有 service 引用,无法删 transcript。
        groups = [[_tool_call_event(1, "x", "1"), _tool_result_event(2)]]
        result = snip_redundant_groups(groups)
        # 原 groups 长度不变(transcript 没被删)。
        assert len(groups[0]) == 2
        # candidate 是新结构。
        assert isinstance(result, SnipResult)
        assert isinstance(result.stats.tokens_before, int)


class TestSnipRedundancyRemoval:
    def test_removes_tool_result_covered_by_later_group(self):
        # 组1: search(query=a) [run_id=r1] → result [run_id=r1];
        # 组2: search(query=a) [run_id=r2] → result [run_id=r2](覆盖)
        group1 = [
            _tool_call_event(1, "search", "query=a", run_id="r1"),
            _tool_result_event(2, run_id="r1"),
        ]
        group2 = [
            _tool_call_event(3, "search", "query=a", run_id="r2"),
            _tool_result_event(4, run_id="r2"),
        ]
        groups = [group1, group2]

        result = snip_redundant_groups(groups)

        # 组1 的 tool_call/tool_result 按 run_id 精确配对移除,组2 保留。
        flat = [e for g in result.groups for e in g]
        seq_ids = [e.seq_id for e in flat]
        assert 1 not in seq_ids  # 旧 tool_call 被移除
        assert 2 not in seq_ids  # 配对 tool_result(run_id=r1)被精确移除,不留孤儿
        assert 3 in seq_ids
        assert 4 in seq_ids
        assert result.stats.removed_redundant_tool_results >= 2  # call+result 成对

    def test_parallel_tool_calls_no_orphan_result(self):
        """parallel_tool_calls=True 时同轮多 tool_call 交错。

        按 run_id 精确配对，不留下孤儿结果。
        同组两个 tool_call(search + web_fetch)交错，search 被后续组覆盖。
        旧实现(邻接配对)会误删 web_fetch 的 result 或留 search 的孤儿 result;
        新实现(按 run_id 配对)只删 search 的 call+result,web_fetch 完整保留。
        """
        # 组1: 两个并行 tool_call + 交错 result(search[r1] 被 web_fetch[r2] 隔开)
        group1 = [
            _tool_call_event(1, "search", "query=a", run_id="r1"),
            _tool_call_event(2, "web_fetch", "url=x", run_id="r2"),
            _tool_result_event(3, run_id="r1"),  # search 的 result(被覆盖,应删)
            _tool_result_event(4, run_id="r2"),  # web_fetch 的 result(保留)
        ]
        # 组2: search(query=a) 覆盖组1 的 search
        group2 = [
            _tool_call_event(5, "search", "query=a", run_id="r3"),
            _tool_result_event(6, run_id="r3"),
        ]
        groups = [group1, group2]

        result = snip_redundant_groups(groups)

        flat = [e for g in result.groups for e in g]
        seq_ids = [e.seq_id for e in flat]
        # search[r1] 的 call(1) 和 result(3) 都被移除(按 run_id 精确配对)。
        assert 1 not in seq_ids
        assert 3 not in seq_ids
        # web_fetch[r2] 的 call(2) 和 result(4) 完整保留(不因邻接被误删)。
        assert 2 in seq_ids
        assert 4 in seq_ids
        # 组2 的 search[r3] 保留。
        assert 5 in seq_ids
        assert 6 in seq_ids

    def test_missing_run_id_keeps_results_conservatively(self):
        """run_id 缺失时(退化到 invocation_id 共享)保守不误删 tool_result。

        真实 LangGraph 路径 chunk 始终带 per-tool run_id,但若 chunk 无 run_id,
        tool_result 退化到 prepared.invocation_id(整轮共享)。此时 covered_run_ids
        不收录(配对退化),tool_result 全部保留,避免误删未配对结果。
        """
        # 两个 tool_call 都无 run_id(模拟退化路径),即使 search 被覆盖,result 也保留。
        group1 = [_tool_call_event(1, "search", "query=a"), _tool_result_event(2)]  # 无 run_id
        group2 = [_tool_call_event(3, "search", "query=a"), _tool_result_event(4)]  # 无 run_id
        groups = [group1, group2]

        result = snip_redundant_groups(groups)

        flat = [e for g in result.groups for e in g]
        seq_ids = [e.seq_id for e in flat]
        # tool_call(1)被覆盖移除(signature 覆盖判定不依赖 run_id);但 tool_result(2)因无 run_id
        # 无法精确配对,保守保留(不误删)。这是有意的保守行为,避免 run_id 缺失时误删 result。
        assert 1 not in seq_ids  # tool_call 覆盖移除(不依赖 run_id)
        assert 2 in seq_ids  # tool_result 无 run_id,保守保留
        assert 3 in seq_ids and 4 in seq_ids

    def test_keeps_different_tool_results(self):
        group1 = [_tool_call_event(1, "search", "query=a"), _tool_result_event(2)]
        group2 = [_tool_call_event(3, "web_fetch", "url=x"), _tool_result_event(4)]
        groups = [group1, group2]

        result = snip_redundant_groups(groups)

        flat = [e for g in result.groups for e in g]
        assert len(flat) == 4  # 不同 tool 不覆盖,全保留

    def test_disabled_via_env_returns_unchanged(self, monkeypatch):
        monkeypatch.setenv("KSADK_COMPACT_SNIP_ENABLED", "false")
        group1 = [_tool_call_event(1, "search", "query=a"), _tool_result_event(2)]
        group2 = [_tool_call_event(3, "search", "query=a"), _tool_result_event(4)]
        groups = [group1, group2]

        result = snip_redundant_groups(groups)

        flat = [e for g in result.groups for e in g]
        assert len(flat) == 4  # 禁用后不移除
        assert result.stats.removed_redundant_tool_results == 0


class TestMicrocompactProjection:
    """Codex 纪律:L3 合成摘要 event 只存在于 candidate,不写回 transcript。"""

    def test_microcompact_replaces_cold_groups_with_summary_event(self):
        # 5 组,默认 cold_rounds=3,前 2 组视为冷组。
        groups = [
            [_assistant_event(1, "old round 1")],
            [_assistant_event(2, "old round 2")],
            [_assistant_event(3, "tail 1")],
            [_assistant_event(4, "tail 2")],
            [_assistant_event(5, "tail 3")],
        ]

        result = microcompact_cold_groups(groups)

        # candidate: [合成摘要组] + 尾部 3 组。
        assert len(result.groups) == 4
        # 第一组是合成摘要 event。
        summary_event = result.groups[0][0]
        assert summary_event.event_type == "assistant_message"
        assert summary_event.content.get("role") == "assistant"
        assert "microcompact" in (summary_event.content.get("text") or "").lower()
        assert summary_event.metadata.get("compaction_projection") is True
        assert summary_event.metadata.get("compaction_stage") == "microcompact"
        # 尾部组保留原 event。
        assert result.groups[1][0].seq_id == 3

    def test_microcompact_does_not_mutate_original_groups(self):
        groups = [
            [_assistant_event(1, "x")],
            [_assistant_event(2, "y")],
            [_assistant_event(3, "z")],
            [_assistant_event(4, "w")],
        ]
        original_len = len(groups)

        microcompact_cold_groups(groups)

        # 原 groups 列表不变。
        assert len(groups) == original_len

    def test_microcompact_preserved_receipts_collected(self):
        cold_event = _assistant_event(1, "x")
        cold_event.metadata = {"receipt_key": "deepresearch:web_search:v1"}
        _groups = (
            [cold_event, _assistant_event(2, "y")],
            [[_assistant_event(3, "z")]],
            [[_assistant_event(4, "w")]],
            [[_assistant_event(5, "v")]],
        )
        # 构造 4+ 组让前几组变冷。
        flat_groups = [
            [cold_event, _assistant_event(2, "y")],
            [_assistant_event(3, "z")],
            [_assistant_event(4, "w")],
            [_assistant_event(5, "v")],
        ]
        result = microcompact_cold_groups(flat_groups)
        assert "deepresearch:web_search:v1" in result.stats.preserved_receipts

    def test_microcompact_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("KSADK_COMPACT_MICROCOMPACT_ENABLED", "false")
        groups = [[_assistant_event(1, "x")]] * 5
        result = microcompact_cold_groups(groups)
        assert result.stats.compacted_groups == 0
        assert len(result.groups) == 5  # 原样返回


class TestPipelineProgressiveStop:
    def test_pipeline_stops_after_snip_if_under_threshold(self):
        # 单组小内容,snip 后 token 远低于阈值。
        groups = [[_assistant_event(1, "small")]]
        result = run_pipeline(groups, threshold_tokens=100000)
        assert "snip" in result["pipeline_stages"]
        assert "microcompact" not in result["pipeline_stages"]  # 没超阈值,不进 L3

    def test_pipeline_runs_microcompact_if_over_threshold(self):
        # 5 组大内容,snip 后仍超低阈值,触发 L3。
        big = "x" * 5000
        groups = [[_assistant_event(i, big)] for i in range(1, 6)]
        result = run_pipeline(groups, threshold_tokens=100)
        assert "microcompact" in result["pipeline_stages"]
        # L3 后 candidate 应含合成摘要 + 尾部组。
        assert len(result["candidate_groups"]) <= 4

    def test_pipeline_returns_stats(self):
        groups = [[_assistant_event(1, "x")], [_assistant_event(2, "y")]]
        result = run_pipeline(groups, threshold_tokens=100000)
        assert "snip_stats" in result
        assert "tokens_before" in result
        assert "tokens_after" in result
        assert result["tokens_before"] >= result["tokens_after"]


class TestCandidateTokens:
    def test_empty_groups_zero_tokens(self):
        assert candidate_tokens([]) == 0

    def test_counts_all_events(self):
        groups = [[_assistant_event(1, "hello world")]]
        tokens = candidate_tokens(groups)
        assert tokens > 0


class TestWorkingSetMetadata:
    def test_conservative_metadata_no_file_content(self):
        # Codex 纪律:L5 第一版只记 metadata,不读内容。
        ws = build_working_set_metadata(
            pinned_state={"pending_approvals": ["appr1"], "current_user_goal": "deploy"},
            recent_files=[{"path": "/a/b.md", "mtime_ns": 1, "size_bytes": 100}],
            active_tools=["web_search", "web_fetch"],
        )
        assert ws["recent_files"] == [{"path": "/a/b.md", "mtime_ns": 1, "size_bytes": 100}]
        assert "content" not in ws["recent_files"][0]  # 不含文件内容
        assert ws["active_tools"] == ["web_search", "web_fetch"]
        assert ws["pinned_approvals"] == ["appr1"]
        assert ws["current_user_goal"] == "deploy"

    def test_caps_recent_files(self, monkeypatch):
        monkeypatch.setenv("KSADK_WORKING_SET_MAX_FILES", "2")
        files = [{"path": f"/f{i}"} for i in range(5)]
        ws = build_working_set_metadata(pinned_state={}, recent_files=files)
        assert len(ws["recent_files"]) == 2  # 截断到 2
