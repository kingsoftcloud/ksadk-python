## 1. 基线与完成口径

- [x] 1.1 建立七项能力规格到现有模块、测试和真实矩阵的映射表，并通过逐项链接检查确认每个 Requirement/Scenario 均有负责人或明确缺口
- [x] 1.2 固化当前 Harness、Memory、Architecture 测试基线，使用项目 `uv run` 环境运行并记录通过、跳过和失败原因
- [x] 1.3 定义统一终态、错误分类及 `ready`/`warning`/`blocked`/`not_configured` 报告 Schema，并用序列化兼容测试验证旧读取端可忽略新增字段

## 2. Agent Loop 可靠性

- [x] 2.1 审计并补齐非流式与流式 Tool Call 的分片重组、调用配对和单次执行保护，以相关 Provider/Reasoner 单测验证非法与跨分片参数
- [x] 2.2 收口 Provider 限流、鉴权、上下文溢出、无效请求和瞬时故障分类及有界重试，以故障注入测试验证重试只发生在允许类别
- [x] 2.3 补齐取消、中断、审批恢复和副作用去重路径，以 SQLite Checkpoint 跨进程恢复测试验证已完成工具不会重复执行
- [x] 2.4 运行已配置 Provider 的真实兼容矩阵，验证非流式、流式、Tool Call、Usage 与 Overflow，并输出机器可读结果或明确 `not_configured`

## 3. Context 与 Memory 治理

- [x] 3.1 完成 Transcript/Checkpoint/WorkingContextPatch 的确定性重建器，以重放一致性和乐观版本冲突测试验证不静默覆盖
- [x] 3.2 收口后台压缩、关键事实重注入、工具配对和降级事件，以跨压缩长任务测试验证目标、纠正、约束及 Tool Pair 保留
- [x] 3.3 审计 Memory 作用域、纠正、遗忘、锁定、TTL、来源和故障降级，以跨租户拒绝及服务不可用测试验证主对话继续可用
- [x] 3.4 补齐语义召回、融合排序与可插拔 Reranker 的稳定合同，以固定金标集验证排序确定性和有效事实召回
- [x] 3.5 扩展真实模型 Context/Memory 评测样本并运行质量门禁，报告写入精确率、召回率、压缩保真度和证据保真度，缺少凭证时标记 `not_configured`

## 4. Skill 与 MCP 运行韧性

- [x] 4.1 审计 Skill 和 MCP 的 L0→L1→L2→L3 游标、越级拒绝及 Checkpoint 恢复，以默认 Agent Loop E2E 验证序列和子 Agent 透传
- [x] 4.2 收口 MCP 鉴权引用、凭证轮换、Schema 签名漂移、缓存失效和重连语义，以离线故障注入和真实 SDK 服务重启测试验证
- [x] 4.3 验证动态风险审批、健康状态、超时分类、熔断和恢复，以高低风险混合及断连恢复 E2E 确认低风险直通、高风险中断
- [x] 4.4 验证大型或敏感 Tool 结果的 Artifact Offload、配额和失败降级，以大 JSON、敏感命中、无 Store、超配额和写入失败测试确认正文不回填 Context
- [x] 4.5 扩展代表性 Skill/MCP 真实兼容矩阵并生成结构化结果，确保未授权/越级为零且未配置的企业鉴权项不计为通过

## 5. Sandbox Backend Conformance

- [x] 5.1 固化 SandboxBackend 能力声明及 capability_unsupported 行为，以契约测试验证上层不会调用未声明能力
- [x] 5.2 收口租约、Fence、Journal、取消、重连与恢复，以过期执行者、断连和副作用去重故障注入测试验证
- [x] 5.3 验证路径隔离、网络策略、Artifact 返回和日志脱敏，以越权路径、Secret 输出和大型文件测试确认安全边界
- [x] 5.4 对 Subprocess 运行完整真实 Conformance，并对已配置 E2B、KOP/私有后端运行同一矩阵；未配置后端输出 `not_configured`

## 6. Draft 与正式生命周期

- [x] 6.1 验证 DraftRuntime 与正式编译合同等价，同时 Draft 会话不创建 Build、Route 或正式部署记录，以隔离集成测试证明
- [x] 6.2 收口 Revision 不可变、Build 证据、审批、Deployment、激活 Route 和版本调用关系，以状态迁移测试拒绝非法跳转
- [x] 6.3 使用真实本地 Runtime 进程运行 Build→Deploy→Approve→Activate→Invoke→Rollback E2E，确认回滚后的调用命中历史版本
- [x] 6.4 记录云端控制面联调所需字段、鉴权、失败语义和未满足条件，以跨仓契约检查确认本仓未吸收控制面职责

## 7. 多 Agent 编排

- [x] 7.1 为子 Agent 固化独立 Run、Checkpoint、预算、取消域和父子事件关联，以并行子任务测试验证状态不互相覆盖
- [x] 7.2 收口依赖图调度、并发上限及 Token/时间/工具/Artifact 预算分配，以依赖和预算耗尽测试验证未授权工作不会启动
- [x] 7.3 实现并验证 fail-fast、部分成功、可重试失败和父取消传播策略，以多分支故障注入确认待执行节点正确取消或保留
- [x] 7.4 固化子 Agent 结构化结果、Artifact 和 Receipt 验收，以 Schema 失败及聚合测试验证无效结果不会进入父上下文
- [x] 7.5 完成跨进程多 Agent 恢复 E2E，确认成功节点不重跑、未完成节点按策略恢复且副作用不重复

## 8. 多 Runtime 适配与一致性

- [x] 8.1 固化 RuntimeAdapter Capability Declaration，覆盖指令、历史、压缩、记忆、工具、流式、审批、Checkpoint、恢复和事件，以 Schema 测试验证必填项及责任方
- [x] 8.2 验证默认 LangGraph 托管引擎满足全部适用规格，并以联合 E2E 输出其完整能力声明和证据引用
- [x] 8.3 为已有 ADK、Codex/DSH 类适配路径运行适用 Conformance，确保不可观测字段标记 unavailable、强制安全用例不能被规避
- [x] 8.4 验证 RuntimeEvent 转换保留 Run、父子、Tool Call、Usage 和终态关联，以事件配对测试确认缺失数据不伪造为零值

## 9. 汇总门禁与交付收口

- [x] 9.1 实现只消费领域矩阵结果的 Harness 就绪度汇总器，以 Fixture 测试验证发布目标会把必要的 `not_configured` 升级为 blocked
- [x] 9.2 运行 Harness、Memory、Architecture 全量回归及所有可用真实环境 E2E，保存 fresh 报告并逐项对照七份规格
- [x] 9.3 对事件、报告、Fixture、文档和改动文件执行定向 Secret Scan，确认不含真实 Key、内网凭证、临时 URL 或敏感正文
- [x] 9.4 更新 Harness 正式技术文档，说明默认托管路径、外部 Runtime 边界、降级语义、验证命令和已知 `not_configured` 项，并通过文档链接检查
- [x] 9.5 完成代码评审与完成度审计，确认第 7 项 Studio 展示/查询未混入本变更，所有未完成外部条件均被明确记录而非宣称通过
