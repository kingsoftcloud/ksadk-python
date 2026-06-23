"""KsADK 记忆库 ADK 模块综合单元测试

覆盖 ADK 记忆模块的全部接口和使用流程，分 8 个测试类:

  A. TestInMemoryLTMBackendExtended  - InMemoryLTMBackend 边界测试
  B. TestHttpLTMBackend              - HttpLTMBackend Mock HTTP 测试
  C. TestSdkLTMBackend               - SdkLTMBackend Mock SDK 测试
  D. TestLongTermMemoryInit          - LongTermMemory 构造和工厂方法
  E. TestLongTermMemoryEventFiltering - 事件过滤逻辑
  F. TestLongTermMemorySearchMemory  - 检索和响应解析
  G. TestShortTermMemory             - ShortTermMemory 会话管理
  H. TestADKRunnerMemoryIntegration  - ADKRunner 记忆集成

所有测试纯本地运行，不依赖 LLM / 远程 API。

运行方式:
    .venv/bin/python -m pytest tests/unit/memory/test_adk_memory_comprehensive.py -v
"""

import json
import os
import time
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# ============================================================
# A. TestInMemoryLTMBackendExtended
# ============================================================


class TestInMemoryLTMBackendExtended:
    """InMemoryLTMBackend 边界场景和详细行为测试"""

    def _make_backend(self, index="test_app"):
        from ksadk.memory.adk.backends.inmemory_ltm_backend import InMemoryLTMBackend
        return InMemoryLTMBackend(index=index)

    def test_index_property(self):
        backend = self._make_backend(index="my_custom_index")
        assert backend.index == "my_custom_index"

    def test_save_empty_list_returns_true(self):
        backend = self._make_backend()
        assert backend.save_memory("user_1", []) is True
        assert backend.search_memory("user_1", "anything") == []

    def test_unicode_special_characters(self):
        backend = self._make_backend()
        events = [
            json.dumps({"text": "我喜欢🎉派对和日本語テスト"}, ensure_ascii=False),
            json.dumps({"text": "特殊字符: <>&\"'\\n\\t"}, ensure_ascii=False),
        ]
        assert backend.save_memory("u1", events) is True
        results = backend.search_memory("u1", "派对", top_k=5)
        assert len(results) >= 1
        assert any("派对" in r for r in results)

    def test_large_volume_memory(self):
        backend = self._make_backend()
        events = [f"memory_item_{i}: topic_{i % 10}" for i in range(200)]
        assert backend.save_memory("u1", events) is True
        results = backend.search_memory("u1", "topic_5", top_k=10)
        assert len(results) == 10

    def test_top_k_limits_results(self):
        backend = self._make_backend()
        events = [f"event_{i}" for i in range(10)]
        backend.save_memory("u1", events)

        results_3 = backend.search_memory("u1", "event", top_k=3)
        assert len(results_3) == 3

        results_1 = backend.search_memory("u1", "event", top_k=1)
        assert len(results_1) == 1

        # top_k > total: returns all
        results_100 = backend.search_memory("u1", "event", top_k=100)
        assert len(results_100) == 10

    def test_no_match_returns_recent(self):
        """查询无匹配时，返回最近 top_k 条记忆"""
        backend = self._make_backend()
        events = [f"event_{i}" for i in range(5)]
        backend.save_memory("u1", events)

        results = backend.search_memory("u1", "completely_unrelated_xyz", top_k=3)
        assert len(results) == 3
        # 应该是最后 3 条
        assert results == events[-3:]

    def test_multiple_users_isolation(self):
        backend = self._make_backend()
        for i in range(5):
            backend.save_memory(f"user_{i}", [f"secret_data_for_user_{i}"])

        for i in range(5):
            results = backend.search_memory(f"user_{i}", f"secret_data_for_user_{i}")
            assert len(results) == 1
            assert f"user_{i}" in results[0]
            # 不应搜到其他用户的精确数据
            for j in range(5):
                if j != i:
                    other_results = backend.search_memory(
                        f"user_{i}", f"secret_data_for_user_{j}"
                    )
                    assert not any(f"user_{j}" in r for r in other_results)

    def test_full_match_scores_higher(self):
        """完整匹配得分 (+10) 高于部分关键词匹配 (+1)"""
        backend = self._make_backend()
        backend.save_memory("u1", [
            "I love Python programming",      # 完整匹配 "Python programming"
            "Python is good",                  # 仅部分匹配 "Python"
        ])
        results = backend.search_memory("u1", "Python programming", top_k=2)
        assert len(results) == 2
        # 完整匹配的应该排在前面
        assert "Python programming" in results[0]


# ============================================================
# B. TestHttpLTMBackend
# ============================================================


