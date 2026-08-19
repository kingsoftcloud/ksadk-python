# Phase 1 Agent Kernel + Interaction/Web 0.3.2 — Review 交接记录

> 交接时间：2026-08-20。分支均未 push。两份计划：
> - `docs/superpowers/plans/2026-08-17-agent-runtime-v2-phase1-agent-kernel.md`（Phase 1，14 任务）
> - `/Users/xiayu/kingsoft/code/agent-sdk/docs/superpowers/plans/2026-08-19-agent-kernel-interaction-web-0.3.2.md`（Interaction/Web，8 任务）

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

## 四、已知未修问题（review 重点）

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
