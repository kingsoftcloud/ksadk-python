# Phase 1 Agent Kernel + Interaction/Web 0.3.2 — Review 交接记录

> 2026-08-23 当前结论（优先于下方历史章节）：Studio 声明式 Agent 已进入共享预发
> 主流程，不再依赖 `agent-kernel-phase1` canary。现有 Agent
> `ar-20260823075542-cce1df89` 已通过 Studio 原地 UpdateAgent 到带来源证明的
> Codex Runtime；Operator 观测到 KsADK `0.8.1`、commit `7bbb491d…`、wheel
> SHA `525340bc…`，`AgentKernelRuntimeIdentityReady=True` 且
> `AgentKernelReady=True`。共享预发 Server/Gateway/Runtime-Service/Operator/
> Hosted UI 已滚动，Gateway 最新为 `79fc4fe`（Helm revision 82）。本地 Agent、
> Studio 云端 Agent 和 Hosted UI 的真实模型多轮、会话删除均通过；评测已有
> 1/1 PASS 报告。测试会话为 0，旧 canary namespace 已删除，另删除 11 个名称
> 明确的旧测试 Agent。完整、脱敏的当前事实见
> `preprod/studio-main-flow-closure.json`。
>
> **但完整 Phase 1 release gate 当前仍是红灯。** 用 raw evidence（而非历史聚合
> report）重跑 gate 时，durability/fencing/rollback 证据仍绑定旧 aggregate
> digest `69771d8d…`，当前冻结合同为 `d4a66a72…`。因此下文 2026-08-21 的
> “27 checks / 0 failed”不能继续作为当前 release 结论；必须用报告中列出的当前
> 镜像重新跑 PostgreSQL/FIFO/reconnect/recovery/stale-fence/audit/rollback 矩阵。
> 当前共享 Agent 显式使用 memory store、单副本，只证明非 HA 产品闭环，不能替代
> PG durable/HA 验收。

> 更新：2026-08-20（终版，所有已知 P0 修复完毕，gate 全绿）两份计划：
> - `docs/superpowers/plans/2026-08-17-agent-runtime-v2-phase1-agent-kernel.md`（Phase 1，14 任务）
> - `/Users/xiayu/kingsoft/code/agent-sdk/docs/superpowers/plans/2026-08-19-agent-kernel-interaction-web-0.3.2.md`（Interaction/Web，8 任务）

## 2026-08-21 运行态补充（以本节为准）

- `docs/superpowers/evidence/phase0/manifest.json` 已存在且 `accepted=true`。以该
  manifest 和既有 `preprod-report.json` 重跑
  `scripts/phase1_preprod_gate.py`，得到 **27 checks / 0 failed**（本次本地
  report 仅写入 `/tmp/phase1-current-gate.json`，不覆写历史证据）。因此下文中
  “17 pass / 1 fail” 与 “Phase 0 manifest 未产出”均为历史快照，不再代表当前
  gate 结论。
- 独立 Hosted UI 已由集群的 `ksadk-web 0.3.1` 滚动至
  `0.3.2-beta.3-e41710a`，镜像 digest 为
  `sha256:0b8e3e9746afbe4a76faf33f6b3d871f83943d6a90127c2a51bae9873dcff35c`。
  三个保留 Studio 样本的 `/hosted-ui/chat/` 均返回同一已验证 bundle；Web
  Interaction E2E 的 10 个场景通过，包括“审批托盘位于输入框上方且不遮挡”。
- **不得把上面的 Hosted UI 与 Studio 样本状态混为 Kernel 端到端证据。** 现网
  Server 的部署组合根未注入 `AGENT_KERNEL_DEPLOYMENT_ENABLED` 与其配套的
  signing/JWKS/capability 配置；因此当前三个声明式 Studio Agent 的 `RUNNING`
  只证明既有 CreateAgent/ManagedRuntime 生命周期。它们不会获得 AgentKernel
  projection，也不能用于证明 Gateway → Server admission → Runtime Kernel
  的真实链路。开启该组合根前必须完成受控的签名 Secret、runtime capability
  pin、JWKS 可达性和 AgentInstance migration 验收；不能以空配置降级开启。
