# Build An ADK Agent

This tutorial shows how to run a minimal Google ADK project through KsADK,
debug it locally, and expose OpenAI-compatible APIs. It only needs local files
and model provider configuration.

## 1. Create The Project

```bash
mkdir adk-hello-agent
cd adk-hello-agent
python -m venv .venv
source .venv/bin/activate
pip install -U "ksadk[adk]"
```

Create `agentengine.yaml`:

```yaml
name: adk-hello-agent
framework: adk
entry_point: agent.py
agent_variable: root_agent
```

Create `.env`:

```bash
OPENAI_API_KEY=sk-test
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL_NAME=my-model
```

Keep real tokens in local `.env` files or runtime environment variables, not in
the repository.

## 2. Write The Agent

Create `agent.py`:

```python
from __future__ import annotations

from google.adk.agents import Agent


def lookup_city(city: str) -> dict:
    """Return a small demo profile for a city."""
    profiles = {
        "beijing": "Beijing works well for a mixed indoor/outdoor half day.",
        "shanghai": "Shanghai works well for a commute-friendly half day.",
        "guangzhou": "Guangzhou plans should keep rain gear and indoor backups.",
    }
    return {"city": city, "summary": profiles.get(city.lower(), "No demo data.")}


root_agent = Agent(
    name="adk_hello_agent",
    description="A small ADK agent that can call a local city lookup tool.",
    instruction=(
        "You are a concise trip-planning assistant. When the user mentions "
        "a city, call lookup_city first, then return two or three suggestions."
    ),
    tools=[lookup_city],
)
```

`root_agent` is still a native Google ADK `Agent`. KsADK does not rewrite the
ADK programming model; it adds CLI, Web UI, session, and HTTP protocol adapters
around it.

## 3. Run Locally

```bash
agentengine run . -i
```

Try:

```text
Plan a half day in Beijing.
```

If startup fails, check:

- `agentengine.yaml` has `framework: adk`.
- `entry_point` points to `agent.py`.
- `agent_variable` is `root_agent`.
- The active virtual environment has `ksadk[adk]` installed.

## 4. Open The Web UI

In another terminal:

```bash
source .venv/bin/activate
agentengine web . --no-open
```

Open the printed local URL. The Web UI is built from the independent
[`kingsoftcloud/ksadk-web`](https://github.com/kingsoftcloud/ksadk-web)
project; the Python repository consumes the built static output.

## 5. Call The OpenAI-Compatible APIs

Start the local server:

```bash
agentengine run . --port 8080
```

Call Responses:

```bash
curl http://127.0.0.1:8080/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "adk-hello-agent",
    "input": "Give me a short Shanghai half-day plan.",
    "stream": false
  }'
```

Call Chat Completions:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "adk-hello-agent",
    "messages": [
      {"role": "user", "content": "Give me a short Guangzhou half-day plan."}
    ],
    "stream": false
  }'
```

## 6. Add Advanced Capabilities Gradually

Keep the ADK project minimal first, then add advanced capabilities one at a
time:

| Capability | Recommended entry | Note |
| --- | --- | --- |
| MCP tools | `KSADK_MCP_SERVERS` | injected as ADK toolsets during runner load |
| long-term memory | `KSADK_LTM_*` | configure namespace and backend before behavior tests |
| knowledge base | provider environment variables | describe when to use knowledge context in instructions |
| Skill Runtime | `ksadk[skills]` | load only reviewed skill packages |
| session management | Web UI or API session fields | confirm history within the same session |

Do not enable every optional capability at once. After each addition, verify it
with `agentengine run . -i`, `agentengine web . --no-open`, and one API smoke
request.

## 7. Files To Commit

Commit:

```text
agent.py
agentengine.yaml
README.md
tests/
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

- [Frameworks](../guides/frameworks.en.md): ADK runner behavior and detection order.
- [Agent Best Practices](../guides/agent-best-practices.en.md): ADK, memory, knowledge, MCP, and Skill Runtime patterns.
- [Environment Variables](../reference/environment-variables.en.md): model, memory, knowledge, MCP, and tracing variables.
- [OpenAI-Compatible API](../reference/openai-compatible-api.en.md): local protocol shape.
