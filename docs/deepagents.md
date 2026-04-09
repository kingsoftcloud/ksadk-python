# DeepAgents 框架支持说明

> 主入口已迁移：完整用户路径请先看 [ksadk_usage_guide.md](./ksadk_usage_guide.md)，当前实现边界请看 [ksadk_technical_design.md](./ksadk_technical_design.md)。
>
> 本文保留为 DeepAgents 专项参考，重点说明框架识别、runner 复用和平台能力接入细节。

## 目标

`ksadk` 新增 `deepagents` 框架支持，遵循两个原则：

- 最小适配：尽量复用已有 LangGraph 运行时，不重写 DeepAgents 逻辑。
- 优雅扩展：在检测、Runner 分发、构建依赖、CLI 模板四层统一接入，后续新增框架可按同样路径扩展。

## 设计要点

1. 框架识别
- 新增 `FrameworkType.DEEPAGENTS`
- 支持配置显式声明：`framework: deepagents`
- 支持代码特征识别：
  - `from deepagents import ...`
  - `import deepagents`
  - `create_deep_agent(...)`
- 兼容脚本型目录：目录下存在 `agent.py/main.py/app.py` 且无需 `__init__.py` 也可检测

2. 运行时
- 新增 `DeepAgentsRunner`，直接继承 `LangGraphRunner`
- 原因：官方 `create_deep_agent` 返回 LangGraph `CompiledStateGraph`，天然兼容 LangGraph invoke/stream 语义
- 结果：保留 DeepAgents 原生能力，`ksadk` 仅做统一入口与协议封装
- 平台级 `KSADK_KB_*` / `KSADK_LTM_*` 也沿用 LangGraph 路径：
  - env-only 时，调用前自动注入 KB / LTM ambient context
  - 手动导入 `search_knowledge_base` / `load_memory` / `save_memory` 也可直接使用

3. 构建与部署
- `code/container/deploy manager` 依赖生成增加 `deepagents>=0.3.0`
- Tracing callback-only 判定将 `deepagents` 归入 LangChain 生态（与 `langchain/langgraph` 一致）
- 部署请求中的 `framework` 直传 `deepagents`（不做客户端映射）

4. 服务端参数校验
- 服务端框架白名单已扩展为：`langgraph/langchain/deepagents/adk/openclaw`
- 白名单已统一抽取为服务端常量，避免多处硬编码不一致：
  - `app/core/frameworks.py`
  - `app/api/v1/models/agent_models.py`
  - `app/api/v1/actions/agent_actions.py`

5. CLI
- `agentengine init -f deepagents`
- `agentengine config` 框架可选项新增 `deepagents`
- `run/web` 展示与识别新增 `DeepAgents`
- `create --from-agent` 自动检测 `deepagents` 代码并生成正确依赖

## 模板示例

`agentengine init my_agent -f deepagents` 生成的核心入口：

```python
from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(...)
root_agent = create_deep_agent(model=llm)
```

## 官方文档对齐

- DeepAgents 概览：<https://docs.langchain.com/oss/python/deepagents/overview>
- Streaming：<https://docs.langchain.com/oss/python/deepagents/streaming>

关键对齐点：

- `create_deep_agent()` 返回可直接 `invoke/stream` 的 LangGraph 图
- 输入以 `messages` 为核心，`ksadk` 继续沿用统一输入协议并在运行时转换
- 若 agent / tool 定义了 `context_schema`，`ksadk` 会优先尝试走原生 `context=` 注入；否则回退到 system-context / preamble 语义
