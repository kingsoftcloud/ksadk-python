# 构建 ADK 智能体

这个教程展示一个最小 Google ADK 项目如何用 KsADK 运行、调试并暴露
OpenAI 兼容接口。它只依赖本地文件和模型 provider 配置，不需要私有部署环境。

## 1. 创建项目

```bash
mkdir adk-hello-agent
cd adk-hello-agent
python -m venv .venv
source .venv/bin/activate
pip install -U "ksadk[adk]"
```

创建 `agentengine.yaml`：

```yaml
name: adk-hello-agent
framework: adk
entry_point: agent.py
agent_variable: root_agent
```

创建 `.env`：

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

from google.adk.agents import Agent


def lookup_city(city: str) -> dict:
    """Return a small demo profile for a city."""
    profiles = {
        "beijing": "北京适合安排室内外结合的短行程。",
        "shanghai": "上海适合安排通勤友好的半日行程。",
        "guangzhou": "广州适合预留雨具和室内备选方案。",
    }
    return {"city": city, "summary": profiles.get(city.lower(), "暂无演示数据。")}


root_agent = Agent(
    name="adk_hello_agent",
    description="A small ADK agent that can call a local city lookup tool.",
    instruction=(
        "你是一个简洁的行程助手。用户提到城市时，先调用 lookup_city，"
        "再给出两到三条建议。"
    ),
    tools=[lookup_city],
)
```

这里的 `root_agent` 仍然是原生 Google ADK `Agent`。KsADK 不改写 ADK
编程模型，只是在运行时外面加一层 CLI、Web UI、会话和 HTTP 协议适配。

## 3. 本地运行

```bash
agentengine run . -i
```

可以试：

```text
帮我规划一个北京半日行程。
```

如果启动失败，优先检查：

- `agentengine.yaml` 的 `framework` 是否为 `adk`。
- `entry_point` 是否指向 `agent.py`。
- `agent_variable` 是否为 `root_agent`。
- 当前虚拟环境是否安装了 `ksadk[adk]`。

## 4. 打开 Web UI

另开一个终端：

```bash
source .venv/bin/activate
agentengine web . --no-open
```

打开命令输出的本地地址。Web UI 使用独立
[`kingsoftcloud/ksadk-web`](https://github.com/kingsoftcloud/ksadk-web)
项目构建出的静态 UI，Python 仓库只消费构建产物。

## 5. 调用 OpenAI 兼容接口

启动本地 server：

```bash
agentengine run . --port 8080
```

调用 Responses：

```bash
curl http://127.0.0.1:8080/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "adk-hello-agent",
    "input": "给我一个上海半日安排。",
    "stream": false
  }'
```

调用 Chat Completions：

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "adk-hello-agent",
    "messages": [
      {"role": "user", "content": "给我一个广州半日安排。"}
    ],
    "stream": false
  }'
```

## 6. 逐步接入高级能力

ADK 项目先保持最小可运行，再逐步接入高级能力：

| 能力 | 推荐入口 | 说明 |
| --- | --- | --- |
| MCP 工具 | `KSADK_MCP_SERVERS` | runner 加载阶段注入 ADK toolset |
| 长期记忆 | `KSADK_LTM_*` | 先配置 namespace 和 backend，再做行为测试 |
| 知识库 | provider 环境变量 | 在 Agent 指令里明确何时使用知识上下文 |
| Skill Runtime | `ksadk[skills]` | 只加载经过审核的 skill 包 |
| 会话管理 | 本地 Web UI 或 API session 字段 | 确认同一 session 内历史符合预期 |

不要一次性打开所有可选能力。每接入一项，都用 `agentengine run . -i`、
`agentengine web . --no-open` 和一条 API smoke request 验证行为。

## 7. 提交文件

建议提交：

```text
agent.py
agentengine.yaml
README.md
tests/
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

- [框架接入](../guides/frameworks.md)：ADK runner、加载边界和检测顺序。
- [Agent 最佳实践](../guides/agent-best-practices.md)：ADK、记忆、知识库、MCP 和 Skill Runtime 模式。
- [环境变量](../reference/environment-variables.md)：模型、记忆、知识库、MCP 和 tracing 变量。
- [OpenAI 兼容 API](../reference/openai-compatible-api.md)：本地协议形态。
