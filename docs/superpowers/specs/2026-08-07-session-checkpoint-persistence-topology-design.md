# Session 与 Checkpoint 双库持久化拓扑设计

## 1. 摘要

KsADK 需要同时支持 Session 存储与框架原生 Checkpoint 存储。用户可只配置其中一个 PostgreSQL 数据库，也可分别配置两个数据库实例。系统必须将缺失的一侧回退到已配置的一侧，同时不把显式本地 Session 配置误改为远程持久化。

本设计引入统一的“有效持久化拓扑”解析层。Session、ADK 与 LangGraph 系列 Runner 都消费该解析结果，而不是分别读取环境变量。能力展示和恢复门控分别检查有效 Session 库、有效 Checkpoint 库及框架原生 checkpointer 是否真正就绪。

## 2. 目标与非目标

目标：

- 未配置任一持久化库时，Session、Checkpoint 与 ResumeRun 明确声明不可用。
- 只配置 Session 库时，将该库作为有效 Checkpoint 库。
- 只配置 Checkpoint 库时，将该库作为有效 Session 库。
- 两者均配置时，Session 数据与框架 checkpoint 数据使用各自数据库。
- 显式 `KSADK_SESSION_BACKEND=local` 是主动本地模式，不触发远程 Session fallback。
- bootstrap 暴露 Session 与 Checkpoint 的独立、无凭据状态和来源。

非目标：

- 不承诺旧版 LangChain 获得原生 checkpoint 或恢复能力。
- 不修改控制面 API、数据库服务或数据库实例生命周期。
- 不在 capability 层伪造尚未创建的 LangGraph saver。

## 3. 配置模型

新增框架无关的 `KSADK_CHECKPOINT_DSN`，用于平台默认 Checkpoint PostgreSQL 数据库。现有框架变量继续兼容，并优先于该默认值。

| 使用者 | 有效数据库优先级 |
| --- | --- |
| KsADK Session | `KSADK_SESSION_DSN` → `KSADK_CHECKPOINT_DSN` |
| Google ADK 原生状态 | `KSADK_ADK_SESSION_URL` → `KSADK_CHECKPOINT_DSN` → `KSADK_SESSION_DSN` |
| LangGraph / 新版 LangChain / DeepAgents checkpoint | `KSADK_LANGGRAPH_CHECKPOINT_DSN` → `KSADK_CHECKPOINT_DSN` → `KSADK_SESSION_DSN` |

`KSADK_SESSION_BACKEND=postgres` 明确启用远程 Session；未设置该变量但存在可用的有效 Session DSN 时，拓扑解析器将 Session 判为 PostgreSQL。显式 `local`、`sqlite` 或 `memory` 不被该自动推断覆盖。

## 4. 有效拓扑与能力语义

| Session DSN | Checkpoint DSN | 有效 Session 库 | 有效 Checkpoint 库 | 来源 |
| --- | --- | --- | --- | --- |
| 无 | 无 | 无 | 无 | `none` |
| 有 | 无 | Session 库 | Session 库 | Checkpoint 为 `session_fallback` |
| 无 | 有 | Checkpoint 库 | Checkpoint 库 | Session 为 `checkpoint_fallback` |
| 有 | 有 | Session 库 | Checkpoint 库 | 均为 `explicit` |

恢复能力同时依赖两种状态：

1. 有效 Session 库必须 ready，用于 Session、事件、checkpoint 索引及框架引用。
2. 有效 Checkpoint 库必须 ready，用于 ADK 原生可恢复状态或 LangGraph saver。
3. Runner 必须已具备对应框架的原生 checkpoint 实现。仅有 DSN 不能让旧版 LangChain 或未配置 saver 的图获得恢复能力。

建议 bootstrap 保持兼容的顶层 `Persistence`，并新增独立状态：

```json
{
  "Persistence": { "Ready": true, "Source": "explicit" },
  "CheckpointPersistence": { "Ready": true, "Source": "session_fallback" }
}
```

顶层 `Persistence` 表示有效 Session 库；`CheckpointPersistence` 表示当前 Agent 框架的有效 checkpoint 库。状态不得包含 DSN、用户名或数据库地址。

## 5. 框架行为

### 5.1 Google ADK

KsADK Session 服务保存 Session、transcript、run checkpoint 事件和 KsADK invocation 到 ADK invocation 的映射。ADK `DatabaseSessionService` 保存 ADK 原生 invocation 状态。

因此双库场景中：