class TestHttpLTMBackend:
    """HttpLTMBackend Mock HTTP 测试"""

    def _make_backend(self, base_url="http://test.local", token="test-token"):
        from ksadk.memory.adk.backends.http_ltm_backend import HttpLTMBackend
        return HttpLTMBackend(index="test", base_url=base_url, token=token)

    def test_empty_base_url_save_returns_false(self):
        backend = self._make_backend(base_url="")
        assert backend.save_memory("u1", ["event"]) is False

    def test_empty_base_url_search_returns_empty(self):
        backend = self._make_backend(base_url="")
        assert backend.search_memory("u1", "query") == []

    def test_save_memory_success(self):
        backend = self._make_backend()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        backend._client = mock_client

        result = backend.save_memory("u1", ["event_1", "event_2"])
        assert result is True
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        assert payload["user_id"] == "u1"
        assert payload["events"] == ["event_1", "event_2"]

    def test_save_memory_http_error(self):
        import httpx
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_client.post.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=mock_response
        )
        backend._client = mock_client

        assert backend.save_memory("u1", ["event"]) is False

    def test_search_memory_success(self):
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"memories": ["mem_1", "mem_2"]}
        mock_client.post.return_value = mock_response
        backend._client = mock_client

        results = backend.search_memory("u1", "query", top_k=5)
        assert results == ["mem_1", "mem_2"]

    def test_search_memory_http_error(self):
        import httpx
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        mock_client.post.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=mock_response
        )
        backend._client = mock_client

        assert backend.search_memory("u1", "query") == []

    def test_client_lazy_init(self):
        backend = self._make_backend()
        assert backend._client is None
        client1 = backend.client
        assert backend._client is not None
        client2 = backend.client
        assert client1 is client2

    def test_token_in_headers(self):
        backend = self._make_backend(token="my-secret-token")
        client = backend.client
        assert "Authorization" in client.headers
        assert client.headers["Authorization"] == "Bearer my-secret-token"

    def test_close_resets_client(self):
        backend = self._make_backend()
        _ = backend.client  # trigger lazy init
        assert backend._client is not None
        backend.close()
        assert backend._client is None


# ============================================================
# C. TestSdkLTMBackend
# ============================================================


