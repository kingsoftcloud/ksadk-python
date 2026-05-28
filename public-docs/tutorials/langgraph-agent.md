# 构建 LangGraph 智能体

这个教程从零创建一个可本地运行的 LangGraph 智能体。示例代码可以直接复制到新目录中运行，不需要金山云账号、私有网关或 hosted 部署环境。

## 1. 创建项目

```bash
mkdir weather-agent
cd weather-agent
python -m venv .venv
source .venv/bin/activate
pip install -U "ksadk[langgraph]" langchain-openai
```

创建 `agentengine.yaml`：

```yaml
name: weather-agent
framework: langgraph
entry_point: agent.py
agent_variable: root_agent
```

创建 `.env`，写入本地模型 provider 配置：

```bash
OPENAI_API_KEY=sk-test
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL_NAME=my-model
```

真实 token 只放在本地 `.env` 或运行环境变量中，不提交到仓库。

## 2. 编写 Agent

创建 `agent.py`：

```python
from __future__ import annotations

import os
from typing import Annotated, TypedDict
import operator

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph


class AgentState(TypedDict):
    messages: Annotated[list, operator.add]


def lookup_weather(city: str) -> str:
    demo_data = {
        "beijing": "北京晴，26 摄氏度，微风。",
        "shanghai": "上海多云，24 摄氏度，湿度较高。",
        "guangzhou": "广州小雨，27 摄氏度。",
    }
    return demo_data.get(city.strip().lower(), f"没有 {city} 的演示天气。")


llm = ChatOpenAI(
    model=os.environ.get("OPENAI_MODEL_NAME", "my-model"),
    base_url=os.environ.get("OPENAI_BASE_URL"),
    api_key=os.environ.get("OPENAI_API_KEY"),
)


def chat(state: AgentState):
    messages = [
        SystemMessage(
            content=(
                "You are a concise weather planning assistant. "
                "If the user asks about demo weather, use the local lookup data."
            )
        ),
        *state["messages"],
    ]
    latest = state["messages"][-1].content if state["messages"] else ""
    if "beijing" in latest.lower():
        messages.append(HumanMessage(content=lookup_weather("beijing")))
    elif "shanghai" in latest.lower():
        messages.append(HumanMessage(content=lookup_weather("shanghai")))
    elif "guangzhou" in latest.lower():
        messages.append(HumanMessage(content=lookup_weather("guangzhou")))
    return {"messages": [llm.invoke(messages)]}


graph = StateGraph(AgentState)
graph.add_node("chat", chat)
graph.set_entry_point("chat")
graph.add_edge("chat", END)

root_agent = graph.compile()
```

示例把查询逻辑留在进程内，方便本地复现。真实项目里建议把外部 API 调用封装成显式函数，并为这些函数单独写测试。

## 3. 运行 Agent

```bash
agentengine run . -i
```

可以试：

```text
根据演示天气帮我规划北京下午行程。
```

如果还没调用模型就失败，优先检查 `agentengine.yaml` 是否指向 `agent.py`，以及 `agent.py` 是否导出了 `root_agent`。

## 4. 打开 Web UI

另开一个终端：

```bash
source .venv/bin/activate
agentengine web . --no-open
```

打开命令输出的本地地址，发送同样的问题，对比终端和浏览器行为。

## 5. 调用本地 API

启动本地 server：

```bash
agentengine run . --port 8080
```

调用 Responses：

```bash
curl http://127.0.0.1:8080/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "weather-agent",
    "input": "给我一个北京天气行程建议。",
    "stream": false
  }'
```

调用 Chat Completions：

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "weather-agent",
    "messages": [
      {"role": "user", "content": "给我一个上海天气行程建议。"}
    ],
    "stream": false
  }'
```

## 6. 添加 Smoke Test

创建 `tests/test_project_shape.py`：

```python
from pathlib import Path
import yaml


def test_project_config_points_to_agent():
    config = yaml.safe_load(Path("agentengine.yaml").read_text())
    assert config["framework"] == "langgraph"
    assert config["entry_point"] == "agent.py"
    assert config["agent_variable"] == "root_agent"
    assert Path(config["entry_point"]).is_file()
```

安装测试依赖并运行：

```bash
pip install pytest pyyaml
pytest -q
```

## 7. 建议提交的文件

提交源码和测试：

```text
agent.py
agentengine.yaml
tests/test_project_shape.py
```

不要提交：

```text
.env
.venv/
.agentengine/
__pycache__/
dist/
build/
```

## 后续阅读

- 阅读 [框架接入](../guides/frameworks.md)，了解框架适配和 runner 加载边界。
- 阅读 [会话与文件](../reference/runtime-sessions-files.md)，了解 session、上传和工作区文件。
- 阅读 [构建与打包](../guides/build-and-package.md)，了解本地构建和公开 artifact 规则。
