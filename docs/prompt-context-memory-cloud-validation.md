# Prompt、Context 与 Memory 云端预发验证手册

> 适用分支：`feature-prompt-context-optimize`
> 目标：把当前工作区源码随 LangGraph Code 制品部署到 AgentEngine Serverless，验证本地与云端行为合同、跨 Session 记忆、长会话降载和基线指标。
> 原则：先预发、单副本、显式开启；不把云账号 AK/SK、模型 Key 或其他凭证提交进仓库。

## 1. 当前已经具备的部署接线

当前分支已补齐标准 `agentengine launch` 路径所需的四项接线：

1. `agentengine.yaml` 的 `context` 配置会进入 `RuntimeLaunchContext`，并投影为 `agent_system`、`agent_task` 和 `prompt_integration_mode`；Studio 与标准 Code 部署不再走两套开关逻辑。
2. Serverless 部署器自动注入 `KSADK_DEPLOYMENT_MODE=ksadk_managed_cloud`，但它不改变 Runner ownership；LangGraph 只有显式声明 `prompt_ownership: ksadk` 才进入 hosted 接管，Codex 仍保持 `native_runtime`。
3. 每轮结束和 compaction 前提取的长期记忆按可信 `user_id` 写入，不再误用 `session_id`，因此可以验证跨 Session 召回。
4. `KSADK_BASELINE_FLUSH_EACH_TURN=true` 时，基线 JSONL 每轮原子落盘，不依赖云端长驻进程退出。

以上行为均受开关控制；未开启 V2 或未声明 KsADK ownership 时，继续走原有 Runner 路径。

## 2. 需要准备的最小信息

不需要把任何明文凭证发到聊天或提交到 Git。请在本机通过 `agentengine config` 或未跟踪的 env 文件配置。

| 类别 | 最小信息 | 用途 |
|---|---|---|
| 云账号 | 金山云 AK/SK、目标 Region，账号具备 AgentEngine 与 Code 制品 KS3 权限 | 本机构建与部署 |
| 预发资源 | 可创建或更新的 Agent 名称；建议独立名称，如 `ksadk-pcm-canary` | 避免影响现有 Agent |
| 模型 | OpenAI 兼容 Base URL、模型名、模型 API Key | 运行 LangGraph canary |
| 网络 | 仅当模型或 Memory endpoint 为内网时，提供 VPC、子网、安全组；否则先开公网验证 | Pod 访问依赖 |
| 存储 | 允许创建至少 20 Gi PVC，挂载到 `/home/node/.agentengine` | 保存 Session、SQLite canary Memory 和基线文件 |
| 可观测 | Dashboard/Trace 的查看权限；若使用文件 API，还需 workspace 文件访问权限 | 取证和前后对比 |

生产 Memory Service 的 endpoint、`MemoryCollectionId` 和运行时身份不是第一轮 canary 的阻塞项。第一轮可以先用单副本 + PVC SQLite 验证平台合同；通过后再接生产 Provider。SQLite 不适合作为多副本生产记忆库。

## 3. Agent 配置

在待部署的 LangGraph Agent 项目中，为 `agentengine.yaml` 增加 `context`。这里的 system/task 是 Build 事实源，不要同时在 graph 节点内重复拼一份 system prompt。

```yaml
name: ksadk-pcm-canary
framework: langgraph
entry_point: agent.py
agent_variable: root_agent

context:
  prompt_ownership: ksadk
  agent_system: |
    你是 KsADK Prompt、Context 与 Memory 云端验证 Agent。
  agent_task: |
    回答必须简洁；涉及用户偏好时，只使用当前请求或已召回的可信记忆。
```

当前 CodeBuilder 会把正在运行的本仓 `ksadk` 源码打进 Code 制品。因此应使用本仓 `.venv/bin/agentengine` 执行构建，不能改用全局旧版 CLI：

```bash
/Users/zhangxiuqi/ksadk-python/.venv/bin/agentengine --version
```

## 4. 运行时配置文件

在 Agent 项目目录创建不提交的 `preprod.env`：

```dotenv
OPENAI_API_KEY=<model-api-key>
OPENAI_BASE_URL=<openai-compatible-base-url>
OPENAI_MODEL_NAME=<model-name>

KSADK_CONTEXT_ENGINE_V2_ENABLED=true
KSADK_MEMORY_ENABLED=true
KSADK_MEMORY_FLUSH_ENABLED=true

KSADK_SESSION_BACKEND=local
KSADK_SESSION_PATH=/home/node/.agentengine/sessions.sqlite
KSADK_MEMORY_DB_PATH=/home/node/.agentengine/memory.db

KSADK_BASELINE_COLLECT=true
KSADK_BASELINE_FLUSH_EACH_TURN=true
KSADK_BASELINE_PATH=/home/node/.agentengine/context-baseline.jsonl
```