class TestSdkLTMBackend:
    """SdkLTMBackend Mock SDK 测试"""

    def _make_backend(self, **kwargs):
        from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend
        defaults = {
            "index": "test_idx",
            "access_key": "test_ak",
            "secret_key": "test_sk",
            "namespace": "test_ns",
        }
        defaults.update(kwargs)
        return SdkLTMBackend(**defaults)

    def test_init_no_credentials_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend
            backend = SdkLTMBackend(index="test", access_key="", secret_key="")
        assert "AK/SK not provided" in caplog.text

    def test_save_empty_events_returns_true(self):
        backend = self._make_backend()
        assert backend.save_memory("u1", []) is True

    def test_save_calls_create_memory_sdk(self):
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.call.return_value = '{"RequestId": "123"}'

        with patch.object(backend, '_get_client', return_value=mock_client):
            event = json.dumps(
                {"role": "user", "parts": [{"text": "hello"}]},
                ensure_ascii=False,
            )
            result = backend.save_memory(
                "u1",
                [event],
                metadata={"agent_id": "agent-1", "session_id": "sess-1"},
            )

        assert result is True
        mock_client.call.assert_called_once()
        call_args = mock_client.call.call_args
        assert call_args[0][0] == "CreateMemorySdk"
        params = call_args[0][1]
        assert params["MemoryCollectionId"] == "test_ns"
        assert params["AgentUserId"] == "u1"
        assert params["AgentId"] == "agent-1"
        assert params["SessionId"] == "sess-1"
        assert params["SceneId"] == "_sys_general"
        assert params["DataType"] == "conversation"
        assert "Namespace" not in params
        assert "UserId" not in params

    def test_save_data_conversation_format(self):
        """验证 Data 字段为 {"Conversation": [...]} 结构"""
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.call.return_value = "{}"

        with patch.object(backend, '_get_client', return_value=mock_client):
            event = json.dumps(
                {"role": "user", "parts": [{"text": "test msg"}]},
                ensure_ascii=False,
            )
            backend.save_memory("u1", [event])

        params = mock_client.call.call_args[0][1]
        assert "Data" in params
        assert "Conversation" in params["Data"]
        assert isinstance(params["Data"]["Conversation"], list)
        assert len(params["Data"]["Conversation"]) == 1

    def test_save_conversation_item_fields(self):
        """每个 Conversation 项必须有 Role/CreatedAt/MessageId/Content"""
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.call.return_value = "{}"

        with patch.object(backend, '_get_client', return_value=mock_client):
            event = json.dumps(
                {"role": "user", "parts": [{"text": "hello world"}]},
                ensure_ascii=False,
            )
            backend.save_memory("u1", [event])

        params = mock_client.call.call_args[0][1]
        item = params["Data"]["Conversation"][0]
        assert item["Role"] == "user"
        assert isinstance(item["CreatedAt"], int)
        assert item["CreatedAt"] > 0
        assert len(item["MessageId"]) > 0
        assert item["Content"] == [{"Type": "input_text", "Text": "hello world"}]

    def test_save_parses_event_json(self):
        """从 ADK event JSON 正确提取 role 和 text"""
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.call.return_value = "{}"

        with patch.object(backend, '_get_client', return_value=mock_client):
            events = [
                json.dumps({"role": "user", "parts": [{"text": "msg_1"}]}),
                json.dumps({"role": "user", "parts": [{"text": "msg_2"}]}),
            ]
            backend.save_memory("u1", events)

        params = mock_client.call.call_args[0][1]
        conv = params["Data"]["Conversation"]
        assert len(conv) == 2
        assert conv[0]["Content"][0]["Text"] == "msg_1"
        assert conv[1]["Content"][0]["Text"] == "msg_2"

    def test_save_plain_text_fallback(self):
        """非 JSON 格式的事件字符串按纯文本处理"""
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.call.return_value = "{}"

        with patch.object(backend, '_get_client', return_value=mock_client):
            backend.save_memory("u1", ["plain text message"])

        params = mock_client.call.call_args[0][1]
        item = params["Data"]["Conversation"][0]
        assert item["Role"] == "user"
        assert item["Content"][0]["Text"] == "plain text message"

    def test_save_exception_returns_false(self):
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.call.side_effect = Exception("SDK error")

        with patch.object(backend, '_get_client', return_value=mock_client):
            assert backend.save_memory("u1", ["event"]) is False

    def test_search_calls_query_memory_sdk(self):
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.call.return_value = json.dumps({"Memories": ["result1"]})

        with patch.object(backend, '_get_client', return_value=mock_client):
            results = backend.search_memory("u1", "query", top_k=3)

        assert results == ["result1"]
        call_args = mock_client.call.call_args
        assert call_args[0][0] == "QueryMemorySdk"
        params = call_args[0][1]
        assert params["MemoryCollectionId"] == "test_ns"
        assert params["AgentUserId"] == "u1"
        assert params["SceneId"] == "_sys_general"
        assert params["Query"] == "query"
        assert params["Limit"] == 3
        assert "Namespace" not in params
        assert "UserId" not in params

    def test_search_exception_returns_empty(self):
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.call.side_effect = Exception("SDK error")

        with patch.object(backend, '_get_client', return_value=mock_client):
            assert backend.search_memory("u1", "query") == []

    def test_get_session_status_calls_list_sessions(self):
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.call.return_value = json.dumps({
            "Code": 200,
            "Message": "success",
            "Data": {
                "Total": 1,
                "Items": [
                    {"SessionId": "sess-1", "State": 0, "DataType": "conversation"},
                ],
            },
        })

        with patch.object(backend, '_get_client', return_value=mock_client):
            status = backend.get_session_status(user_id="u1", session_id="sess-1")

        assert status == {"SessionId": "sess-1", "State": 0, "DataType": "conversation"}
        call_args = mock_client.call.call_args
        assert call_args[0][0] == "ListSessions"
        assert call_args[0][1] == {
            "MemoryCollectionId": "test_ns",
            "AgentUserId": "u1",
            "Page": 1,
            "PageSize": 20,
        }

    def test_namespace_fallback_to_index(self):
        backend = self._make_backend(namespace="", index="fallback_idx")
        mock_client = MagicMock()
        mock_client.call.return_value = json.dumps({"Memories": []})

        with patch.object(backend, '_get_client', return_value=mock_client):
            backend.search_memory("u1", "query")

        params = mock_client.call.call_args[0][1]
        assert params["MemoryCollectionId"] == "fallback_idx"

    def test_optional_search_params(self):
        backend = self._make_backend(scene_id="scene_1")
        mock_client = MagicMock()
        mock_client.call.return_value = json.dumps({"Memories": []})

        with patch.object(backend, '_get_client', return_value=mock_client):
            backend.search_memory(
                "u1", "query",
                occurred_after=1000,
                occurred_before=2000,
                mode="semantic",
            )

        params = mock_client.call.call_args[0][1]
        assert params["SceneId"] == "scene_1"
        assert params["OccurredAfter"] == 1000
        assert params["OccurredBefore"] == 2000
        assert params["Mode"] == "semantic"

    # --- _parse_query_response tests ---

    def test_parse_response_memories_format(self):
        backend = self._make_backend()
        result = backend._parse_query_response({"Memories": ["text1", "text2"]})
        assert result == ["text1", "text2"]

    def test_parse_response_data_dict_format(self):
        backend = self._make_backend()
        result = backend._parse_query_response({
            "Data": [{"Content": "content_1"}, {"Text": "text_1"}]
        })
        assert result == ["content_1", "text_1"]

    def test_parse_response_data_nested_empty_memories_is_empty(self):
        backend = self._make_backend()
        result = backend._parse_query_response({
            "Code": 200,
            "Message": "success",
            "Data": [{"Memories": []}],
        })
        assert result == []

    def test_parse_response_data_nested_aicp_memory_field(self):
        backend = self._make_backend()
        result = backend._parse_query_response({
            "Code": 200,
            "Message": "success",
            "Data": [{
                "Memories": [
                    {
                        "MemoryId": "mem-1",
                        "Memory": "用户张三喜欢喝桃汁。",
                        "Score": 0.99,
                    },
                    {
                        "MemoryId": "mem-2",
                        "Memory": "用户张三不喜欢喝咖啡。",
                        "Score": 0.98,
                    },
                ],
            }],
        })
        assert result == ["用户张三喜欢喝桃汁。", "用户张三不喜欢喝咖啡。"]

    def test_parse_response_results_format(self):
        backend = self._make_backend()
        result = backend._parse_query_response({
            "Results": [{"Text": "r1"}, {"Content": "r2"}]
        })
        assert result == ["r1", "r2"]

    def test_parse_response_content_priority(self):
        """Content 字段优先于 Text 和 Data"""
        backend = self._make_backend()
        result = backend._parse_query_response({
            "Memories": [{"Content": "preferred", "Text": "fallback", "Data": "last"}]
        })
        assert result == ["preferred"]

    def test_parse_response_unknown_format(self, caplog):
        import logging
        backend = self._make_backend()
        with caplog.at_level(logging.WARNING):
            result = backend._parse_query_response({"UnknownKey": "value"})
        assert result == []
        assert "Unknown QueryMemorySdk response format" in caplog.text

    def test_parse_response_invalid_json(self):
        backend = self._make_backend()
        result = backend._parse_query_response("not valid json {{{")
        assert result == []


