# AgentEngine 常见问题 FAQ

## 安装与配置

### Q: pip install ksadk 失败怎么办？

A: 请确认 Python 版本 >= 3.10。推荐使用以下命令安装：
```bash
pip install --upgrade pip
pip install ksadk[all]
```
如果遇到网络问题，可以使用国内镜像源：
```bash
pip install ksadk -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### Q: 支持哪些 LLM 模型？

A: AgentEngine 通过 LiteLLM 支持所有 OpenAI 兼容的模型接口，包括：
- 金山云 KSPMAS（DeepSeek-V3.2 等）
- OpenAI GPT 系列
- Anthropic Claude 系列
- 任何 OpenAI 兼容的 API 端点

配置方式：在 `.env` 中设置 `OPENAI_API_BASE` 和 `OPENAI_API_KEY`。

### Q: 如何切换不同的模型？

A: 修改 `.env` 中的 `MODEL_NAME` 变量，或在命令行中指定：
```bash
agentengine run . --model gpt-4
```

## 框架相关

### Q: ADK、LangChain、LangGraph 三者如何选择？

A:
- **ADK**：推荐首选，Google 官方框架，内置记忆、工具注入等能力
- **LangGraph**：适合需要复杂工作流、多 Agent 协作的场景
- **LangChain**：适合简单的链式调用场景

### Q: AgentEngine 如何检测项目使用的框架？

A: AgentEngine 通过分析 `agent.py` 中的 import 语句自动检测。也可以在 `agentengine.yaml` 中显式指定：
```yaml
type: adk  # 或 langgraph, langchain
```

## 运行与调试

### Q: agentengine run 和 agentengine web 有什么区别？

A:
- `agentengine run .`：启动 REST API 服务，适合程序调用
- `agentengine run . --interactive`：启动 TUI 交互界面，适合开发调试
- `agentengine web .`：启动 Web UI，适合可视化体验

### Q: 如何查看 Agent 的思考过程？

A: 运行时添加 `--show-thinking` 参数：
```bash
agentengine run . --interactive --show-thinking
```

### Q: Agent 响应太慢怎么办？

A:
1. 检查模型 API 的响应速度
2. 减少工具数量，避免 Agent 在工具选择上犹豫
3. 优化工具的 docstring，让 LLM 更容易做决策
4. 使用流式输出模式，提升用户感知速度

## 知识库相关

### Q: 知识库支持哪些文档格式？

A: 金山云 AICP 知识库支持以下格式的文档导入：
- PDF
- Word（.docx）
- Markdown（.md）
- 纯文本（.txt）
- HTML

### Q: 知识库检索结果不准确怎么优化？

A:
1. 优化文档切片策略，确保每个片段语义完整
2. 启用重排序：`KSADK_KB_RERANKING_ENABLE=true`
3. 调整 TopK 值，增加候选数量
4. 优化 Agent 的 instruction，引导其生成更好的检索 query
5. 设置 ScoreThreshold 过滤低质量结果

### Q: 如何调试知识库检索过程？

A: 可以直接使用 Python 测试检索：
```python
from ksadk.knowledge_base import search_knowledge
result = search_knowledge("你的查询")
print(result)
```

## 部署相关

### Q: Serverless 部署的冷启动时间是多少？

A:
- 代码模式（Code Mode）：约 5-10 秒
- 容器模式（Container Mode）：约 20-60 秒

推荐使用代码模式以获得更快的冷启动。

### Q: 部署后如何更新 Agent？

A: 修改代码后重新执行部署命令即可。AgentEngine 自动支持版本管理：
```bash
agentengine launch . --target serverless
# 自动创建新版本，旧版本可随时回滚
```

### Q: 最大支持多少并发请求？

A: Serverless 平台会自动弹性扩缩容。默认最大并发数为 100，可通过金山云控制台调整。每个实例处理一个请求，新请求会自动触发新实例创建。