`KSADK_DEPLOYMENT_MODE` 不必手写，Serverless 部署器默认注入 `ksadk_managed_cloud`。如显式传入，部署器会保留调用方值。

> 预发实测注意：不能只依据 `--storage-mount-path` 假定 SQLite/JSONL 已持久化。当前
> `ksadk-pcm-canary` 在滚动更新后观察到 `/home/node/.agentengine/memory.db` 记录归零，而
> workspace 文件 API 映射到 `/app/code/.agentengine/ui/workspace`。正式验收前必须通过
> “写入 → 更新 Agent → 再读取”证明真实挂载路径和 PVC 复用关系；未通过时，SQLite 只能
> 用于同一副本版本内的功能验证，不能宣称跨发布持久化。

## 5. 部署步骤

先验证本地 Agent，确认入口、依赖和模型配置无误：

```bash
/Users/zhangxiuqi/ksadk-python/.venv/bin/agentengine run <agent-project-dir> -i
```

审查 dry-run。必须确认目标为 Serverless Code、Region 正确、挂载目录正确，并且输出中没有意外凭证明文：

```bash
/Users/zhangxiuqi/ksadk-python/.venv/bin/agentengine launch <agent-project-dir> \
  --target serverless \
  --artifact-type Code \
  --name ksadk-pcm-canary \
  --region cn-beijing-6 \
  --enable-public-access \
  --storage-size-gi 20 \
  --storage-mount-path /home/node/.agentengine \
  --env-file <agent-project-dir>/preprod.env \
  --no-cache \
  --dry-run
```

dry-run 审核通过后，删除最后的 `--dry-run` 执行真实部署。部署是外部状态变更，应由有权限的人员确认预发 Agent 名称和 Region 后执行。

```bash
/Users/zhangxiuqi/ksadk-python/.venv/bin/agentengine launch <agent-project-dir> \
  --target serverless \
  --artifact-type Code \
  --name ksadk-pcm-canary \
  --region cn-beijing-6 \
  --enable-public-access \
  --storage-size-gi 20 \
  --storage-mount-path /home/node/.agentengine \
  --env-file <agent-project-dir>/preprod.env \
  --no-cache \
  --auto-rollback
```

查看状态并等待 Ready：

```bash
/Users/zhangxiuqi/ksadk-python/.venv/bin/agentengine agent status \
  ksadk-pcm-canary --region cn-beijing-6 --watch
```

## 6. 必跑验证 Case

### Case A：基本 Prompt 接管

```bash
/Users/zhangxiuqi/ksadk-python/.venv/bin/agentengine agent invoke \
  ksadk-pcm-canary --region cn-beijing-6 \
  --message "先只回答：PCM-CANARY-OK"
```

`agentengine agent invoke` 当前只支持 pretty 输出；`--output json` 会返回 usage error。
结构化状态请使用 `agentengine agent status --output json`。

验收：请求成功；Trace/基线同时记录 `deployment_mode=ksadk_managed_cloud` 与 hosted integration；未出现重复 system prompt。

### Case B：同 Session 连贯性

先生成一个固定 Session ID，例如 `pcm-session-a`，连续调用：

```bash
agentengine agent invoke ksadk-pcm-canary --region cn-beijing-6 \
  --session pcm-session-a --message "本轮代号是蓝鲸，只回复已记录"
agentengine agent invoke ksadk-pcm-canary --region cn-beijing-6 \
  --session pcm-session-a --message "刚才的代号是什么？"
```

验收：第二轮回答“蓝鲸”；Session 中只有一份规范历史，不因 KsADK 与 Runner 双重注入而重复。

### Case C：跨 Session 长期记忆

```bash
agentengine agent invoke ksadk-pcm-canary --region cn-beijing-6 \
  --session pcm-memory-write --message "记住：我偏好所有部署命令先 dry-run"
agentengine agent invoke ksadk-pcm-canary --region cn-beijing-6 \
  --session pcm-memory-read --message "我的部署偏好是什么？"
```

验收：第二个 Session 能召回“先 dry-run”；Memory 记录 scope 为 `user`，scope_id 为可信调用身份对应的 user id，而不是两个 session id。若网关没有稳定传递同一 user id，此 Case 应失败并作为身份投影缺口记录，不能退化成全局共享记忆。

### Case D：长会话与 PTL

在同一 Session 连续发送足够长的工具结果/文本，直到跨过 soft limit，再继续到 hard limit。验收：