# ============================================================
# D. TestLongTermMemoryInit
# ============================================================


class TestLongTermMemoryInit:
    """LongTermMemory 构造和 from_env() 工厂方法"""

    def test_init_local_string(self):
        from ksadk.memory.adk.long_term_memory import LongTermMemory
        from ksadk.memory.adk.backends.inmemory_ltm_backend import InMemoryLTMBackend

        ltm = LongTermMemory(backend="local", app_name="test_app")
        assert isinstance(ltm._backend, InMemoryLTMBackend)

    def test_init_backend_instance(self):
        from ksadk.memory.adk.long_term_memory import LongTermMemory
        from ksadk.memory.adk.backends.inmemory_ltm_backend import InMemoryLTMBackend

        custom_backend = InMemoryLTMBackend(index="custom_index")
        ltm = LongTermMemory(backend=custom_backend)
        assert ltm._backend is custom_backend
        assert ltm.index == "custom_index"

    def test_init_with_backend_config(self):
        from ksadk.memory.adk.long_term_memory import LongTermMemory

        ltm = LongTermMemory(
            backend="local",
            backend_config={"index": "config_index"},
            app_name="test",
        )
        assert ltm._backend.index == "config_index"

    def test_init_default_index(self):
        from ksadk.memory.adk.long_term_memory import LongTermMemory

        ltm = LongTermMemory(backend="local")
        assert ltm.index == "default_app"

    def test_init_app_name_as_index(self):
        from ksadk.memory.adk.long_term_memory import LongTermMemory

        ltm = LongTermMemory(backend="local", app_name="my_app")
        assert ltm.index == "my_app"

    def test_init_invalid_backend_raises(self):
        from ksadk.memory.adk.long_term_memory import LongTermMemory
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LongTermMemory(backend="unknown_backend", app_name="test")

    def test_from_env_default(self, monkeypatch):
        from ksadk.memory.adk.long_term_memory import LongTermMemory

        # Clear all LTM env vars
        for key in list(os.environ.keys()):
            if key.startswith("KSADK_LTM_"):
                monkeypatch.delenv(key, raising=False)

        ltm = LongTermMemory.from_env()
        assert ltm.backend == "local"

    def test_from_env_http(self, monkeypatch):
        from ksadk.memory.adk.long_term_memory import LongTermMemory

        monkeypatch.setenv("KSADK_LTM_BACKEND", "http")
        monkeypatch.setenv("KSADK_LTM_HTTP_URL", "http://test.local")
        monkeypatch.setenv("KSADK_LTM_HTTP_TOKEN", "tok123")

        ltm = LongTermMemory.from_env()
        assert ltm.backend == "http"
        assert ltm.backend_config["base_url"] == "http://test.local"
        assert ltm.backend_config["token"] == "tok123"

    def test_from_env_sdk(self, monkeypatch):
        from ksadk.memory.adk.long_term_memory import LongTermMemory

        monkeypatch.setenv("KSADK_LTM_BACKEND", "sdk")
        monkeypatch.setenv("KSADK_LTM_ACCESS_KEY", "ak_test")
        monkeypatch.setenv("KSADK_LTM_SECRET_KEY", "sk_test")
        monkeypatch.setenv("KSADK_LTM_NAMESPACE", "ns_test")

        ltm = LongTermMemory.from_env()
        assert ltm.backend == "sdk"
        assert ltm.backend_config["access_key"] == "ak_test"
        assert ltm.backend_config["secret_key"] == "sk_test"
        assert ltm.backend_config["namespace"] == "ns_test"
        assert ltm.backend_config["scene_id"] == "_sys_general"

    def test_from_env_sdk_ak_fallback(self, monkeypatch):
        from ksadk.memory.adk.long_term_memory import LongTermMemory

        monkeypatch.setenv("KSADK_LTM_BACKEND", "sdk")
        monkeypatch.delenv("KSADK_LTM_ACCESS_KEY", raising=False)
        monkeypatch.delenv("KSADK_LTM_SECRET_KEY", raising=False)
        monkeypatch.setenv("KSYUN_ACCESS_KEY", "fallback_ak")
        monkeypatch.setenv("KSYUN_SECRET_KEY", "fallback_sk")

        ltm = LongTermMemory.from_env()
        assert ltm.backend_config["access_key"] == "fallback_ak"
        assert ltm.backend_config["secret_key"] == "fallback_sk"

    def test_from_env_sdk_prefers_explicit_region_over_ksyun_region(
        self, monkeypatch
    ):
        from ksadk.memory.adk.long_term_memory import LongTermMemory

        monkeypatch.setenv("KSADK_LTM_BACKEND", "sdk")
        monkeypatch.setenv("KSADK_LTM_REGION", "cn-beijing-6")
        monkeypatch.setenv("KSYUN_REGION", "pre-online")

        ltm = LongTermMemory.from_env()
        assert ltm.backend_config["region"] == "cn-beijing-6"

    def test_from_env_sdk_falls_back_to_ksyun_region(self, monkeypatch):
        from ksadk.memory.adk.long_term_memory import LongTermMemory

        monkeypatch.setenv("KSADK_LTM_BACKEND", "sdk")
        monkeypatch.delenv("KSADK_LTM_REGION", raising=False)
        monkeypatch.setenv("KSYUN_REGION", "pre-online")

        ltm = LongTermMemory.from_env()
        assert ltm.backend_config["region"] == "pre-online"

    def test_from_env_sdk_uses_http_for_inner_endpoint_when_scheme_unset(
        self, monkeypatch
    ):
        from ksadk.memory.adk.long_term_memory import LongTermMemory

        monkeypatch.setenv("KSADK_LTM_BACKEND", "sdk")
        monkeypatch.delenv("KSADK_LTM_SCHEME", raising=False)
        monkeypatch.setenv("KSADK_LTM_ENDPOINT", "aicp.inner.api.ksyun.com")

        ltm = LongTermMemory.from_env()
        assert ltm.backend_config["scheme"] == "http"

    def test_from_env_top_k(self, monkeypatch):
        from ksadk.memory.adk.long_term_memory import LongTermMemory

        monkeypatch.setenv("KSADK_LTM_TOP_K", "10")
        ltm = LongTermMemory.from_env()
        assert ltm.top_k == 10