- 当前仍缺一次私有 Dashboard 的真实浏览器会话验证（新建短期访问链接和发送
  测试消息会产生外部会话记录，需操作人确认）。

## 一、交付范围与状态

### Phase 1（Task 0-13）：全部完成，预发 gate 17 pass / 1 fail（phase0_baseline，Phase 0 manifest 未产出，硬前置如实红灯）
### Interaction/Web 0.3.2（IT-1~8）：全部完成，含真实 Codex 预发闭环（一项 PARTIAL）

## 二、六仓 commit（feat/agent-kernel-phase1，均未 push）

| 仓库 | worktree | tip | 说明 |
|---|---|---|---|
| ksadk-python | .worktrees/phase1-agent-kernel | `1dee5af3`（38 commits） | 合同冻结→kernel store×3→facade/worker/recovery→入口收敛→interaction ledger/provider→hosted bootstrap→codex 0.147→真实闭环修复 |
| agentengine-server | .worktrees/phase1-agentengine-server | `827136d` | admission 决策对象/SubmitInteraction/internal 目录/JWKS/audit/权威 readiness |
| agentengine-gateway | .worktrees/phase1-agentengine-gateway | `4921aba` | 路由收敛到 Server（零直连 runtime）/防伪造 header/SSE cursor |
| agent-runtime-service | .worktrees/phase1-agent-runtime-service | `59178fd` | agentKernel CR 投射 |
| agent-platform-operator | .worktrees/phase1-agent-platform-operator | `260b353` | CRD/SecretKeyRef DSN/env 注入/observed readiness |
| ksadk-web | .worktrees/web-0.3.2（feat/interaction-v1-web-0.3.2） | `d2bacee` | 0.3.2-beta.1：Interaction 统一消费/三源归一/A2UI fallback（203 单测+10 e2e，未发布 npm） |
| agentengine-images | agentengine/agentengine-images（master） | `fe7d42f` | codex 0.147.0 镜像 + kernel 装配 + contract test |

Contract digest：`d4a66a7249e10375d32d6a83434fde1d16ee6721e3a09ea03ed71217ee742d62`（含 Interaction/v1；additive-only gate 全绿）

## 三、预发验证（ns agent-kernel-phase1，全 digest-pinned）

### 协议层（EchoAdapter canary）：17 项 pass
digest 三端一致 / runtime+control 事件流 / 重连 / 100 FIFO / 幂等双路径 / queue_full / capability typed / permit 负向（伪造/过期/越权/nonce 重放全拒+留痕）/ 冷恢复双态 / split-brain（token+1 旧 owner 拒写）/ 回滚兼容 / audit 三方 command_id 闭环 / 六仓版本。

### 真实 Codex 闭环（agent-runtime-codex:0.147.0-kernel-phase1-805bcc5，digest sha256:c7e94f7a…）
- a 真实模型：**PASS**（gateway→server permit→inbox→CodexRuntimeAdapter→model_proxy→kspmas glm-5.3；71 条 runtime/v2 真实事件，seq 1 accepted → seq 71 run.completed）
- b 审批：**PARTIAL**（控制链路全通+ledger resolve 落库；glm-5.3 经 chat 转换不主动调工具，live approve→续跑未演示）
- c Pod kill：**PASS 有缺口**（消息零丢失；孤儿 run 卡 session 需人工 interrupted）
- d 真实 capability：**PASS**（codex native cancel/pause/resume/submit/checkpoint，attach unavailable）

## 四、前次 review 阻塞项处理结果（2026-08-20 全部关闭）

