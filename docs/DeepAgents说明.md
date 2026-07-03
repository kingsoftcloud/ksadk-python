# DeepAgents说明

本文档说明 `ksadk` 当前对 `deepagents` 框架的接入方式。

## 1. 设计原则

- 最小适配：尽量复用现有 LangGraph 运行时路径
- 统一入口：CLI、框架识别、构建依赖和部署参数都走 `deepagents`

## 2. 当前实现

### 2.1 框架识别

当前支持：

- 显式配置 `framework: deepagents`
- 代码特征识别：
  - `from deepagents import ...`
  - `import deepagents`
  - `create_deep_agent(...)`

### 2.2 运行时

当前 `DeepAgentsRunner` 沿用 LangGraph 路径，原因是 `create_deep_agent()` 返回 LangGraph 图对象，天然兼容现有 invoke / stream 语义。

### 2.3 平台能力

`deepagents` 当前与 `langgraph` 一样可以复用：

- KB 检索能力
- LTM 环境变量注入
- 默认 storage 挂载基座 `/home/node/.agentengine`
- Hosted WorkspaceFiles capability

## 3. 初始化示例

```bash
agentengine init my-agent -f deepagents
```

生成项目示意：

```python
from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(...)
root_agent = create_deep_agent(model=llm)
```

## 4. 相关文档

- [ksadk使用文档](./ksadk使用文档.md)
- [ksadk技术设计](./ksadk技术设计.md)
