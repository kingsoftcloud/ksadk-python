# ksadk 能力对标与迭代规划

> 状态：草案 | 创建：2026-04-30 | 更新：2026-04-30

---

## 一、veadk vs ksadk 功能全景对比

| 能力维度 | veadk | ksadk | 差距 |
|---------|-------|-------|------|
| **框架支持** | 仅 ADK | ADK / LangGraph / LangChain / DeepAgents | ksadk 强 |
| **Agent 组合** | Sequential / Parallel / Loop / Supervisor | Sequential / Parallel / Loop + RunnerAdapter | 基本持平 |
| **短期记忆** | local / mysql / sqlite / postgresql | InMemory / SQLite / (ADK DB) | **veadk 后端更丰富** |
| **长期记忆** | local / opensearch / redis / vikingdb / mem0 | HTTP / SDK (金山云 AICP) | veadk 后端更丰富 |
| **知识库** | local / opensearch / redis / viking / tos_vector / context_search | 金山云 AICP KnowledgeBase | veadk 后端更丰富 |
| **内置工具** | 22 个 (图像/视频/TTS/搜索/飞书/浏览器...) | sandbox + MCP toolsets | **veadk 大幅领先** |
| **MCP 支持** | TrustedMcpToolset (带认证注入) | MCP runtime (环境变量驱动) | 基本持平 |
| **A2A** | VeA2AServer + RemoteVeAgent + Hub | A2AServer + card_builder | 基本持平 |
| **Tracing** | OTel + APMPlus / Cozeloop / TLS 三导出器 | OTel + Langfuse / OTLP | 基本持平 |
| **CLI 部署** | VeFaaS + APIG + Identity | code zip + container + serverless | ksadk 模式更多 |
| **Web UI** | ADK Web (简单包装) | 自研完整 UI + hosted 版 | **ksadk 领先** |
| **实时语音** | 完整二进制协议 + WebSocket | 无 | **veadk 独有** |
| **评估系统** | ADKEvaluator + DeepEval | 无 | **veadk 独有** |
| **技能系统** | 完整 (本地/云端/AIO沙箱/检查清单) | 无 | **veadk 独有** |
| **Reflector** | 基于轨迹的自我反思+提示优化 | 无 | **veadk 独有** |
| **提示管理** | PromptManager + PromptPilot + 优化/评估 | 无 | **veadk 独有** |
| **Supervisor** | 自定义 Flow 覆盖 | 无 | **veadk 独有** |
| **Ghostchar** | 防上下文遗忘 | 无 | **veadk 独有** |
| **数据集生成** | after_callback 自动生成 RL 数据 | 无 | **veadk 独有** |
| **RL 脚手架** | ark / lightning 平台初始化 | 无 | **veadk 独有** |
| **Vanna SQL** | ClickHouse / VikingDB 集成 | 无 | **veadk 独有** |
| **认证体系** | 完整 VeIdentity (OAuth2 / TIP token) | 基础 KCR registry auth | **veadk 领先** |
| **记忆压缩** | LLM 驱动对话历史压缩 | 会话 compaction | 基本持平 |
| **模型切换** | 构造参数 + fallback 列表 | 环境变量 + 运行时热切换 | 各有千秋 |

---

## 二、短期记忆问题深度分析

### 现状：多 pod 部署必然丢记忆

ksadk 的短期记忆（session state）存在严重的生产可用性问题：

**默认全内存，所有数据进程级隔离：**

| 后端 | 存储 | 持久化 | 多 pod 安全 |
|------|------|--------|------------|
| `InMemorySessionService` (默认) | Python dict | 否，重启丢失 | **否** |
| `LocalSessionService` (SQLite) | 本地 .sqlite 文件 | 单节点持久 | **否，文件 pod 本地** |
| `MemoryManager` + `InMemoryBackend` | Python dict | 否 | **否** |
| `MemoryManager` + `RedisBackend` | Redis | 是 | **是（如 Redis 共享）** |
| ADK `DatabaseSessionService` (SQLite) | aiosqlite | 单节点持久 | **否** |
| ADK `DatabaseSessionService` (Postgres) | SQLAlchemy | 是 | **是（如 DB 共享）** |