- Session 数据写入有效 Session 库。
- ADK 原生状态写入有效 Checkpoint 库。
- 恢复时先从 Session 库解析 checkpoint 与 invocation 映射，再使用 Checkpoint 库中的 ADK 状态恢复。

ADK 维持 `invocation_id` 的 forward-only 恢复语义，不声明 LangGraph 式 time-travel。

### 5.2 LangGraph、LangChain 与 DeepAgents

LangGraph 使用有效 Checkpoint DSN 创建或验证 PostgreSQL saver。自动注入仍要求 `KSADK_LANGGRAPH_AUTO_CHECKPOINT=1` 且项目导出 `ksadk_graph_factory(*, checkpointer)`；不修改已编译 graph 的私有字段。

新版 LangChain 与 DeepAgents 复用 LangGraphRunner，因此采用相同拓扑。旧版 LangChain 链没有原生 checkpoint，始终声明 checkpoint 不可用。

## 6. 实现方案比较

### 方案 A：在各处增加环境变量 fallback

分别在 Session、ADK、LangGraph Runner 中读取另一侧 DSN。

优点是改动少；缺点是不同调用点会继续产生不同结论，也无法独立探活或说明当前使用哪一个库。不推荐。

### 方案 B：统一 PersistenceTopology 解析层

建立不可变的配置对象，包含 Session/Checkpoint 的候选 DSN、有效 DSN、来源、是否显式 local，以及 framework override。各 Runner 与 bootstrap 只消费该对象。

优点是四态规则、优先级、日志脱敏和测试矩阵都有唯一真相源；改动范围适中。推荐。

### 方案 C：可插拔持久化 Provider Registry

定义可注册的 Session/Checkpoint Provider，支持 PostgreSQL 以外的数据库、按租户路由和更多框架。

长期扩展性最好，但当前仅需 PostgreSQL 双库时抽象过重。不推荐作为本阶段范围。

## 7. 推荐实现边界

采用方案 B，新增 `PersistenceTopology`、`StorageTarget` 和无凭据 `StorageStatus`。

- Session resolver 创建平台 SessionService 时使用有效 Session target。
- ADK ShortTermMemory 接收有效 ADK checkpoint target，而不是自行读取散落环境变量。
- LangGraphRunner 使用有效 LangGraph checkpoint target；其原生 saver 初始化失败时写入 checkpoint 专属失败原因。
- bootstrap 分别探测两个 target；若它们引用相同 DSN，复用一次探测结果。
- checkpoint capability gate 要求 Session 与 Checkpoint 同时 ready，并要求框架 native capability 已初始化成功。
- `agentengine web` 必须尊重显式运行时持久化配置，不能在解析后的 target 上再次覆盖为本地 SQLite。

建议稳定原因码：`SESSION_STORE_NOT_CONFIGURED`、`CHECKPOINT_STORE_NOT_CONFIGURED`、`SESSION_STORE_UNREACHABLE`、`CHECKPOINT_STORE_UNREACHABLE`、`CHECKPOINTER_NOT_CONFIGURED` 与 `CHECKPOINTER_NOT_DURABLE`。

## 8. 影响范围与验证

预计修改 7–9 个生产模块：Session 配置/探针、ADK memory 与 runner、LangGraph runner、bootstrap 路由、CLI 本地运行时、环境变量注册与文档。LangChain、DeepAgents 通过 LangGraphRunner 继承，不需要独立实现。

构建器已经为 ADK 与 LangGraph 打包 PostgreSQL 相关依赖；需要补充对仅 `KSADK_CHECKPOINT_DSN` 场景的依赖判定测试。

必须覆盖：

- 四种配置组合及显式 local opt-out。
- Session 与 Checkpoint 分别不可达、无建表权限、依赖缺失。
- ADK 双库 invocation 恢复与单库 fallback 恢复。
- LangGraph saver 的双库、单库 fallback、factory 缺失和初始化失败。
- bootstrap 不回显连接信息，并准确展示两个状态与原因码。
- `agentengine web`、构建包与部署运行时的环境传播。

## 9. 迁移与兼容

现有 `KSADK_SESSION_DSN`、`KSADK_ADK_SESSION_URL` 和 `KSADK_LANGGRAPH_CHECKPOINT_DSN` 保持有效。未配置新变量时，单库部署继续使用既有 Session DSN 作为 checkpoint fallback。新 `KSADK_CHECKPOINT_DSN` 仅提供跨框架的标准 checkpoint 默认值，不改变显式框架专用 DSN 的优先级。