# ============================================================
# E. TestLongTermMemoryEventFiltering
# ============================================================


class TestLongTermMemoryEventFiltering:
    """LongTermMemory._filter_and_convert_events() 详细测试"""

    def _make_ltm(self):
        from ksadk.memory.adk.long_term_memory import LongTermMemory
        return LongTermMemory(backend="local", app_name="filter_test")

    def _make_event(self, author="user", text=None, function_call=None):
        from google.adk.events.event import Event
        from google.genai import types

        parts = []
        if text is not None:
            parts.append(types.Part(text=text))
        if function_call is not None:
            parts.append(types.Part(function_call=function_call))

        content = types.Content(role=author, parts=parts) if parts else None
        return Event(invocation_id="inv1", author=author, content=content)

    def test_only_user_events_pass(self):
        ltm = self._make_ltm()
        events = [
            self._make_event(author="user", text="user message"),
            self._make_event(author="model", text="model reply"),
        ]
        result = ltm._filter_and_convert_events(events)
        assert len(result) == 1
        assert "user message" in result[0]

    def test_function_call_filtered(self):
        from google.genai import types
        ltm = self._make_ltm()
        events = [
            self._make_event(
                author="user",
                function_call=types.FunctionCall(name="search", args={"q": "test"}),
            ),
        ]
        result = ltm._filter_and_convert_events(events)
        assert len(result) == 0

    def test_empty_content_filtered(self):
        from google.adk.events.event import Event
        ltm = self._make_ltm()
        event = Event(invocation_id="inv1", author="user", content=None)
        result = ltm._filter_and_convert_events([event])
        assert len(result) == 0

    def test_empty_parts_filtered(self):
        from google.adk.events.event import Event
        from google.genai import types
        ltm = self._make_ltm()
        event = Event(
            invocation_id="inv1",
            author="user",
            content=types.Content(role="user", parts=[]),
        )
        result = ltm._filter_and_convert_events([event])
        assert len(result) == 0

    def test_event_serialization_json(self):
        ltm = self._make_ltm()
        events = [self._make_event(author="user", text="hello world")]
        result = ltm._filter_and_convert_events(events)
        assert len(result) == 1

        parsed = json.loads(result[0])
        assert "role" in parsed
        assert "parts" in parsed
        assert parsed["parts"][0]["text"] == "hello world"

    async def test_empty_session_no_save(self):
        from google.adk.sessions import InMemorySessionService
        ltm = self._make_ltm()

        svc = InMemorySessionService()
        session = await svc.create_session(app_name="test", user_id="u1")
        # Session has no events
        await ltm.add_session_to_memory(session)
        # No error, nothing saved

    async def test_all_filtered_no_save(self):
        from google.adk.sessions import InMemorySessionService
        ltm = self._make_ltm()

        svc = InMemorySessionService()
        session = await svc.create_session(app_name="test", user_id="u1")
        # Only model events
        session.events = [self._make_event(author="model", text="model only")]
        await ltm.add_session_to_memory(session)
        # Nothing saved to backend

    def test_mixed_events_only_user_text_saved(self):
        from google.genai import types
        ltm = self._make_ltm()
        events = [
            self._make_event(author="user", text="keep this"),
            self._make_event(author="model", text="discard model"),
            self._make_event(
                author="user",
                function_call=types.FunctionCall(name="fn", args={}),
            ),
            self._make_event(author="user", text="keep this too"),
        ]
        result = ltm._filter_and_convert_events(events)
        assert len(result) == 2
        assert "keep this" in result[0]
        assert "keep this too" in result[1]