**问题全景：**

1. **默认路径全丢** — 每个框架 Runner 的默认 session 都是在内存里，pod 重启或调度到不同 pod 即丢失
2. **两套 session 系统不互通** — `conversations/runtime.py` 的 session service（事件日志/压缩）和框架原生的 session 机制（ADK SessionService / LangGraph checkpointer）是独立运行的，互不同步
3. **Redis 只用于 KV，不用于 Session** — `RedisBackend` 存在但只服务于 `MemoryManager` 的 KV 存储，`BaseSessionService` 没有 Redis 实现
4. **Postgres 只在 ADK 侧** — ADK 的 `DatabaseSessionService` 支持 Postgres URL，但 `ksadk.sessions` 自身没有 Postgres 实现
5. **ADKRunner 的 session 映射在内存** — `_session_map: Dict[str, str]` 是进程内存字典，pod 切换后外部 session_id 到内部 ADK session_id 的映射丢失
6. **无分布式锁** — 多 pod 并发写同一 session 可能损坏数据，SQLite 场景尤其危险
7. **无 session 过期/清理** — session 无限累积，没有 GC 机制
8. **HTTP LTM 是空壳** — API 合约标记为 TODO，云端长期记忆实际不可用
9. **LangChain/LangGraph 无默认持久化** — 不配置 hook/checkpointer 就回退到 transcript replay（把历史拼进 prompt），跨 pod 完全丢失
10. **压缩状态随 session 丢失** — compaction boundary 和 checkpoint 写在 session 里，session 丢了压缩也白做

### 核心问题：两套 session 系统

```
用户请求
   │
   ▼
┌──────────────────────────────────┐
│ conversations/runtime.py        │
│ (session_service = InMemory/     │
│  SQLite, 用于事件日志和压缩)      │
│                                  │
│  session A: [event1, event2,     │
│   compaction_checkpoint, ...]    │
└──────────┬───────────────────────┘
           │ 完全独立，互不知道
           ▼
┌──────────────────────────────────┐
│ 框架原生 session                 │
│                                  │
│  ADK: Runner + SessionService   │
│  LangGraph: checkpointer        │
│  LangChain: message_history      │
│                                  │
│  session A: [messages, state]    │
└──────────────────────────────────┘
```

两套系统各自管各自的状态，同一个对话在两个系统里的 session_id 可能不一致，状态也不同步。

### 解决方案：统一共享 session 层

```
┌──────────────────────────────────────────┐
│           统一 Session 抽象层             │
│                                          │
│  RedisSessionService  (推荐生产默认)      │
│  PostgresSessionService                  │
│  SQLiteSessionService  (本地开发)         │
│  InMemorySessionService (测试)           │
│                                          │
│  统一接口:                                │
│  - create_session()                      │
│  - get_session()                         │
│  - append_event()                        │
│  - get_events()                          │
│  - get_state() / update_state()          │
│  - compact()                             │
│                                          │
│  两个消费者都读写同一个后端:              │
│  - conversations/runtime → 事件+压缩     │
│  - 框架 Runner → 消息历史+状态           │
└──────────────────────────────────────────┘
```

**关键改动：**

1. **新增 `RedisSessionService`** — 生产默认，所有 pod 共享同一个 Redis
2. **新增 `PostgresSessionService`** — 企业场景，已有 PG 实例可复用
3. **消除两套系统** — conversations/runtime 和框架 Runner 都通过统一抽象层读写
4. **ADKRunner 去掉 `_session_map`** — 外部 session_id 直接作为内部 session_id，映射关系存 Redis
5. **LangGraph 默认挂 checkpointer** — `MemorySaver` 本地开发，`PostgresSaver` 生产
6. **Session TTL** — 默认 24h 过期，自动清理
7. **分布式锁** — 同一 session 并发写入时 Redis 加锁

---

## 三、值得跟进的 5 个方向

### 1. 内置工具生态（最大差距）

veadk 有 22 个开箱即用的内置工具，ksadk 几乎是零。用户想用搜索/图像/TTS 都得自己写。

**建议优先补的高频工具：**

