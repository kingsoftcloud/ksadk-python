# 自然语言驱动的 Agent 开发闭环：生成 → 验证 → 调试 → 迭代 → 部署

> 状态：草案 | 创建：2026-04-30 | 更新：2026-04-30

## 一句话

自然语言描述需求 → LLM 自动生成/修改 Agent 代码 → 自动验证是否符合预期 → 本地实时调试 → 自然语言优化 → 实时看效果 → 一键部署分发。

## 完整闭环

```
  ┌─────────────────────────────────────────────────────────┐
  │                                                         │
  │   ksadk create --prompt "做一个搜索+总结的 agent"         │
  │                    │                                    │
  │                    ▼                                    │
  │            LLM 生成代码                                 │
  │                    │                                    │
  │                    ▼                                    │
  │          自动验证（能否跑通？输出对不对？）                 │
  │               ╱    ╲                                   │
  │            通过      失败                               │
  │             │          │                               │
  │             ▼          ▼                               │
  │     本地调试交互    LLM 自动修 bug                       │
  │             │          │                               │
  │             ╲         ╱                                │
  │              ▼       ▼                                 │
  │     "加上知识库"  "换个模型"  ← 自然语言优化             │
  │              │                                         │
  │              ▼                                         │
  │        实时看效果（本地 Web UI）                         │
  │              │                                         │
  │        满意？ │                                         │
  │         ╱   ╲                                         │
  │      Yes     No → 继续自然语言修改                      │
  │       │                                               │
  │       ▼                                               │
  │   ksadk deploy → 一键上云 + A2A 分发                   │
  │                                                       │
  └─────────────────────────────────────────────────────────┘
```

## 核心场景

### 场景 1：从零创建 + 自动验证

```bash
$ ksadk create --prompt "帮我做一个能搜索网页、总结内容的 agent，需要人工审批"

🤖 LLM 分析:
   框架: LangGraph (原因: 需要人工审批中断点)
   工具: web_search, summarize
   流程: search → summarize → interrupt(审批) → output

   生成文件: ✓ agentengine.yaml ✓ agent.py ✓ tools.py ✓ .env ✓ requirements.txt

🔬 自动验证:
   ✓ 语法检查通过
   ✓ ksadk detect 识别为 LangGraph
   ✓ 依赖安装成功
   ▶ 启动本地测试...

   测试输入: "搜索金山云最新动态并总结"
   ✓ 工具调用: web_search("金山云最新动态") → 返回结果
   ✓ 工具调用: summarize(搜索结果) → 生成摘要
   ✓ 中断点触发: interrupt → 等待人工确认
   ✓ 流程完整，输出符合预期

   ✅ Agent 验证通过，可以本地调试
```

### 场景 2：本地调试 + 自然语言优化 + 实时看效果

```bash
$ ksadk dev
# 启动本地 Web UI（复用现有 ksadk server，支持热重载）

🌐 Local Dev Server: http://localhost:8000

   💬 你可以直接在 UI 里对话测试
   📝 或者用自然语言修改 agent:

> "加上长期记忆，让它能记住之前的对话"
🤖 修改中...
   + from ksadk.memory.adk import LongTermMemory
   + ltm = LongTermMemory.from_env(app_name="my_agent")
   + agent = graph.compile(checkpointer=checkpointer)  # checkpoint 已有

   🔄 热重载中... ✓ 完成

> "现在问它我之前聊过什么"
🤖 Agent: "根据我的记忆，你之前问过关于金山云的最新动态..."
   ✓ 长期记忆生效了

> "把总结步骤改成并行搜索3个来源再汇总"
🤖 修改中...
   - 顺序: search → summarize
   + 并行: [search_source1, search_source2, search_source3] → merge_summarize

   🔄 热重载中... ✓ 完成
   💬 你可以在 UI 里测试新流程
```

### 场景 3：一键部署 + A2A 分发

```bash
$ ksadk deploy

📦 构建: ksadk build --mode code
☁️  上传: → agentengine-server
🚀 部署: serverless pod 启动中... ✓

   线上地址: https://my-agent.agentengine.ksyun.com

📡 A2A 分发:
   ✓ Agent Card 已注册: /a2a/card
   ✓ 其他 agent 可通过 A2A 协议调用
   ✓ 访问控制: 公开

   分享链接: https://agentengine.ksyun.com/agents/my-agent
```

## 关键设计

### 1. 自动验证：生成后自动跑通

不是生成完就结束，而是**自动验证生成的东西能不能用**：

```
验证层级:

L1 语法校验 ──── Python AST 解析，确保代码能 import
   │
L2 框架识别 ──── ksadk detect，确保框架能正确检测
   │
L3 依赖安装 ──── pip install，确保依赖能装上
   │
L4 启动测试 ──── agent.load_agent()，确保能加载进内存
   │
L5 功能测试 ──── 用 LLM 生成测试输入，跑一次完整流程
   │              - 工具是否被正确调用？
   │              - 中断点是否触发？
   │              - 输出是否合理？
   │
L6 边界测试 ──── 空输入、超长输入、工具失败等
```

