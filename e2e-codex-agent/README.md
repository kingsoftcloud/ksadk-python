# e2e_codex_agent — codex runtime agent

## 快速开始

```bash
# 1. 编辑 .env 填星流 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL_NAME
# 2. 本地运行(浏览器对话,codex 自动探测并启用代理)
agentengine web .
# 或
ksadk web .
```

## 说明

- codex 的 agent 逻辑由 `agentengine.yaml` 的 `prompt`(开发者指令)承载,无 agent.py。
- codex 自动探测模型协议:OpenAI 官方直连,星流 chat 模型自动启用转换代理。
- 部署:`agentengine build .` → `agentengine deploy .`(服务端 framework=codex)。
