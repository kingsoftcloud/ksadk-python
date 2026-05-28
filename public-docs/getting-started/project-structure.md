# 项目结构

一个显式配置的 KsADK 项目通常包含：

```text
my-agent/
├── agentengine.yaml
├── .env
├── requirements.txt
└── my_agent/
    ├── __init__.py
    └── agent.py
```

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `agentengine.yaml` | 框架、入口文件和导出变量 |
| `.env` | 本地模型和可选平台配置，不提交真实值 |
| `requirements.txt` | 业务依赖 |
| `agent.py` | 导出 `root_agent` 或配置中的变量名 |

## 不应提交

- 真实 `.env`。
- `.agentengine/ui/sessions.sqlite`。
- 构建产物、缓存和日志。
- token、kubeconfig、私有 registry 凭证。

公开示例可以提供 `.env.example`，但只能包含占位值。
