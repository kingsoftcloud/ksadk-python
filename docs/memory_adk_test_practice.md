# KsADK 记忆库 ADK 模块 - 测试实践文档

## 1. 测试目标

对 KsADK 记忆库 ADK 模块进行系统性单元测试，覆盖全部接口和使用流程：

- 验证三种后端实现（InMemory / HTTP / SDK）的接口契约
- 验证 LongTermMemory 服务层的事件过滤、检索解析、工厂方法
- 验证 ShortTermMemory 会话管理的全部路径
- 验证 ADKRunner 的记忆初始化、工具注入、会话映射
- 所有测试纯本地运行，不依赖 LLM 或远程 API

## 2. 覆盖矩阵

### 2.1 后端层

| 组件 | 测试类 | 用例数 | 覆盖范围 |
|------|--------|--------|----------|
| InMemoryLTMBackend | TestInMemoryLTMBackendExtended | 8 | Unicode、大量数据、top_k、隔离、评分、边界 |
| HttpLTMBackend | TestHttpLTMBackend | 9 | Mock HTTP、错误处理、懒加载、认证、关闭 |
| SdkLTMBackend | TestSdkLTMBackend | 17 | Mock SDK、Conversation 格式、响应解析、参数传递 |

### 2.2 服务层

| 组件 | 测试类 | 用例数 | 覆盖范围 |
|------|--------|--------|----------|
| LongTermMemory 构造 | TestLongTermMemoryInit | 11 | 构造路径、from_env()、环境变量回退 |
| 事件过滤 | TestLongTermMemoryEventFiltering | 8 | 各类事件过滤、序列化格式、空 session |
| 检索解析 | TestLongTermMemorySearchMemory | 8 | 响应类型、JSON/纯文本解析、错误处理 |
| ShortTermMemory | TestShortTermMemory | 10 | 会话创建/获取、from_env()、后端回退 |

### 2.3 集成层

| 组件 | 测试类 | 用例数 | 覆盖范围 |
|------|--------|--------|----------|
| ADKRunner 记忆集成 | TestADKRunnerMemoryIntegration | 14 | STM/LTM 初始化、工具注入、会话映射 |

**合计**: 8 个测试类，85 个测试用例

## 3. 测试用例详表

### A. TestInMemoryLTMBackendExtended

| # | 测试名 | 输入 | 预期结果 | 通过标准 |
|---|--------|------|----------|----------|
| 1 | test_index_property | index="my_custom_index" | backend.index == "my_custom_index" | 属性正确 |
| 2 | test_save_empty_list_returns_true | save_memory([], ) | True, search 返回空 | 不崩溃 |
| 3 | test_unicode_special_characters | 中文+emoji+特殊字符 | 存取正确 | 检索到包含内容 |
| 4 | test_large_volume_memory | 200 条记忆 | top_k=10 返回 10 条 | 数量正确 |
| 5 | test_top_k_limits_results | top_k=3/1/100 | 分别返回 3/1/10 条 | 限制生效 |
| 6 | test_no_match_returns_recent | 无匹配查询 | 返回最近 top_k 条 | 回退正确 |
| 7 | test_multiple_users_isolation | 5 用户各存数据 | 互不可见 | 完全隔离 |
| 8 | test_full_match_scores_higher | 完整/部分匹配 | 完整匹配排前 | 评分正确 |

### B. TestHttpLTMBackend

| # | 测试名 | 输入 | 预期结果 | 通过标准 |
|---|--------|------|----------|----------|
| 1 | test_empty_base_url_save_returns_false | base_url="" | False | 空 URL 不调用 |
| 2 | test_empty_base_url_search_returns_empty | base_url="" | [] | 空 URL 返回空 |
| 3 | test_save_memory_success | Mock 200 | True, payload 正确 | 请求格式正确 |
| 4 | test_save_memory_http_error | Mock 500 | False | 错误处理 |
| 5 | test_search_memory_success | Mock memories 响应 | 解析正确 | 数据提取正确 |
| 6 | test_search_memory_http_error | Mock 404 | [] | 错误处理 |
| 7 | test_client_lazy_init | 两次访问 | 同一实例 | 懒加载 |
| 8 | test_token_in_headers | token="xxx" | Authorization header | 认证正确 |
| 9 | test_close_resets_client | close() | _client=None | 清理正确 |

### C. TestSdkLTMBackend