- soft/hard/emergency 触发带可解释；
- compaction 后仍能回答当前目标、已完成步骤、下一步和关键约束；
- 不出现孤立 tool result；
- PTL 率不高于旧路径，发生 PTL 时最多按既定策略恢复一次；
- compaction 前 Memory Flush 失败不会阻断紧急压缩。

### Case E：回退

复制一套环境或更新同一预发 Agent，将 `KSADK_CONTEXT_ENGINE_V2_ENABLED=false`、`KSADK_MEMORY_FLUSH_ENABLED=false`，重复 A/B。验收：恢复原 Runner 行为且请求仍成功。不要用生产 Agent 做首次回退演练。

## 7. 证据收集

每轮基线文件包含 turn 记录和末尾 summary，只记录 hash、token 数、分类、延迟、PTL/重试等，不包含 Prompt 正文。若预发文件 API 对挂载目录开放，可下载：

```bash
agentengine files download ksadk-pcm-canary \
  --region cn-beijing-6 \
  --remote-path context-baseline.jsonl \
  --output-path ./artifacts/context-baseline.jsonl
```

如果文件 API 未映射该挂载目录，则需要平台侧提供 Pod 日志/文件导出或 OTEL 指标出口；这是环境能力缺失，不应把“文件已在 Pod 内生成”当作已经完成云端评测。

每次验证至少保留：

- Agent ID、Build/Version、Region、部署时间；
- 当前 Git commit 与工作区补丁标识；
- Prompt compiler、Context policy、capability hash；
- 测试 Case、Session ID、输入和脱敏输出；
- 基线 JSONL/Trace 链接；
- 旧路径与 V2 的成功率、PTL、输入 token、延迟、任务正确率对比；
- 回退是否成功。

## 8. 第一轮不宣称的内容

单副本 PVC SQLite canary 通过，只能证明 canonical 主链、持久化和作用域合同可运行，不能证明生产长期记忆已经完成。生产验收还需要：

1. 接入 `LongTermMemoryService`/HTTP/SDK Provider；
2. 使用平台可信 tenant/workspace/user principal，而不是请求方可伪造字段；
3. 多副本并发、版本冲突、删除、TTL、故障降级和数据隔离测试；
4. Memory Service 的最小权限运行时身份与审计。

## 9. 2026-08-07 首轮真实预发结果

验证对象：`ksadk-pcm-canary`，Region `cn-beijing-6`，Agent ID
`ar-20260807141359-21a82339`，Serverless Code，单副本。

| Case | 结果 | 证据与结论 |
|---|---|---|
| 制品/状态 | 通过后出现控制面故障 | 当前工作区 `ksadk` 已进入 Code ZIP；控制面曾收敛为 `RUNNING`、1/1 Ready，随后更新期间 GetAgent/Deploy 连续返回 502，但既有数据面仍可调用 |
| A 基本请求 | 通过 | 返回 `PCM-CANARY-OK` |
| B 同 Session | 通过 | 第一轮写入代号“白鹭”，第二轮只回答“白鹭” |
| C 写入 | 通过（同一部署版本） | active 记录 1 条，scope=`user` 且默认可信用户记录 1 条 |
| C Provider 检索 | 通过 | SQLite 中文自然问法直接命中 1 条 |
| C 跨 Session 注入 | 通过 | 新 Session 回答“先 dry-run”，模型输入明确包含召回片段 |
| 滚动更新持久化 | 未通过 | 更新 Agent 后记录由 1 变 0；需修正真实 PVC 路径或改接生产 Memory Provider |
| D 长会话 Compaction | 机制通过、行为未通过 | 降低 canary 阈值后，第 5 轮真实触发 1 次 compaction；baseline 为 `last_compacted=true`、`last_trigger=auto`、计划输入 12,228 tokens，但原“不得操作生产环境”和精确下一步未被稳定恢复，形成 Working State/摘要 Bad Case |
| D PTL | 未完成 | 本轮模型没有返回真实 PTL，baseline 为 `ptl_rate=0`、`retry=0`；一次性模拟 Provider PTL 的测试版本因控制面 Deploy 502 未上线，不能记为通过 |
| E 回退 | 已准备、未执行 | 本地 canary env 已切换 `KSADK_CONTEXT_ENGINE_V2_ENABLED=false`、`KSADK_MEMORY_FLUSH_ENABLED=false`；控制面连续 502，尚未滚动更新，数据面仍运行上一可用版本 |

本次预发发现并修复了三类真实缺口：

1. Hosted Assembler 的空 history 未覆盖旧 history，且 Planner 保留优先级顺序被误当作
   消息时序，导致当前输入重复；
2. `MemoryRecallContributor` 未导入，但异常被 best-effort 分支吞掉，导致 Provider 可命中而
   请求级 Context 没有记忆；