| 工具 | 用途 | 优先级 |
|------|------|--------|
| `web_search` | 网页搜索 | P0 |
| `link_reader` | URL 内容提取 | P0 |
| `code_runner` | 代码沙箱执行 | P0 |
| `image_generate` | 图像生成 | P1 |
| `tts` / `asr` | 语音合成/识别 | P1 |
| `web_scraper` | 网页抓取 | P1 |

### 2. 评估系统（质量保障闭环）

veadk 有完整的 eval 框架：从追踪 JSON 自动构建测试集、支持 ADK + DeepEval 多种评估器。ksadk 只能"跑通"，不能判断"跑得好不好"。

**建议：**
- `ksadk eval` — 基于黄金数据集的对比评估
- LLM-as-judge 质量评估
- 和自然语言修改闭环打通：生成 → 验证 → 评估 → 优化

### 3. 提示优化（从能用变好用）

veadk 有三层提示优化：Reflector（轨迹反思）、PromptPilot（调优服务）、PromptManager（版本管理）。ksadk 完全没有。

**建议：**
- Phase 1：`ksadk optimize` — 基于执行轨迹的提示反思（本地 LLM 驱动）
- Phase 2：和自然语言修改闭环结合 — "这个 agent 回答不够准确" → 分析轨迹 → 优化 prompt → 重新测试

### 4. 技能系统 + 检查清单（生产级质量保证）

veadk 的技能不只是"工具"，而是打包的领域知识 + 结构化检查清单。agent 执行技能时必须逐步完成 checklist，保证不遗漏步骤。

**建议：**
- 短期：给 MCP 工具加 `validation_steps` 元数据，执行时自动校验
- 长期：如果做 agent 市场，技能系统是分发的自然单元

### 5. 实时语音（差异化能力）

veadk 实现了火山引擎实时对话的完整二进制 WebSocket 协议。ksadk 没有任何语音能力。

**建议：**
- 看业务需求，优先级低于前 4 项
- 如果做，先基于金山云语音服务做 WebSocket 协议适配
- 架构上可抽象为 `VoiceRunner`

---

## 四、迭代优先级

```
紧急且重要（生产阻塞）:
  ① 短期记忆共享 — 多 pod 部署现在就是坏的
  ② 内置工具 — 没工具的 agent 是残废的

重要不紧急（竞争力）:
  ③ 评估系统 — 没评估就没有质量保障
  ④ 提示优化 — 让 agent 从能用变好用

按需（差异化）:
  ⑤ 技能/检查清单 — 生产环境质量保证
  ⑥ 实时语音 — 差异化但非刚需
```

---

## 五、短期记忆共享 — 具体实施路径

### Phase 1：RedisSessionService（生产默认）

```python
# 使用方式 — 环境变量即可切换
KSADK_SESSION_BACKEND=redis
KSADK_SESSION_URL=redis://redis.agentengine.svc:6379
KSADK_SESSION_TTL=86400

# 代码
from ksadk.sessions import create_session_service
service = create_session_service()  # 自动根据环境变量选择后端
```

**实现要点：**
- Redis Hash 存 session 元数据 (agent_id, user_id, title, state, version)
- Redis List 存 events (append-only, 按 seq_id 排序)
- Redis String 存 state (JSON, 带 version 做 CAS)
- TTL 继承到所有 key
- 分布式锁：`SETNX session_lock:{session_id}` 写入前获取

### Phase 2：统一 session 抽象

- `conversations/runtime.py` 不再自己管 session，改为调用统一 session service
- 框架 Runner 侧：ADKRunner 使用 `DatabaseSessionService(redis_url)`，LangGraphRunner 默认挂 `RedisSaver`
- 两套系统变成一个后端的两个读写视图

### Phase 3：PostgresSessionService

- 企业客户已有 PG 实例，不愿额外运维 Redis
- 事件存储用 PG JSONB，支持复杂查询和索引
- LangGraph 的 `PostgresSaver` 可以复用同一个 PG 实例

### Phase 4：Session 生命周期管理

- Session TTL 默认 24h，可配置
- 过期 session 后台异步清理
- Session 级别统计（消息数、token 数、最后活跃时间）
- 接入 agentengine-server 管理面