# ============================================================
# F. TestLongTermMemorySearchMemory
# ============================================================


class TestLongTermMemorySearchMemory:
    """LongTermMemory.search_memory() 返回格式和解析"""

    def _make_ltm(self):
        from ksadk.memory.adk.long_term_memory import LongTermMemory
        return LongTermMemory(backend="local", app_name="search_test")

    async def test_returns_search_memory_response(self):
        from google.adk.memory.base_memory_service import SearchMemoryResponse
        ltm = self._make_ltm()
        result = await ltm.search_memory(
            app_name="search_test", user_id="u1", query="anything"
        )
        assert isinstance(result, SearchMemoryResponse)

    async def test_memory_entry_structure(self):
        ltm = self._make_ltm()
        # Pre-populate backend
        ltm._backend.save_memory("u1", [
            json.dumps({"role": "user", "parts": [{"text": "test memory"}]})
        ])

        result = await ltm.search_memory(
            app_name="search_test", user_id="u1", query="test"
        )
        assert len(result.memories) == 1
        entry = result.memories[0]
        assert hasattr(entry, "author")
        assert hasattr(entry, "content")
        assert entry.content.parts[0].text == "test memory"

    async def test_json_format_parsing(self):
        ltm = self._make_ltm()
        ltm._backend.save_memory("u1", [
            json.dumps({"role": "user", "parts": [{"text": "parsed correctly"}]})
        ])

        result = await ltm.search_memory(
            app_name="test", user_id="u1", query="parsed"
        )
        assert len(result.memories) == 1
        assert result.memories[0].content.parts[0].text == "parsed correctly"
        assert result.memories[0].content.role == "user"

    async def test_plain_text_fallback(self):
        ltm = self._make_ltm()
        ltm._backend.save_memory("u1", ["just plain text, not json"])

        result = await ltm.search_memory(
            app_name="test", user_id="u1", query="plain text"
        )
        assert len(result.memories) == 1
        assert result.memories[0].content.parts[0].text == "just plain text, not json"
        assert result.memories[0].content.role == "user"

    async def test_non_standard_json_skipped(self):
        ltm = self._make_ltm()
        ltm._backend.save_memory("u1", [
            json.dumps({"invalid": "no parts key"})
        ])

        result = await ltm.search_memory(
            app_name="test", user_id="u1", query="invalid"
        )
        # Non-standard format is skipped
        assert len(result.memories) == 0

    async def test_empty_results(self):
        from google.adk.memory.base_memory_service import SearchMemoryResponse
        ltm = self._make_ltm()
        result = await ltm.search_memory(
            app_name="test", user_id="nonexistent", query="anything"
        )
        assert isinstance(result, SearchMemoryResponse)
        assert len(result.memories) == 0

    async def test_backend_error_returns_empty(self):
        ltm = self._make_ltm()
        # Replace the private _backend with a mock after construction
        mock_backend = MagicMock()
        mock_backend.search_memory.side_effect = Exception("boom")
        object.__setattr__(ltm, '_backend', mock_backend)

        result = await ltm.search_memory(
            app_name="test", user_id="u1", query="query"
        )
        assert len(result.memories) == 0

    async def test_top_k_passed_to_backend(self):
        ltm = self._make_ltm()
        ltm.top_k = 3
        mock_backend = MagicMock()
        mock_backend.search_memory.return_value = []
        object.__setattr__(ltm, '_backend', mock_backend)

        await ltm.search_memory(app_name="test", user_id="u1", query="q")
        mock_backend.search_memory.assert_called_once_with(
            query="q", top_k=3, user_id="u1"
        )


# ============================================================
# G. TestShortTermMemory
# ============================================================


