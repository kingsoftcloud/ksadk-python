# 构建 LangGraph 智能体

这个教程展示最小 LangGraph 项目。生产型例子见
[Agent 最佳实践](../guides/agent-best-practices.zh.md)。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U "ksadk[langgraph]" langchain-openai
```

## Agent

```python
from typing import Annotated, TypedDict
import operator
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

class State(TypedDict):
    messages: Annotated[list, operator.add]

llm = ChatOpenAI(model="my-model", base_url="https://api.example.com/v1", api_key="sk-test")

def chat(state: State):
    return {"messages": [llm.invoke(state["messages"])]}

graph = StateGraph(State)
graph.add_node("chat", chat)
graph.set_entry_point("chat")
graph.add_edge("chat", END)

root_agent = graph.compile()
```

## 配置

```yaml
name: langgraph-agent
framework: langgraph
entry_point: agent.py
agent_variable: root_agent
```

## 运行

```bash
agentengine run . -i
agentengine web . --no-open
```