| # | 测试名 | 输入 | 预期结果 | 通过标准 |
|---|--------|------|----------|----------|
| 1 | test_init_no_credentials_warning | AK/SK="" | 记录 warning | 不崩溃 |
| 2 | test_save_empty_events_returns_true | [] | True | 空列表跳过 |
| 3 | test_save_calls_create_memory_sdk | 有效事件 | 调用 CreateMemorySdk | API 名正确 |
| 4 | test_save_data_conversation_format | 事件 JSON | Data.Conversation 结构 | 格式正确 |
| 5 | test_save_conversation_item_fields | 事件 JSON | Role/CreatedAt/MessageId/Content | 字段完整 |
| 6 | test_save_parses_event_json | ADK event JSON | 提取 role 和 text | 解析正确 |
| 7 | test_save_plain_text_fallback | 纯文本 | role=user, Text=原文 | 回退正确 |
| 8 | test_save_exception_returns_false | SDK 异常 | False | 错误处理 |
| 9 | test_search_calls_query_memory_sdk | 查询参数 | 调用 QueryMemorySdk | 参数正确 |
| 10 | test_search_exception_returns_empty | SDK 异常 | [] | 错误处理 |
| 11 | test_namespace_fallback_to_index | namespace="" | 使用 index | 回退正确 |
| 12 | test_optional_search_params | occurred_after 等 | 参数传递 | 可选参数 |
| 13-17 | test_parse_response_* | 各种响应格式 | 正确解析或空 | 兼容多格式 |

### D-H. 服务层和集成层测试

（详见测试文件中各 Class 的具体测试函数）

## 4. 执行方法

### 前置条件

```bash
cd /Users/albin/Documents/workspace/ezone/ksadk-python
source .venv/bin/activate
pip install -e ".[adk,dev]"
```

### 运行全部测试

```bash
.venv/bin/python -m pytest tests/unit/memory/test_adk_memory_comprehensive.py -v
```

### 按测试类运行

```bash
# 仅 InMemoryLTMBackend
.venv/bin/python -m pytest tests/unit/memory/test_adk_memory_comprehensive.py -v -k "TestInMemoryLTMBackendExtended"

# 仅 ShortTermMemory
.venv/bin/python -m pytest tests/unit/memory/test_adk_memory_comprehensive.py -v -k "TestShortTermMemory"

# 仅 SDK 后端
.venv/bin/python -m pytest tests/unit/memory/test_adk_memory_comprehensive.py -v -k "TestSdkLTMBackend"

# 仅 ADKRunner
.venv/bin/python -m pytest tests/unit/memory/test_adk_memory_comprehensive.py -v -k "TestADKRunnerMemoryIntegration"
```

### 覆盖率报告

```bash
.venv/bin/python -m pytest tests/unit/memory/test_adk_memory_comprehensive.py \
    --cov=ksadk.memory.adk --cov-report=term-missing
```

## 5. 测试结果

| 测试类 | 用例数 | 通过 | 失败 | 状态 |
|--------|--------|------|------|------|
| TestInMemoryLTMBackendExtended | 8 | 8 | 0 | PASS |
| TestHttpLTMBackend | 9 | 9 | 0 | PASS |
| TestSdkLTMBackend | 17 | 17 | 0 | PASS |
| TestLongTermMemoryInit | 11 | 11 | 0 | PASS |
| TestLongTermMemoryEventFiltering | 8 | 8 | 0 | PASS |
| TestLongTermMemorySearchMemory | 8 | 8 | 0 | PASS |
| TestShortTermMemory | 10 | 10 | 0 | PASS |
| TestADKRunnerMemoryIntegration | 14 | 14 | 0 | PASS |
| **合计** | **85** | **85** | **0** | **ALL PASS** |

执行环境: Python 3.13.0, pytest 9.0.2, macOS Darwin 23.4.0

## 6. 已知限制

1. **HttpLTMBackend / SdkLTMBackend**: 使用 Mock 测试，未验证真实网络调用。真实连通性测试见 `examples/memory_demo_adk/test_memory_agent.py` Test 1.5。
2. **ShortTermMemory sqlite/database 后端**: 仅验证回退到 InMemory 的行为，未测试真实 SQLite 持久化（需要 google-adk database extras）。
3. **ADKRunner.load_agent()**: 未在此测试中覆盖（需要完整的 Agent 模块），仅测试独立的记忆初始化方法。
4. **Agent 端到端流程**: 需 LLM 的完整 Agent 对话流测试见 `examples/memory_demo_adk/test_memory_agent.py` Test 3-5。

## 7. 关键文件

| 文件 | 说明 |
|------|------|
| `tests/unit/memory/test_adk_memory_comprehensive.py` | 综合单元测试 (85 tests) |
| `ksadk/memory/adk/backends/base_ltm_backend.py` | 后端抽象基类 |
| `ksadk/memory/adk/backends/inmemory_ltm_backend.py` | InMemory 后端 |
| `ksadk/memory/adk/backends/http_ltm_backend.py` | HTTP 后端 |
| `ksadk/memory/adk/backends/sdk_ltm_backend.py` | SDK 后端 (AICP API) |
| `ksadk/memory/adk/long_term_memory.py` | LongTermMemory 服务 |
| `ksadk/memory/adk/short_term_memory.py` | ShortTermMemory 服务 |
| `ksadk/runners/adk_runner.py` | ADKRunner 集成 |