class TestShortTermMemory:
    """ShortTermMemory 会话管理测试"""

    def test_init_local(self):
        from ksadk.memory.adk.short_term_memory import ShortTermMemory
        from google.adk.sessions import InMemorySessionService

        stm = ShortTermMemory(backend="local")
        assert isinstance(stm.session_service, InMemorySessionService)

    def test_init_database_no_url_raises(self):
        from ksadk.memory.adk.short_term_memory import ShortTermMemory

        with pytest.raises(ValueError, match="KSADK_SESSION_DSN"):
            ShortTermMemory(backend="database", db_url="")

    def test_init_unknown_backend_raises(self):
        """Pydantic Literal validation rejects unknown backends"""
        from ksadk.memory.adk.short_term_memory import ShortTermMemory
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ShortTermMemory(backend="xyz_unknown")

    def test_session_service_property(self):
        from ksadk.memory.adk.short_term_memory import ShortTermMemory
        from google.adk.sessions import BaseSessionService

        stm = ShortTermMemory(backend="local")
        assert isinstance(stm.session_service, BaseSessionService)

    async def test_create_session_auto_id(self):
        from ksadk.memory.adk.short_term_memory import ShortTermMemory

        stm = ShortTermMemory(backend="local")
        session = await stm.create_session(app_name="app", user_id="u1")
        assert session is not None
        assert session.id  # auto-generated, non-empty

    async def test_create_session_with_id(self):
        from ksadk.memory.adk.short_term_memory import ShortTermMemory

        stm = ShortTermMemory(backend="local")
        session = await stm.create_session(
            app_name="app", user_id="u1", session_id="custom_session_123"
        )
        assert session is not None
        assert session.id == "custom_session_123"

    async def test_create_session_retrieves_existing(self):
        from ksadk.memory.adk.short_term_memory import ShortTermMemory

        stm = ShortTermMemory(backend="local")
        s1 = await stm.create_session(
            app_name="app", user_id="u1", session_id="shared_id"
        )
        s2 = await stm.create_session(
            app_name="app", user_id="u1", session_id="shared_id"
        )
        assert s1.id == s2.id

    def test_from_env_default(self, monkeypatch):
        from ksadk.memory.adk.short_term_memory import ShortTermMemory

        monkeypatch.delenv("KSADK_STM_BACKEND", raising=False)
        monkeypatch.delenv("KSADK_STM_PATH", raising=False)
        monkeypatch.delenv("KSADK_STM_DB_URL", raising=False)
        monkeypatch.delenv("KSADK_STM_DB_PATH", raising=False)
        monkeypatch.delenv("KSADK_STM_URL", raising=False)
        monkeypatch.delenv("KSADK_ADK_SESSION_BACKEND", raising=False)
        monkeypatch.delenv("KSADK_ADK_SESSION_PATH", raising=False)
        monkeypatch.delenv("KSADK_ADK_SESSION_URL", raising=False)

        stm = ShortTermMemory.from_env()
        assert stm.backend == "local"

    def test_from_env_backend(self, monkeypatch):
        from ksadk.memory.adk.short_term_memory import ShortTermMemory

        monkeypatch.setenv("KSADK_STM_BACKEND", "sqlite")
        stm = ShortTermMemory.from_env()
        assert stm.backend == "sqlite"

    def test_from_env_db_path(self, monkeypatch):
        from ksadk.memory.adk.short_term_memory import ShortTermMemory

        monkeypatch.delenv("KSADK_STM_PATH", raising=False)
        monkeypatch.delenv("KSADK_ADK_SESSION_PATH", raising=False)
        monkeypatch.setenv("KSADK_STM_DB_PATH", "/custom/path.db")
        stm = ShortTermMemory.from_env()
        assert stm.local_database_path == "/custom/path.db"


# ============================================================
# H. TestADKRunnerMemoryIntegration
# ============================================================


