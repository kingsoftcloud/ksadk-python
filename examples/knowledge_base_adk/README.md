# 知识库问答助手 (ADK) — 完整运行指南

基于 Google ADK 框架的知识库问答 Agent 示例，演示如何接入金山云知识库实现 RAG 检索问答。

## 目录结构

```
knowledge_base_adk/
├── .env.example                   # 环境变量模板
├── .env                           # 实际配置（需自行创建）
├── agentengine.yaml               # AgentEngine 项目配置
├── knowledge_base_adk/
│   ├── __init__.py
│   └── agent.py                   # Agent 定义（显式导入知识库工具）
├── demo.py                        # 完整 demo（验证连通性 + Agent 问答）
├── test_kb_agent.py               # 自动化测试套件（4 个测试阶段）
└── dataset/                       # 知识库示例文档（7 篇 md）
```

---

## Step 1: 进入项目虚拟环境并安装依赖

```bash
cd ksadk-python

# 激活虚拟环境（已有 .venv 时）
source .venv/bin/activate

# 如果还没有虚拟环境，先创建一个
# python3 -m venv .venv && source .venv/bin/activate

# 开发模式安装（推荐，代码改动实时生效）
pip install -e ".[adk,kb]"

# 或正常安装
# pip install ksadk[adk,kb]
```

验证安装：

```bash
python -c "from ksadk.knowledge_base.client import KnowledgeBaseClient; print('OK')"
python -c "from google.adk.agents import Agent; print('OK')"
```

> **注意**: 后续所有命令都需要在虚拟环境中执行。如果新开了终端窗口，需要重新 `source .venv/bin/activate`。

---

## Step 2: 配置环境变量

```bash
cd examples/knowledge_base_adk

# 复制模板
cp .env.example .env
```

编辑 `.env`，需要填入 **3 个必填值**：

### 2.1 获取知识库 ID

