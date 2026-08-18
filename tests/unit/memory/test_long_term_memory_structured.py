"""KsADK 长期记忆结构化能力单元测试（技术改造方案 §11.1）。

覆盖 14 条必须项：
  1.  显式自包含事实保存 flush=True -> CreateMemorySdk.Flush=true
  2.  普通自动保存请求不含 Flush / 显式 false
  3.  结构化 query 解析 MemoryId/正文/score/时间
  4.  update 请求含 collection/user/memory ID
  5.  UpdateMemory 响应 new_memory_id 解析为新句柄
  6.  delete 请求含 collection/user/memory ID
  7.  Session 状态映射 0/50/100/-50/-100
  8.  记录不存在稳定错误；重复删除 -> already_absent
  9.  State=-100 返回稳定失败，不自动重发/Flush
  10. QueryMemorySdk/ListMemories/UpdateMemory/DeleteMemory 脱敏真实 fixture 严格解析
  11. 未知 schema fail closed
  12. SDK 异常不泄露凭证
  13. 旧 search_entries() 仍返回文本列表
  14. backend capabilities 与真实实现一致

运行:
    .venv/bin/python -m pytest tests/unit/memory/test_long_term_memory_structured.py -v
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend
from ksadk.memory.models import (
    LongTermMemoryRecord,
    MemoryExtractionStatus,
    MemoryMutationResult,
    UnsupportedMemoryOperation,
    map_session_state,
)
from ksadk.memory.service import LongTermMemoryService


def _make_backend(**kwargs):
    defaults = {
        "index": "test_idx",
        "access_key": "test_ak",
        "secret_key": "test_sk",
        "namespace": "test_ns",
    }
    defaults.update(kwargs)
    return SdkLTMBackend(**defaults)


def _query_response(records):
    """构造 QueryMemorySdk 响应：Data[].Memories[]。"""
    return json.dumps({"Data": [{"Memories": records}]})


# ============================================================
# 1-2: Flush 语义
# ============================================================


class TestFlushSemantics:
    def test_explicit_self_contained_fact_sets_flush_true(self):
        """§11.1.1：显式自包含事实 flush=True -> CreateMemorySdk.Flush=true。"""
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = '{"RequestId": "r"}'
        with patch.object(backend, "_get_client", return_value=client):
            backend.save_memory(
                "u1",
                [json.dumps({"role": "user", "parts": [{"text": "记住X"}]}, ensure_ascii=False)],
                metadata={"session_id": "s1"},
                flush=True,
            )
        params = client.call.call_args[0][1]
        assert params["Flush"] is True
        assert params["SessionId"] == "s1"

    def test_ordinary_autosave_omits_flush(self):
        """§11.1.2：普通自动保存不含 Flush 或显式 false。"""
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = '{"RequestId": "r"}'
        with patch.object(backend, "_get_client", return_value=client):
            # 显式 flush=False -> 不携带 Flush 参数
            backend.save_memory(
                "u1",
                [json.dumps({"role": "user", "parts": [{"text": "普通对话"}]}, ensure_ascii=False)],
                metadata={"session_id": "s1"},
                flush=False,
            )
        params = client.call.call_args[0][1]
        assert "Flush" not in params
        # flush=None 且 metadata 不含 flush -> 同样不携带
        client.call.reset_mock()
        with patch.object(backend, "_get_client", return_value=client):
            backend.save_memory(
                "u1",
                [json.dumps({"role": "user", "parts": [{"text": "普通"}]}, ensure_ascii=False)],
                metadata={"session_id": "s1"},
            )
        params = client.call.call_args[0][1]
        assert "Flush" not in params

    def test_service_save_text_explicit_flush_overrides_metadata(self):
        """§7.6：显式 flush 参数优先于 metadata。"""
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = '{"RequestId": "r"}'
        svc = LongTermMemoryService(backend=backend)
        with patch.object(backend, "_get_client", return_value=client):
            # metadata flush=True，显式 flush=False -> 不 Flush
            svc.save_text(user_id="u1", content="x", metadata={"flush": True}, session_id="s1", flush=False)
        assert "Flush" not in client.call.call_args[0][1]
        # metadata flush=False，显式 flush=True -> Flush
        client.call.reset_mock()
        with patch.object(backend, "_get_client", return_value=client):
            svc.save_text(user_id="u1", content="x", metadata={"flush": False}, session_id="s1", flush=True)
        assert client.call.call_args[0][1]["Flush"] is True


# ============================================================
# 3: 结构化 query 解析
# ============================================================


class TestStructuredQueryParse:
    def test_structured_query_parses_fields(self):
        """§11.1.3：解析 MemoryId/正文/score/时间。"""
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = _query_response(
            [
                {
                    "MemoryId": "mem-1",
                    "Memory": "用户叫李建波",
                    "Score": 0.91,
                    "OccurredStart": "2026-01-01",
                    "OccurredEnd": "2026-01-02",
                    "AgentUserId": "u1",
                }
            ]
        )
        with patch.object(backend, "_get_client", return_value=client):
            records = backend.search_records("u1", "姓名", top_k=3)
        assert len(records) == 1
        r = records[0]
        assert r.memory_id == "mem-1"
        assert r.content == "用户叫李建波"
        assert r.score == 0.91
        assert r.user_id == "u1"
        assert r.metadata["OccurredStart"] == "2026-01-01"
        assert r.metadata["OccurredEnd"] == "2026-01-02"
        assert r.metadata["AgentUserId"] == "u1"


# ============================================================
# 4-6: update / delete 请求与响应
# ============================================================


class TestMutationRequests:
    def test_update_request_contains_scope_and_ids(self):
        """§11.1.4：update 请求含 collection/user/memory ID。"""
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = '{"new_memory_id": "mem-9"}'
        with patch.object(backend, "_get_client", return_value=client):
            r = backend.update_memory(user_id="u1", memory_id="mem-1", content="新正文")
        params = client.call.call_args[0][1]
        assert params["MemoryCollectionId"] == "test_ns"
        assert params["AgentUserId"] == "u1"
        assert params["MemoryId"] == "mem-1"
        assert params["Content"] == "新正文"
        assert client.call.call_args[0][0] == "UpdateMemory"

    def test_update_parses_new_memory_id(self):
        """§11.1.5：new_memory_id 解析为新句柄。"""
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = '{"Data": {"NewMemoryId": "mem-9"}}'
        with patch.object(backend, "_get_client", return_value=client):
            r = backend.update_memory(user_id="u1", memory_id="mem-1", content="x")
        assert r.ok and r.status == "updated"
        assert r.new_memory_id == "mem-9"
        # id 未变化时保留旧 id
        client.call.return_value = '{"RequestId": "r"}'
        with patch.object(backend, "_get_client", return_value=client):
            r = backend.update_memory(user_id="u1", memory_id="mem-1", content="x")
        assert r.ok and r.new_memory_id == "mem-1"

    def test_delete_request_contains_scope_and_ids(self):
        """§11.1.6：delete 请求含 collection/user/memory ID。"""
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = '{"RequestId": "r"}'
        with patch.object(backend, "_get_client", return_value=client):
            backend.delete_memory(user_id="u1", memory_id="mem-1")
        params = client.call.call_args[0][1]
        assert params["MemoryCollectionId"] == "test_ns"
        assert params["AgentUserId"] == "u1"
        assert params["MemoryId"] == "mem-1"
        assert client.call.call_args[0][0] == "DeleteMemory"


# ============================================================
# 7-9: 状态映射
# ============================================================


class TestSessionStateMapping:
    @pytest.mark.parametrize(
        "state,expected",
        [
            (0, "queued"),
            (50, "extracting"),
            (100, "extracted"),
            (-50, "duplicate_skipped"),
            (-100, "failed"),
        ],
    )
    def test_state_mapping(self, state, expected):
        """§11.1.7：Session 状态正确映射。"""
        assert map_session_state(state) == expected

    def test_get_extraction_status_state100(self):
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = json.dumps(
            {"Data": {"Items": [{"SessionId": "s1", "State": 100}]}}
        )
        with patch.object(backend, "_get_client", return_value=client):
            st = backend.get_extraction_status(user_id="u1", session_id="s1")
        assert st.status == "extracted" and st.state == 100

    def test_repeat_delete_maps_to_already_absent(self):
        """§11.1.8：重复删除 -> already_absent。"""
        backend = _make_backend()
        client = MagicMock()
        client.call.side_effect = RuntimeError("KsyunSDKException: 记忆不存在 [NotFoundMemory]")
        with patch.object(backend, "_get_client", return_value=client):
            r = backend.delete_memory(user_id="u1", memory_id="mem-1")
        assert r.ok and r.status == "already_absent"

    def test_update_not_found_maps_to_not_found(self):
        backend = _make_backend()
        client = MagicMock()
        client.call.side_effect = RuntimeError("ResourceNotFound: memory does not exist")
        with patch.object(backend, "_get_client", return_value=client):
            r = backend.update_memory(user_id="u1", memory_id="mem-x", content="x")
        assert not r.ok and r.status == "not_found"

    def test_state_negative100_returns_failed_no_retry(self):
        """§11.1.9：State=-100 返回稳定失败，不自动重发。"""
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = json.dumps(
            {"Data": {"Items": [{"SessionId": "s1", "State": -100}]}}
        )
        with patch.object(backend, "_get_client", return_value=client):
            st = backend.get_extraction_status(user_id="u1", session_id="s1")
        assert st.status == "failed"
        assert "重新" in st.message or "失败" in st.message
        # 不应触发任何重发写
        assert client.call.call_count == 1  # 仅 ListSessions 一次


# ============================================================
# 10-11: 严格 fixture 解析 / fail closed
# ============================================================


class TestStrictSchemaAndFailClosed:
    def test_list_memories_parses_memory_list(self):
        """§11.1.10：ListMemories 严格解析 MemoryList[]。"""
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = json.dumps(
            {
                "MemoryList": [
                    {
                        "MemoryId": "mem-2",
                        "Memory": "在快手工作",
                        "CreatedAt": "2026-08-18T08:00:00Z",
                        "UpdatedAt": "2026-08-18T09:00:00Z",
                    }
                ],
                "Total": 1,
            }
        )
        with patch.object(backend, "_get_client", return_value=client):
            lst = backend.list_memory_records(user_id="u1")
        assert lst[0].memory_id == "mem-2"
        assert lst[0].content == "在快手工作"
        assert lst[0].created_at == "2026-08-18T08:00:00Z"

    def test_unknown_query_schema_fail_closed(self):
        """§11.1.11：未知 QueryMemorySdk schema fail closed（返回空）。"""
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = '{"Whatever": 1}'
        with patch.object(backend, "_get_client", return_value=client):
            assert backend.search_records("u1", "q") == []

    def test_unknown_list_memories_schema_fail_closed(self):
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = '{"Foo": []}'
        with patch.object(backend, "_get_client", return_value=client):
            assert backend.list_memory_records(user_id="u1") == []

    def test_item_without_memory_id_skipped(self):
        """缺少 MemoryId 的条目跳过（不能支撑 mutation）。"""
        backend = _make_backend()
        client = MagicMock()
        client.call.return_value = _query_response(
            [{"Memory": "无 ID 记忆"}, {"MemoryId": "mem-1", "Memory": "有 ID"}]
        )
        with patch.object(backend, "_get_client", return_value=client):
            records = backend.search_records("u1", "q")
        assert len(records) == 1
        assert records[0].memory_id == "mem-1"


# ============================================================
# 12: 异常不泄露凭证
# ============================================================


class TestErrorSanitization:
    def test_sdk_exception_message_kept_out_of_result(self, caplog):
        """§11.1.12：SDK 异常不会泄露凭证到结果对象。"""
        backend = _make_backend()
        client = MagicMock()
        secret = RuntimeError("AK=SKLEAKED endpoint=internal.example secret=not-for-logs")
        client.call.side_effect = secret
        with caplog.at_level(logging.ERROR):
            with patch.object(backend, "_get_client", return_value=client):
                r = backend.update_memory(user_id="u1", memory_id="mem-1", content="x")
        assert r.status == "failed"
        assert "SKLEAKED" not in r.message and "internal.example" not in r.message
        assert "SKLEAKED" not in caplog.text and "not-for-logs" not in caplog.text


# ============================================================
# 13: 旧接口兼容
# ============================================================


class TestLegacyCompatibility:
    def test_search_entries_returns_text_list(self):
        """§11.1.13：旧 search_entries() 仍返回文本列表。"""
        from ksadk.memory.adk.backends.inmemory_ltm_backend import InMemoryLTMBackend

        svc = LongTermMemoryService(backend=InMemoryLTMBackend(index="t"))
        svc.save_text(user_id="u1", content="用户叫陈明辉", flush=False)
        entries = svc.search_entries(user_id="u1", query="姓名")
        assert isinstance(entries, list) and all(isinstance(e, str) for e in entries)

    def test_unsupported_update_raises_stable_error(self):
        """旧 backend 不支持 update 时抛稳定异常。"""
        from ksadk.memory.adk.backends.http_ltm_backend import HttpLTMBackend

        backend = HttpLTMBackend(index="t", base_url="https://x")
        with pytest.raises(UnsupportedMemoryOperation):
            backend.update_memory(user_id="u", memory_id="m", content="c")


# ============================================================
# 14: capabilities 一致
# ============================================================


class TestCapabilities:
    def test_sdk_capabilities_match_real_impl(self):
        """§11.1.14：SDK backend capabilities 与真实实现一致。"""
        backend = _make_backend()
        caps = backend.capabilities()
        assert {"search", "add", "flush", "structured_search", "update", "delete", "session_status"} <= caps

    def test_inmemory_capabilities_match_real_impl(self):
        from ksadk.memory.adk.backends.inmemory_ltm_backend import InMemoryLTMBackend

        caps = InMemoryLTMBackend(index="t").capabilities()
        assert {"search", "add", "structured_search", "update", "delete"} <= caps
        # 不声明 flush/session_status（无后台提取过程）
        assert "flush" not in caps and "session_status" not in caps

    def test_http_capabilities_default_only_search_add(self):
        from ksadk.memory.adk.backends.http_ltm_backend import HttpLTMBackend

        caps = HttpLTMBackend(index="t", base_url="https://x").capabilities()
        assert caps == {"search", "add"}


# ============================================================
# Service 层写后确认（§7.7）集成
# ============================================================


class TestServiceWriteConfirm:
    def test_confirm_searchable_matches_content(self):
        backend = _make_backend()
        client = MagicMock()

        def fake_call(action, params, options=None):
            if action == "ListSessions":
                return json.dumps({"Data": {"Items": [{"SessionId": "s1", "State": 100}]}})
            if action == "ListMemories":
                return json.dumps({"MemoryList": [{"MemoryId": "mem-1", "Memory": "用户叫李建波"}]})
            raise AssertionError(action)

        client.call.side_effect = fake_call
        svc = LongTermMemoryService(backend=backend)
        with patch.object(backend, "_get_client", return_value=client):
            st = svc.get_extraction_status(
                user_id="u1", session_id="s1", confirm_searchable=True, expected_content="用户叫李建波"
            )
        assert st.status == "extracted" and st.searchable is True

    def test_confirm_searchable_mismatch_stays_false(self):
        backend = _make_backend()
        client = MagicMock()

        def fake_call(action, params, options=None):
            if action == "ListSessions":
                return json.dumps({"Data": {"Items": [{"SessionId": "s1", "State": 100}]}})
            if action == "ListMemories":
                return json.dumps({"MemoryList": [{"MemoryId": "mem-1", "Memory": "其他事实"}]})
            raise AssertionError(action)

        client.call.side_effect = fake_call
        svc = LongTermMemoryService(backend=backend)
        with patch.object(backend, "_get_client", return_value=client):
            st = svc.get_extraction_status(
                user_id="u1", session_id="s1", confirm_searchable=True, expected_content="用户叫李建波"
            )
        assert st.searchable is False
