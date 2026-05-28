# Build A LangGraph Agent

This tutorial builds a small LangGraph agent from files you can paste into a new
directory. It is intentionally local-first: no Kingsoft Cloud account, private
gateway, or hosted deployment is required.

## 1. Create The Project

```bash
mkdir weather-agent
cd weather-agent
python -m venv .venv
source .venv/bin/activate
pip install -U "ksadk[langgraph]" langchain-openai
```

Create `agentengine.yaml`:

```yaml
name: weather-agent
framework: langgraph
entry_point: agent.py
agent_variable: root_agent
```

Create `.env` with your provider values:

```bash
OPENAI_API_KEY=sk-test
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL_NAME=my-model
```

Use real values only in your local `.env`.

## 2. Write The Agent

Create `agent.py`:

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
        "beijing": "Beijing is clear, 26 C, light wind.",
        "shanghai": "Shanghai is cloudy, 24 C, humid.",
        "guangzhou": "Guangzhou has light rain, 27 C.",
    }
    return demo_data.get(city.strip().lower(), f"No demo weather for {city}.")


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

This example keeps the lookup in process so that the tutorial remains portable.
In a real project, put external API calls behind explicit functions and test
them separately.

## 3. Run The Agent

```bash
agentengine run . -i
```

Try:

```text
Plan my afternoon in Beijing based on the demo weather.
```

If the run fails before calling the model, check that `agentengine.yaml` points
to `agent.py` and that `root_agent` is exported.

## 4. Open The Web UI

In another terminal:

```bash
source .venv/bin/activate
agentengine web . --no-open
```

Open the printed local URL. Send the same prompt and compare terminal and
browser behavior.

## 5. Call The Local API

Run the server:

```bash
agentengine run . --port 8080
```

Call Responses:

```bash
curl http://127.0.0.1:8080/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "weather-agent",
    "input": "Give me a short Beijing weather plan.",
    "stream": false
  }'
```

Call Chat Completions:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "weather-agent",
    "messages": [
      {"role": "user", "content": "Give me a short Shanghai weather plan."}
    ],
    "stream": false
  }'
```

## 6. Add A Smoke Test

Create `tests/test_project_shape.py`:

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

Install test dependencies and run:

```bash
pip install pytest pyyaml
pytest -q
```

## 7. Files To Commit

Commit source and tests:

```text
agent.py
agentengine.yaml
tests/test_project_shape.py
```

Do not commit:

```text
.env
.venv/
.agentengine/
__pycache__/
dist/
build/
```

## Next Steps

- Add framework-specific patterns from [Frameworks](../guides/frameworks.md).
- Add session or file handling from [Runtime Sessions And Files](../reference/runtime-sessions-files.md).
- Package the project with [Build And Package](../guides/build-and-package.md).
