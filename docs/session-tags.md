# 会话标签

托管 Agent 可在 Server 的 `CreateSession` 接口传入业务标签。平台负责保存和传递，业务代码负责解释标签。LangChain、LangGraph、Google ADK 共用同一套读取方法。

```json
{
  "AgentId": "your-agent-id",
  "UserId": "your-user-id",
  "Tags": {"scene": "customer_service", "channel": "web"}
}
```

随后使用返回的 `SessionId` 调用 `RunAgent`，无需每轮重新传入标签。在 Agent 节点、工具或回调中读取：

```python
from ksadk.runtime_context import get_current_session_tags

def select_service():
    tags = get_current_session_tags()
    if tags.get("scene") == "customer_service":
        return "客服业务"
    return "通用业务"
```

`get_current_session_context()` 还提供 `schema_version` 和 `revision`。返回的快照及 `tags` 均只读；需要修改标签时，由业务服务调用 Server `UpdateSession`：

```json
{
  "SessionId": "your-session-id",
  "SetTags": {"scene": "after_sales"},
  "RemoveTags": ["channel"],
  "ExpectedTagsRevision": 1
}
```

`SetTags` 合并/覆盖，`RemoveTags` 删除；不能同时操作同一个键。`ExpectedTagsRevision` 可选，冲突返回 409。不传 revision 时，服务端也通过 CAS 合并并发补丁。创建非空标签时 revision 为 1，未使用标签的旧会话为 0；实际变化才递增。

`GetSession`、`ListSessions` 返回 `Tags` 与 `TagsRevision`；列表可带 `TagFilters` 和 `TagMatch: "all" | "any"` 做精确筛选。最多 32 个标签，键为 1–64 位字母、数字、下划线、点或连字符，值为最长 256 字符的字符串；`agentengine.` 前缀保留。

## LangGraph 原生 context（可选）

通用 helper 不要求修改现有 Agent。若希望通过 LangGraph `Runtime.context` 读取，可显式声明 `SessionContext`：

```python
from dataclasses import dataclass
from langgraph.runtime import Runtime
from ksadk.session_context import SessionContext

@dataclass
class Context:
    session: SessionContext

def node(state, runtime: Runtime[Context]):
    scene = runtime.context.session.tags.get("scene")
    # 按业务需要处理 scene
    return {}

# StateGraph(..., context_schema=Context)
```

只有 `session: SessionContext`（或可空的该类型）开启原生注入；已存在的 `session: str` 等自定义字段保持原行为。ADK 无需增加 session state 字段。

## 生命周期与兼容边界

- 标签快照在平台受理一次执行时确定；运行中更新只影响后续执行。恢复执行重新读取标签；相同幂等键重试保留首次受理的快照。
- 无标签或无运行上下文时，helper 返回空映射。删除最后一个标签后，下一次执行收到空映射和更新后的 revision。
- 标签独立于请求 Metadata、LangChain tracing tags、模型输入和 Agent state，不自动进入 prompt 或公开 Responses metadata。业务代码主动返回标签时仍会出现在输出中。
- 标签用于业务分类。租户、用户和会话访问权限继续由平台认证与归属校验决定，不能把任意业务标签当作授权身份。
- 本版本适用于经过 Server `RunAgent`/`ResumeRun` 及 Kernel admission 的托管调用。公网直转 `/v1/responses`、`/v1/chat/completions` 暂不加载平台标签，且会剥离调用者伪造的保留快照。本地独立 Runtime 的 `CreateSession` 不保存第二份平台标签。
- Server、Gateway 和 KSADK 需配套升级才能使用新能力。不使用 Tags 的历史请求继续工作；旧 Runtime 不会获得新 helper。现有框架的 checkpoint、恢复和审批支持范围保持不变。

## 复现测试

SDK 环境：`uv sync --extra dev --extra langgraph --extra langchain --extra adk`。

```sh
uv run pytest -q tests/test_session_tags_context.py tests/test_session_tags_framework_resume.py
```

Server 工作树通过 `SESSION_TAGS_SDK_DIR=/absolute/path/to/this/checkout` 运行 `tests/test_session_tags_e2e.py`。测试启动独立 TCP Server 和 Runtime 进程，使用真实框架与确定性工具调用模型，不依赖外部模型服务。
