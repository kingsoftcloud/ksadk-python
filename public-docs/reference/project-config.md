# 项目配置

KsADK 项目建议显式提供 `agentengine.yaml`。显式配置比启发式检测更容易审查，也更适合
公开示例。

## 最小配置

```yaml
name: my-agent
version: "0.1.0"
framework: langgraph
entry_point: my_agent/agent.py
agent_variable: root_agent
```

| 字段 | 含义 |
| --- | --- |
| `name` | 项目名 |
| `version` | 项目版本 |
| `framework` | `adk`、`langgraph`、`langchain` 或 `deepagents` |
| `entry_point` | 导入 Agent 的 Python 文件 |
| `agent_variable` | 文件中导出的 Agent 对象变量名 |

## 框架示例

ADK：

```yaml
framework: adk
entry_point: agent.py
agent_variable: root_agent
```

LangGraph：

```yaml
framework: langgraph
entry_point: my_agent/agent.py
agent_variable: root_agent
```

## 环境变量

模型配置通常放在 `.env`：

```bash
OPENAI_API_KEY=sk-test
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL_NAME=my-model
```

不要把真实 key、私有 endpoint、kubeconfig 或客户数据提交到 Git。

## 检测规则

运行时优先读取显式 YAML；没有配置时才尝试 `langgraph.json`、`agent.py`、
`main.py`、`app.py` 等约定路径。公开项目建议始终使用 YAML。