L5 是关键 — 用另一个 LLM 根据 agent 描述生成测试输入，自动跑一轮，检查：
- 工具调用链是否和预期流程一致
- 输出格式是否正确
- 中断/审批点是否按预期触发

如果 L5 失败，LLM 自动分析错误日志，尝试修复代码，再验证（最多 3 轮）。

### 2. 本地调试：dev 模式 + 热重载

复用现有 `ksadk server`，增加开发模式：

```bash
ksadk dev            # 启动本地 UI + 热重载 + 自然语言修改入口
```

**热重载机制：**
- 监听项目目录文件变更
- 检测到变更 → 自动 `load_agent(force_reload=True)`
- Web UI 无需刷新，下次对话即用新 agent

**自然语言修改入口：**
- 在 Web UI 里增加一个输入框（区别于 agent 对话框）
- 用户输入修改意图 → 调用 `ksadk edit` 逻辑 → 代码变更 → 热重载
- 用户在同一个 UI 里立刻测试效果

### 3. 实时效果：改完即看

```
┌───────────────────────────────────────────────────────┐
│  ksadk dev UI                                         │
│                                                       │
│  ┌─ Agent 对话 ──────────┐  ┌─ Agent 修改 ──────────┐│
│  │                       │  │                          ││
│  │  用户: 搜索金山云     │  │  > 加上知识库           ││
│  │  Agent: 正在搜索...    │  │  🤖 修改中...           ││
│  │  Agent: [工具调用结果] │  │  ✓ 代码已更新          ││
│  │  Agent: 总结如下...   │  │  🔄 热重载完成         ││
│  │                       │  │                          ││
│  │  用户: 记住这个话题    │  │  > 把记忆换成本地sqlite ││
│  │  Agent: 已记住。      │  │  🤖 修改中...           ││
│  │                       │  │  ✓ 完成                 ││
│  └───────────────────────┘  └────────────────────────┘│
│                                                       │
│  ┌─ Agent 状态 ──────────────────────────────────────┐│
│  │ 框架: LangGraph                                    ││
│  │ 能力: checkpoint(PostgresSaver), knowledge_base,   ││
│  │       memory(SQLiteSTM), tools(web_search,summarize)││
│  │ 模型: glm-5.1                                      ││
│  │ 最近修改: memory sqlite → postgres ✓               ││
│  └────────────────────────────────────────────────────┘│
└───────────────────────────────────────────────────────┘
```

### 4. 一键部署分发

```bash
ksadk deploy [agent_dir]
```

一条命令完成：

```
ksadk build (code/zip) → 上传 agentengine-server → 创建 serverless pod → 绑定 gateway 路由 → 注册 A2A card → 生成分享链接
```

**A2A 分发：**
部署完成后自动注册 Agent Card，其他 agent（包括非 ksadk 的）可通过 A2A 协议调用。
用户也可以生成分享链接，让其他人直接在 UI 里使用这个 agent。

## 命令全景

```bash
# 创建
ksadk create --prompt "需求描述"     # 自然语言创建
ksadk create --template langgraph    # 模板创建（现有功能）

# 修改
ksadk edit --prompt "修改描述"       # 自然语言增量修改

# 验证
ksadk test                           # 自动生成测试用例并验证
ksadk test --input "自定义测试输入"   # 用指定输入测试

# 调试
ksadk dev                            # 本地开发 UI + 热重载 + 自然语言修改

# 检查
ksadk inspect                        # 查看当前 agent 能力全景

# 部署
ksadk deploy                         # 一键部署上云
ksadk deploy --share                 # 部署 + 生成分享链接
ksadk deploy --a2a                   # 部署 + 注册 A2A card
```

## veadk Agent 的启发

veadk 采用声明式能力挂载：`Agent(knowledgebase=..., long_term_memory=...)` 一行搞定。
ksadk 多框架没法做统一类，但可以做**同一件事的两种表达**：

| 想做的事 | veadk 的方式 | ksadk 的方式 |
|---------|-------------|-------------|
| 加知识库 | `Agent(knowledgebase=KnowledgeBase(...))` | `ksadk edit --prompt "加上知识库"` |
| 加记忆 | `Agent(long_term_memory=LongTermMemory(...))` | `ksadk edit --prompt "加上长期记忆"` |
| 换 checkpoint | 改构造参数 | `ksadk edit --prompt "checkpoint 换成 postgres"` |
| 看当前能力 | 看 Agent 构造参数 | `ksadk inspect` |
| 测试效果 | 手动跑 | `ksadk dev` → UI 对话 |

**本质：veadk 用代码做声明，ksadk 用自然语言做声明。效果一样，入口不同。**

## 框架能力知识库

LLM 的 system prompt 里需要内置"框架能力手册"：

