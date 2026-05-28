# 记忆与知识库

记忆和知识库都是可选能力。公开 quickstart 应在不配置 hosted 服务时也能运行；
生产项目再按需接入长期记忆、知识库检索和框架工具。

## 能力边界

| 能力 | 用途 | 推荐范围 |
| --- | --- | --- |
| Session history | 当前会话上下文 | 每个 session |
| 长期记忆 | 用户偏好、轻量事实、历史决策 | 用户和应用 |
| 知识库 | 稳定文档、产品资料、政策说明 | 数据集或语料 |
| 框架工具 | 让 Agent 主动调用记忆或检索 | Agent 实现 |
| Ambient context | 运行时在调用 runner 前补充上下文 | KsADK 扩展 |

长期记忆不适合保存完整客户数据、token、图片二进制或长附件。

## LangGraph 接入

```python
def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    return {
        "messages": payload.get("input_messages", []),
        "question": payload.get("input", ""),
        "knowledge": session_context.get("kb_context"),
        "memory": session_context.get("memory_context"),
    }
```

如果图只使用 `messages`，通常不需要 hook；当 graph state 有自定义字段时，再显式映射。

## ADK 接入

ADK 项目优先使用 ADK 原生 tool 和 memory service。最小示例先只依赖模型调用；
需要平台能力时再加入：

```python
from ksadk.knowledge_base.tool import search_knowledge
from ksadk.memory.tool import load_memory, save_memory
```

## 知识库工具

```python
from langchain_core.tools import tool

@tool
def search_knowledge_base(query: str) -> str:
    from ksadk.knowledge_base.tool import search_knowledge
    return search_knowledge(query)
```

知识库检索失败时要返回诊断，不要伪造成空结果。

## 记忆工具

```python
from langchain_core.tools import tool

@tool
def save_user_memory(content: str) -> str:
    from ksadk.memory.tool import save_memory
    return save_memory(content)

@tool
def load_user_memory(query: str) -> str:
    from ksadk.memory.tool import load_memory
    return load_memory(query)
```

推荐保存短事实，例如“发布说明先写结论，再写风险和验证方式”。

## 环境变量

常见变量：

- `KSADK_LTM_BACKEND`
- `KSADK_LTM_NAMESPACE`
- `KSADK_LTM_TOP_K`
- `KSADK_KB_DATASET_ID`
- `KSADK_KB_TOP_K`

完整列表见 [环境变量](../reference/environment-variables.zh.md)。