1. 登录 [金山云 AICP 控制台](https://ai.ksyun.com)
2. 进入 **知识库管理** → 创建知识库 → 上传文档
3. 复制知识库 ID（格式类似 `1795516d-1c58-48fc-8e96-08d6bdaa912e`）

### 2.2 获取 AK/SK

1. 登录 [金山云 IAM 控制台](https://iam.console.ksyun.com/)
2. 右上角头像 → **AccessKey 管理** → 创建 AccessKey
3. 保存 AK 和 SK（SK 仅显示一次，请妥善保存）

### 2.3 获取模型 API Key

联系团队管理员获取内部模型服务的 API Key。

### 2.4 填入 .env

```ini
# 模型
OPENAI_API_KEY=<你的 API Key>
OPENAI_API_BASE=http://kspmas.ksyun.com/v1
MODEL_NAME=deepseek-v3.2

# 知识库（3 个必填值）
KSADK_KB_DATASET_ID=<你的知识库 ID>
KSYUN_ACCESS_KEY=<你的 AK>
KSYUN_SECRET_KEY=<你的 SK>

# 网络（内网用这两行）
KSADK_KB_ENDPOINT=aicp.inner.api.ksyun.com
KSADK_KB_SCHEME=http

# 网络（本地/外网用这两行，注释掉上面两行）
# KSADK_KB_ENDPOINT=aicp.api.ksyun.com
# KSADK_KB_SCHEME=https
```

> **提示**: `.env.example` 里有更详细的注释说明，可以参考。

---

## Step 3: 运行方式（四选一）

### 方式 A: agentengine run — API 服务模式（推荐）

启动 HTTP 服务，支持 SSE 和 OpenAI 兼容接口：

```bash
cd examples/knowledge_base_adk
agentengine run .
```

输出：

```
🚀 Server running at http://0.0.0.0:8080
   - API Docs: http://0.0.0.0:8080/docs
   - Chat API: http://0.0.0.0:8080/chat
```

**测试接口：**

```bash
# 1. 创建 Session
curl -s -X POST "http://localhost:8080/apps/knowledge_base_assistant/users/test/sessions" \
  -H "Content-Type: application/json" -d '{}'

# 2. 通过 SSE 接口提问（把 SESSION_ID 替换为上一步返回的 id）
curl -N -X POST "http://localhost:8080/run_sse" \
  -H "Content-Type: application/json" \
  -d '{
    "appName": "knowledge_base_assistant",
    "userId": "test",
    "sessionId": "SESSION_ID",
    "newMessage": {
      "role": "user",
      "parts": [{"text": "知识库如何配置？"}]
    }
  }'

# 3. 或通过 OpenAI 兼容接口（更简单，无需 Session）
curl -s -X POST "http://localhost:8080/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "knowledge_base_assistant",
    "messages": [{"role": "user", "content": "如何部署 Agent？"}]
  }'
```

### 方式 B: agentengine web — Web UI 模式

```bash
cd examples/knowledge_base_adk
agentengine web .
```

打开浏览器访问提示的地址，直接对话。

### 方式 C: python demo.py — 脚本 Demo

不需要启动服务，直接运行完整流程（验证连通 → 创建 Agent → 问答）：

```bash
cd examples/knowledge_base_adk
python demo.py
```

流程：

```
[Step 1] 创建知识库客户端，验证连通性
         → 发送测试查询，确认 API 可达
[Step 2] 创建 Agent (显式添加 search_knowledge_base 工具)
[Step 3] 创建 Runner
[Step 4] 发送问题，Agent 自动检索知识库并回答
[Step 5] 追问一个问题（验证多轮对话）
```

### 方式 D: python test_kb_agent.py — 自动化测试

分 4 个阶段逐层验证，从底层 API 到上层 Agent：

```bash
cd examples/knowledge_base_adk

# 运行全部测试（需要 LLM API Key）
python test_kb_agent.py

# 仅运行检索测试（不需要 LLM，用于排查知识库连通问题）
python test_kb_agent.py --retrieval-only
```

测试阶段：

```
Test 1 — 客户端连通性:   KnowledgeBaseClient 直连 AICP API
Test 2 — 工具层封装:     search_knowledge() 函数 + 结果格式化
Test 3 — Agent 显式集成:  Agent 手动添加工具 → 检索 → LLM 回答
Test 4 — 自动注入:        模拟 ADKRunner 零配置注入 → 检索 → LLM 回答
```

---

## 工作原理

```
用户提问
  │
  ▼
Agent (deepseek-v3.2)
  │ 判断需要查找知识
  ▼
search_knowledge_base(query="...")    ← Agent 自动调用工具
  │
  ▼
KnowledgeBaseClient.search()
  │ HTTP POST → AICP RetrieveKnowledge API
  ▼
金山云 AICP 知识库服务
  │ 返回 Records[].Segment.Content + Score
  ▼
格式化检索结果 → 返回给 Agent
  │
  ▼
Agent 基于检索结果生成回答
  │ 标注信息来源文档
  ▼
返回给用户
```

**工具注入方式：**

- **自动注入**（agentengine run 时）: 只需设置 `KSADK_KB_DATASET_ID`，ADKRunner 自动注入
- **显式导入**（agent.py 中）: `from ksadk.knowledge_base.adk_tool import search_knowledge_base`

---

## 常见问题排查

### 知识库连不通

```bash
# 先跑检索测试，不需要 LLM
python test_kb_agent.py --retrieval-only
```

如果 Test 1 失败：
- 检查 `KSADK_KB_DATASET_ID` 是否正确
- 检查 AK/SK 是否有效
- 内网环境确认 endpoint 用 `aicp.inner.api.ksyun.com` + scheme 用 `http`
- 外网环境确认 endpoint 用 `aicp.api.ksyun.com` + scheme 用 `https`

### LLM API 报错

```
litellm.APIError: OpenAIException - unauthorized consumer
```

检查 `.env` 中 `OPENAI_API_KEY` 是否为有效 Key（不是占位符 `your_api_key`）。

### Agent 偶发 JSON 错误

```
Unterminated string starting at: line 1 column 11
```

这是 DeepSeek 模型偶尔生成不合法 JSON 的 tool call，重新运行即可。测试脚本已内置重试机制。

---

## 环境变量参考

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `OPENAI_API_KEY` | 是 | - | LLM API Key |
| `OPENAI_API_BASE` | 是 | - | LLM API 地址 |
| `MODEL_NAME` | 否 | `deepseek-v3.2` | 模型名称 |
| `KSADK_KB_DATASET_ID` | 是 | - | 知识库 ID |
| `KSYUN_ACCESS_KEY` | 是 | - | 金山云 AK |
| `KSYUN_SECRET_KEY` | 是 | - | 金山云 SK |
| `KSADK_KB_ENDPOINT` | 否 | `aicp.api.ksyun.com` | API 端点 |
| `KSADK_KB_SCHEME` | 否 | `https` | 协议 |
| `KSADK_KB_REGION` | 否 | `cn-north-vip1` | API 区域 |
| `KSADK_KB_TOP_K` | 否 | `5` | 返回结果数 |
| `KSADK_KB_SEARCH_METHOD` | 否 | `intelligence_search` | 检索方法 |
| `KSADK_KB_SCORE_THRESHOLD` | 否 | - | 分数阈值 |
| `KSADK_KB_RERANKING_ENABLE` | 否 | `false` | 重排序开关 |
