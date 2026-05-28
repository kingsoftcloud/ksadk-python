# 故障排查

## 模型调用失败

检查：

- `OPENAI_API_KEY` 是否存在。
- `OPENAI_BASE_URL` 是否是 OpenAI 兼容 endpoint。
- `OPENAI_MODEL_NAME` 是否被 provider 支持。
- 本地网络是否能访问 provider。

## 框架检测失败

优先确认 `agentengine.yaml`：

```yaml
framework: langgraph
entry_point: my_agent/agent.py
agent_variable: root_agent
```

如果检测成功但加载失败，通常是依赖缺失、导入路径错误或导出变量不匹配。

## Web UI 无法打开

检查：

- `agentengine web . --no-open` 是否启动成功。
- 端口是否被占用。
- `ksadk/server/static/index.html` 是否存在。
- 浏览器控制台是否有资源 404。

## 会话丢失

确认 `KSADK_STM_PATH` 或 `AGENTENGINE_UI_DIR` 是否稳定。每次换目录或重新生成
session id 都会导致 UI 看起来像新会话。

## Skill Runtime 不执行

Skill Space 可发现不等于 sandbox 已启用。隔离执行需要：

- `KSADK_SKILL_RUNTIME_BACKEND`
- `KSADK_SKILL_RUNTIME_TEMPLATE_ID` 或 `KSADK_SANDBOX_TEMPLATE_ID`
- 对应 runtime 依赖和凭证

未配置时应返回诊断，而不是伪造执行成功。