3. Code/Container 生成的 `entrypoint.py` 没有把 `agentengine.yaml.context` 写入
   `RuntimeLaunchContext.config`，导致云端静默丢失 `prompt_ownership` 和 hosted 接管模式。

因此当前结论是：Prompt/Context/Memory 的 LangGraph hosted 主链已在真实云端单副本中跑通；
真实主动 Compaction 也已触发，但压缩后的 Working State 连续性未达到验收要求。SQLite 跨滚动
更新持久化、真实 PTL、回退演练和生产 Memory Provider 仍未完成。控制面状态曾从 `CREATING`
收敛为 `RUNNING`，说明原状态同步问题不是永久状态；但后续 GetAgent/Deploy 连续返回 502，
而已有数据面仍能返回 `PCM-DATA-PLANE-OK`，需要将控制面可用性作为独立问题排查。

## 10. 2026-08-10 修复后复测结果

验证对象仍为 `ksadk-pcm-canary`，Agent ID 与 Endpoint 不变。当前源码提交为
`fa83074`；最终恢复版本为 `v27`，控制面状态 `RUNNING`、1/1 Ready。复测先上线 V2，
再临时降低压缩阈值验证 Compaction，随后关闭 V2 演练回退，最后恢复 V2 和默认阈值。

| Case | 结果 | 证据与结论 |
|---|---|---|
| 制品与状态 | 通过 | 当前仓库源码进入 Serverless Code 制品；最终 `v27` 为 `RUNNING`、1/1 Ready |
| A Prompt 接管 | 通过 | 返回 `PCM-FINAL-OK`；最终 `ChatOpenAI` span 的模型输入为 1 条 system + 1 条 user，system 中各只有 1 个 `agent_identity` 和 `agent_policy`，未重复注入 |
| B 同 Session | 通过 | Chat Completions、Responses 的流式与非流式四种组合均能恢复同 Session 校验词；最终隔离 Session 恢复“银杏” |
| C 跨 Session Memory | 通过（同一部署版本） | 写 Session 保存“先预发灰度再正式发布”，新 Session 正确召回；Provider 诊断 `records=1`、`direct_recall=1` |
| C 滚动更新持久化 | 未通过 | `v21` 的 5 条 active 记录在 `v23` 更新后丢失；控制面虽返回 `mount_path=/home/node/.agentengine`、`size_gi=20`，实际数据未复用，不能把 SQLite 当生产 Memory |
| D Compaction | 通过 | canary 临时使用 soft/hard 1%/2%；第 5 轮触发 `compactions=1`、`last_trigger=auto`、`last_planned=20890`，无 PTL 和重试 |
| D Working State | 通过 | 压缩后准确恢复“预发支付接口回归”“绝不能操作生产环境”“已完成登录/查询”“下一步验证退款幂等性”四个 P0 字段 |
| D 真实 PTL | 未触发 | Context 规划和主动压缩在本轮避免了 PTL，`ptl_rate=0`；Provider 真正返回 PTL 时的一次受控重试仍只有确定性测试证据 |
| E 开关回退 | 通过 | `v25` 关闭 `KSADK_CONTEXT_ENGINE_V2_ENABLED` 和 `KSADK_MEMORY_FLUSH_ENABLED` 后，基本调用和同 Session 继续成功；随后 `v27` 恢复 V2，Agent ID/Endpoint 不变 |
| 恢复后稳定性 | 通过，记录切换窗口 | 恢复后 5 个隔离 Session 连续返回 `STABLE-0..4`；滚动切换早期 Memory 请求曾短暂返回 500，重试后恢复，需纳入发布就绪/连接排空观测 |

与首轮相比，当前代码已经关闭两个关键缺口：

1. Studio Evidence 与真实 Runtime 不再各自准备一次请求；同一个
   `PreparedConversationTurn` 同时用于证据和执行，避免 prompt hash、invocation id 和
   ContextPlan 因二次准备发生漂移。
2. Working State 采用严格四字段验收和压缩前状态合并，首轮 Bad Case 已在真实云端复测通过。

当前不能宣称“全部生产完成”。剩余阻塞属于生产基础设施和跨仓治理：

1. 接入 `LongTermMemoryService`/HTTP/SDK Provider，并完成多副本、租户隔离、TTL、删除、
   冲突和故障降级测试；Serverless SQLite/PVC 当前不满足滚动持久化要求。
2. 由 AgentEngine 控制面把 Ready、数据面可调用和旧副本排空形成一致发布状态；本轮观察到
   Ready 后的短暂非 JSON/500 窗口。
3. 在可控 Provider 或专用测试模型上补真实 PTL 注入，验证最多一次恢复和失败语义；不能用
   “没有发生 PTL”冒充 PTL 恢复已通过。
