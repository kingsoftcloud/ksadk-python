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


def _tool_call_event(seq: int, name: str, arguments: str | dict, invocation_id: str = "inv1") -> SessionEvent:
    """构造真实 runtime 形态的 tool_call event。

    真实 runtime 把 tool_name/tool_args 放在 metadata(不是 content),content.role="model",
    content.text 是工具名字符串。这里严格对齐,确保 L2 在生产 event 上生效。
    """
    return SessionEvent(
        id=f"tc-{seq}",
        seq_id=seq,
        event_type="tool_call",
        author="runner",
        invocation_id=invocation_id,
        content={"role": "model", "text": name},
        metadata={"tool_name": name, "tool_args": arguments if isinstance(arguments, dict) else {"_raw": arguments}},
    )


def _tool_result_event(seq: int, ok: bool = True, error_type: str = "") -> SessionEvent:
    """构造真实 runtime 形态的 tool_result event。

    真实 runtime 的失败状态在 metadata.tool_receipt.status,不在 content.ok。
    """
    content = {"role": "tool", "text": "result content"}
    metadata = {"tool_name": "some_tool"}
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
        # 组1: search(query=a) → result_a;组2: search(query=a) → result_a2(覆盖)
        group1 = [_tool_call_event(1, "search", "query=a"), _tool_result_event(2)]
        group2 = [_tool_call_event(3, "search", "query=a"), _tool_result_event(4)]
        groups = [group1, group2]

        result = snip_redundant_groups(groups)

        # 组1 的 tool_call/tool_result 被覆盖移除,组2 保留。
        flat = [e for g in result.groups for e in g]
        seq_ids = [e.seq_id for e in flat]
        assert 1 not in seq_ids  # 旧 tool_call 被移除
        assert 2 not in seq_ids  # 旧 tool_result 被移除
        assert 3 in seq_ids
        assert 4 in seq_ids
        assert result.stats.removed_redundant_tool_results >= 1

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
        groups = [[_assistant_event(1, "x")], [_assistant_event(2, "y")], [_assistant_event(3, "z")], [_assistant_event(4, "w")]]
        original_len = len(groups)

        microcompact_cold_groups(groups)

        # 原 groups 列表不变。
        assert len(groups) == original_len

    def test_microcompact_preserved_receipts_collected(self):
        cold_event = _assistant_event(1, "x")
        cold_event.metadata = {"receipt_key": "deepresearch:web_search:v1"}
        groups = [cold_event, _assistant_event(2, "y")], [[_assistant_event(3, "z")]], [[_assistant_event(4, "w")]], [[_assistant_event(5, "v")]]
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
