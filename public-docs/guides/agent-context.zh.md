# 智能体上下文

KsADK 在每轮调用前构造运行时上下文，帮助 runner 和业务 Agent 取得稳定的用户、
会话、附件、模型和平台信息。

## 核心字段

| 字段 | 含义 |
| --- | --- |
| `agent_id` | Agent 标识 |
| `user_id` | 用户标识 |
| `session_id` | 当前会话 |
| `model` | 当前请求模型 |
| `attachments` | 当前 turn 文件或图片 |
| `memory_context` | 可选长期记忆上下文 |
| `kb_context` | 可选知识库上下文 |

## 框架接入

LangGraph 项目可用 `ksadk_prepare_state` 映射到自定义 state；LangChain 项目可用
`ksadk_prepare_input` 组织输入。ADK 项目优先使用 ADK 原生工具和服务。

不要从私有 server globals 读取上下文；公开代码应使用 runner payload 和 documented hook。