class TestADKRunnerMemoryIntegration:
    """ADKRunner 记忆初始化和工具注入测试"""

    def _make_runner(self):
        from ksadk.runners.adk_runner import ADKRunner

        mock_detection = MagicMock()
        mock_detection.entry_point = "agent.py"
        mock_detection.agent_variable = "root_agent"
        runner = ADKRunner(mock_detection, "/tmp/test_project")
        return runner

    def test_init_stm_no_env(self, monkeypatch):
        monkeypatch.delenv("KSADK_STM_BACKEND", raising=False)
        monkeypatch.delenv("KSADK_STM_PATH", raising=False)
        monkeypatch.delenv("KSADK_STM_URL", raising=False)
        monkeypatch.delenv("KSADK_STM_DB_PATH", raising=False)
        monkeypatch.delenv("KSADK_STM_DB_URL", raising=False)
        monkeypatch.delenv("KSADK_ADK_SESSION_BACKEND", raising=False)
        monkeypatch.delenv("KSADK_ADK_SESSION_PATH", raising=False)
        monkeypatch.delenv("KSADK_ADK_SESSION_URL", raising=False)
        runner = self._make_runner()
        result = runner._init_short_term_memory()
        assert result is None

    def test_init_stm_local(self, monkeypatch):
        monkeypatch.setenv("KSADK_STM_BACKEND", "local")
        runner = self._make_runner()
        result = runner._init_short_term_memory()
        assert result is not None

    def test_init_ltm_no_env(self, monkeypatch):
        monkeypatch.delenv("KSADK_LTM_BACKEND", raising=False)
        runner = self._make_runner()
        result = runner._init_long_term_memory()
        assert result is None

    def test_init_ltm_local(self, monkeypatch):
        monkeypatch.setenv("KSADK_LTM_BACKEND", "local")
        runner = self._make_runner()
        mock_agent = MagicMock()
        mock_agent.name = "test_agent"  # set as attribute, not MagicMock constructor param
        runner._agent = mock_agent
        result = runner._init_long_term_memory()
        assert result is not None

    def test_init_ltm_sdk_env(self, monkeypatch):
        monkeypatch.setenv("KSADK_LTM_BACKEND", "sdk")
        monkeypatch.setenv("KSADK_LTM_ACCESS_KEY", "ak")
        monkeypatch.setenv("KSADK_LTM_SECRET_KEY", "sk")
        monkeypatch.setenv("KSADK_LTM_NAMESPACE", "ns")

        runner = self._make_runner()
        mock_agent = MagicMock()
        mock_agent.name = "test_agent"
        runner._agent = mock_agent
        result = runner._init_long_term_memory()
        assert result is not None

    def test_init_ltm_sdk_uses_ksyun_region_and_inner_http(self, monkeypatch):
        monkeypatch.setenv("KSADK_LTM_BACKEND", "sdk")
        monkeypatch.setenv("KSADK_LTM_ACCESS_KEY", "ak")
        monkeypatch.setenv("KSADK_LTM_SECRET_KEY", "sk")
        monkeypatch.setenv("KSADK_LTM_NAMESPACE", "ns")
        monkeypatch.delenv("KSADK_LTM_REGION", raising=False)
        monkeypatch.delenv("KSADK_LTM_SCHEME", raising=False)
        monkeypatch.setenv("KSYUN_REGION", "pre-online")
        monkeypatch.setenv("KSADK_LTM_ENDPOINT", "aicp.inner.api.ksyun.com")

        runner = self._make_runner()
        mock_agent = MagicMock()
        mock_agent.name = "test_agent"
        runner._agent = mock_agent
        result = runner._init_long_term_memory()

        assert result is not None
        assert result.backend_config["region"] == "pre-online"
        assert result.backend_config["scheme"] == "http"

    def test_inject_tool_into_empty(self):
        runner = self._make_runner()
        runner._agent = MagicMock()
        runner._agent.tools = []

        runner._inject_load_memory_tool()
        tool_names = [
            getattr(t, "name", None) or getattr(t, "__name__", "")
            for t in runner._agent.tools
        ]
        assert "load_memory" in tool_names

    def test_inject_tool_skips_duplicate(self):
        from google.adk.tools import load_memory

        runner = self._make_runner()
        runner._agent = MagicMock()
        runner._agent.tools = [load_memory]

        runner._inject_load_memory_tool()
        # Should still be just 1
        assert len(runner._agent.tools) == 1

    def test_inject_tool_no_tools_attr(self):
        runner = self._make_runner()
        runner._agent = MagicMock(spec=[])  # no 'tools' attribute
        del runner._agent.tools  # ensure it's truly missing

        # Should not crash
        runner._inject_load_memory_tool()

    def test_inject_save_memory_tool_into_empty(self):
        runner = self._make_runner()
        runner._agent = MagicMock()
        runner._agent.tools = []

        runner._inject_save_memory_tool()
        tool_names = [
            getattr(t, "name", None) or getattr(t, "__name__", "")
            for t in runner._agent.tools
        ]
        assert "save_memory" in tool_names

    async def test_ensure_session_new_external(self):
        from google.adk.sessions import InMemorySessionService

        runner = self._make_runner()
        runner._agent = MagicMock()
        runner._agent.name = "test_agent"
        runner._session_service = InMemorySessionService()

        session_id = await runner._ensure_session("external_123")
        assert session_id is not None
        assert "external_123" in runner._session_map

    async def test_ensure_session_cached(self):
        from google.adk.sessions import InMemorySessionService

        runner = self._make_runner()
        runner._agent = MagicMock()
        runner._agent.name = "test_agent"
        runner._session_service = InMemorySessionService()

        id1 = await runner._ensure_session("ext_1")
        id2 = await runner._ensure_session("ext_1")
        assert id1 == id2

    async def test_ensure_session_default(self):
        from google.adk.sessions import InMemorySessionService

        runner = self._make_runner()
        runner._agent = MagicMock()
        runner._agent.name = "test_agent"
        runner._session_service = InMemorySessionService()

        id1 = await runner._ensure_session()
        id2 = await runner._ensure_session()
        assert id1 == id2
        assert runner._default_session_id == id1

    async def test_save_to_ltm_no_ltm(self):
        runner = self._make_runner()
        runner._long_term_memory = None
        result = await runner.save_session_to_long_term_memory("session_1")
        assert result is False

    async def test_save_to_ltm_session_not_found(self):
        from google.adk.sessions import InMemorySessionService

        runner = self._make_runner()
        runner._agent = MagicMock()
        runner._agent.name = "test_agent"
        runner._session_service = InMemorySessionService()
        runner._long_term_memory = MagicMock()

        result = await runner.save_session_to_long_term_memory("nonexistent_session")
        assert result is False