1. ✅ 孤儿 run 收口：`ee970222`（durable interrupted + degraded 降级）
2. ✅ readiness 翻转：`bb2fe85c`+`8891ebd4`+`663e5363`+`0bdbb62e`（独立心跳任务续约 + typed store 降级读 + interaction guard session 级 scope，共挖出 4 个关联 bug）；预发实测审批挂起 75s（>2×TTL）全程 ready，approve 后 70s 仍 ready，新 pod 即起即 ready
3. ✅ Web receipt 终态真相：`e8fddf7`（receipt→resolving，终态唯一来自 interaction.resolved/cancelled/expired 事件；rejected→failed 可重试）
4. ✅ Task 8：Hosted UI 0.3.2-beta.1（`1f5434a`+镜像 v0.3.2-beta.1-7c523f0）、interaction-v1-e2e.json（approve/reject/replay 三场景全链）、gate 刷新、Operator 定向绿、SSE 断开感知修复（`de865a68`，304s→8s）
5. ✅ 真实审批闭环：real-interaction-closure.json（真实 SDK transport，call_id 原样回包，seq6 requested→seq16 resolved→seq22 completed）
6. ✅ Phase 0 manifest：`ae520de5`（依据：0.8.1 release 计划 Task 10 交付于 ff6247b3、release gate 8 tests 复核绿、0.8.1 已公开上线）
7. ✅ 合同不一致阻流演练：contract-mismatch-drill.json（`986f7a3b`，双路径注入均 409 阻流+审计留痕+恢复）
8. ✅ gate 终态：**pass，27 checks / 0 failed**

## 四-b、已知遗留（非阻塞，如实列出）

- [P1] glm-5.3 经 model_proxy 转换不触发工具调用（真实审批演示用 fake app-server；chat 接口带 tools 有 tool_calls，缺口在 codex→proxy 工具面）
- [P1] Server 未实现 SubmitAgentControl/GetAgentStatus/SubscribeSessionEvents 三个公共 Action 的完整 HTTP 面（gateway 已按此转发）
- [P1] `agent_kernel_contract_mismatch_total` 指标在 canary 不可得（以请求级 409+审计行为证据）
- [P1] server chart 无 extraEnv（canary env 为 kubectl patch）；同 tag 重推需按 digest 拉镜像；uv build 复用 stale build/ 需先删
- Studio 一键部署 Codex/LangGraph 上云不在计划内（无 Bundle admission API）；多副本未开；Codex workspace 持久化（PVC/KS3FS）未做
- 六仓分支均未 push（等 review 通过 + 用户批准）
- Web 0.3.2 正式 npm 发布未执行（等用户批准，走 Trusted Publishing）

## 五、原第四节（历史遗留问题存档）

1. **[P0] 孤儿 run takeover 不确定性收口**：Pod kill 后 open run 卡 session，RecoveryCoordinator 吞异常（真实代码 bug，非环境）
2. **[P0] readiness 翻转**：完成 run 的 session 留 heartbeat 集合不续约，TTL 后误报 not-ready（预发多次 503 的元凶）
3. [P1] gateway SSE 在终止事件前提前断流（事件在库，重连可补）
4. [P1] codex 非 proxy 模式不注入自定义 OPENAI_API_BASE provider（必须 KSADK_CODEX_USE_PROXY=1）
5. [P1] Server 尚未实现 SubmitAgentControl/GetAgentStatus/SubscribeSessionEvents 三个公共 Action（Gateway 已按此转发，读路径后端缺）
6. Phase 0 manifest 未产出（gate 最后一盏红灯，待 Release Owner 决策）
7. server chart 无 extraEnv，canary env 是 kubectl patch（helm upgrade 会抹掉）
8. Studio 一键部署 Codex/LangGraph 上云 **不在本计划范围**（无 Server Bundle admission/deployment API），当前真实闭环是 DB 行+手工 deployment
9. 多副本：协议层支持（租约+fencing 已验证），未开；Codex workspace 持久化（PVC/KS3FS）未做

## 五、Review 边界确认（前次 review 要求，已保持）

- hosted Runtime 只信 Server JWKS（AUTHORITY_MODE=local 仅开发）
- PG DSN 只走 SecretKeyRef，不进 env value
- Gateway 只转 Server，零直连 Runtime
- evidence 全部带可追溯标识（command_id/event_id/digest），gate 对缺失/无标识 evidence 判 fail
- 无 DSN 时 PG 测试 skip ≠ pass（CI/预发必须提供 DSN）

## 六、证据目录

`docs/superpowers/evidence/phase1/`（ksadk worktree）：baseline.json、preprod/（step456/step789/v2-e2e/v2-drill/v2-audit/v2-versions/real-codex-closure.json）、preprod-report.json、rollback-report.json、canary/（部署材料，含密码明文待脱敏决策）