```yaml
# 示例：LangGraph 加 checkpoint
langgraph_checkpoint:
  trigger: ["checkpoint", "持久化", "断点续传", "保存状态"]
  changes:
    - file: agent.py
      add_imports: ["from langgraph.checkpoint.postgres import PostgresSaver"]
      add_code: "checkpointer = PostgresSaver.from_conn_string(os.getenv('CHECKPOINT_DB_URL'))"
      modify: "graph.compile()" → "graph.compile(checkpointer=checkpointer)"
    - file: .env
      add: ["CHECKPOINT_DB_URL=postgresql://..."]
    - file: requirements.txt
      add: ["langgraph-checkpoint-postgres>=0.1.0"]

# 示例：ADK 加知识库
adk_knowledge_base:
  trigger: ["知识库", "知识检索", "RAG"]
  changes:
    - file: agent.py
      add_imports: ["from ksadk.knowledge_base.client import KnowledgeBaseClient, search_knowledge_base"]
      add_code: |
        kb = KnowledgeBaseClient.from_env()
        agent.tools.append(search_knowledge_base)
      add_env: ["KSADK_KB_DATASET_ID=xxx", "KSADK_KB_REGION=cn-beijing-6"]

# 示例：LangGraph 加记忆（跨会话）
langgraph_long_term_memory:
  trigger: ["长期记忆", "跨会话记忆", "记住用户"]
  changes:
    - file: agent.py
      add_imports: ["from langgraph.checkpoint.postgres import PostgresSaver"]
      add_code: |
        # 用 checkpoint 存储跨会话状态
        checkpointer = PostgresSaver.from_conn_string(os.getenv('MEMORY_DB_URL'))
      modify: "graph.compile()" → "graph.compile(checkpointer=checkpointer)"
    - file: .env
      add: ["MEMORY_DB_URL=postgresql://..."]
```

## 自动验证详细设计

### L5 功能测试：LLM 生成测试 + 自动执行

```
输入: agent 描述 + 代码
     │
     ▼
1. 测试 LLM 根据 agent 描述生成 3-5 个测试输入
   - 正常场景: "搜索金山云最新动态"
   - 工具场景: "帮我总结这段话"
   - 中断场景: "需要我审批吗"
     │
     ▼
2. 对每个测试输入，执行 agent.invoke()
   - mock 掉真实 API（web_search 返回固定结果）
   - 记录完整事件链: [tool_call, tool_result, text_output, interrupt]
     │
     ▼
3. 另一个 LLM 判断事件链是否符合预期
   - 搜索 agent 是否调用了 search 工具? ✓
   - 审批 agent 是否触发了 interrupt? ✓
   - 输出是否合理（不是报错）? ✓
     │
     ▼
4. 全部通过 → ✅; 有失败 → 生成修复 → 重新验证（最多3轮）
```

### mock 策略

```python
# 项目根目录放 ksadk_mock.py，自动加载
MOCK_RESPONSES = {
    "web_search": {"results": "mocked search results about 金山云"},
    "summarize": {"summary": "mocked summary text"},
}

# ksadk test 时自动替换工具实现
# 真正的 API key 不需要配置，本地就能跑通
```

## 实现路径

### Phase 1：MVP — 从零创建 + 基础验证

- `ksadk create --prompt` 自然语言创建
- L1-L4 自动验证（语法 + 框架识别 + 依赖 + 加载）
- `ksadk test` 手动验证
- `ksadk dev` 本地调试（现有 server）

### Phase 2：增量修改 + 功能验证

- `ksadk edit --prompt` 增量修改
- 框架能力知识库（记忆/知识库/checkpoint/tracing/MCP）
- L5 功能测试（LLM 生成测试 + mock + 自动执行）
- 验证失败自动修复

### Phase 3：开发闭环

- `ksadk dev` 热重载
- Web UI 里集成自然语言修改入口
- `ksadk inspect` 能力全景
- 修改后实时看效果

### Phase 4：部署分发

- `ksadk deploy` 一键部署
- A2A card 自动注册
- 分享链接生成
- agentengine-server UI 集成

### Phase 5：生态

- 框架能力知识库社区贡献
- framework changelog 自动同步
- 测试用例共享
- agent 模板市场

## 开放问题

1. **LLM 调用成本**：创建 + 验证 + 修复可能多次调 LLM，费用谁出？
2. **Mock 真实性**：mock 的工具返回和真实差距大，验证通过≠线上没问题
3. **安全性**：生成的代码如果有危险操作怎么管控？
4. **框架知识更新**：各框架 API 变化快，能力知识库怎么保持最新？
5. **多框架组合**：是否支持一个需求生成多框架协作方案（A2A）？
6. **冲突处理**：用户手动改了代码后再 edit，LLM 的 diff 可能冲突
7. **幂等性**：同一个 edit prompt 执行两次，结果应该一致吗？
8. **热重载边界**：哪些变更可以热重载，哪些需要重启？（比如改了依赖）
9. **测试可靠性**：LLM 判断"输出是否符合预期"本身不可靠，怎么兜底？
