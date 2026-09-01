## Context

当前 Harness 已存在 Agent Loop、Context/Memory、Skill/MCP、Sandbox、Draft Runtime、生命周期、多 Agent、RuntimeAdapter 及多类离线/真实环境矩阵。问题不是缺少统一“大框架”，而是能力合同分散、少数故障路径与外部后端尚未形成一致门禁。详见 [proposal.md](./proposal.md)。本仓只负责 SDK、CLI 与数据面 Runtime；云端控制面、Skill 治理和远程 Sandbox 生命周期仍由对应服务负责。

## Goals / Non-Goals

**Goals:**

- 以现有实现为基础，用正式行为规格、统一失败语义和 Conformance 收口，而不是重写 Harness。
- 让默认 LangGraph 托管路径形成最完整的可控能力，同时允许外部 Runtime 诚实声明差异。
- 将合成测试、真实模型、真实 MCP、真实 Sandbox 和真实生命周期证据汇总为机器可读的完成判定。
- 保持失败可恢复、降级可见、缺少环境不伪造通过。

**Non-Goals:**

- 不建设 Studio 的 Trace 查询、图表、就绪度页面或交互产品；仅保留运行和验证必需的 RuntimeEvent 数据。
- 不实现 Registry、审批控制面、Skill Marketplace、远程 Sandbox 控制面或云端路由服务。
- 不在本变更发布版本、调整公开版本号或兼容所有第三方 Agent 产品的私有语义。

## Decisions

### 1. 采用“合同 + Conformance + 证据”收口，不另起一套 Harness

现有模块继续作为实现主体，每个领域先固化可观察合同，再补齐失败路径与真实环境矩阵。统一收口层只消费各矩阵的结构化结果，给出 `ready`、`warning`、`blocked`、`not_configured`，不复制领域逻辑。

备选方案是新建一个大一统运行内核后迁移全部模块。该方案会扩大回归面，并丢失已经验证的 LangGraph、MCP、Memory 与 Sandbox 行为，因此不采用。

### 2. 默认深度接管，外部 Runtime 通过适配器诚实兼容

KsADK Harness + LangGraph 作为默认托管引擎，负责状态、Checkpoint、Context、Memory、工具、安全与恢复。ADK、Codex 或后续 DSH 类 Loop 通过 RuntimeAdapter 接入，提交 Capability Declaration，并只承诺适用且可验证的行为。

备选方案是强制所有 Runtime 模拟默认引擎的全部内部事件。对于黑盒或自带状态机的 Runtime，这会产生伪造能力和脆弱适配，因此不采用。

### 3. 使用统一终态和错误分类贯穿领域边界

运行终态限定为 completed、failed、cancelled、awaiting_approval、interrupted。错误采用稳定类别并保留领域原因，例如 provider/context_length、mcp/transport_unavailable、sandbox/lease_lost、orchestration/validation_failed。重试策略只基于类别和幂等性，而非字符串猜测。

旧异常继续可作为 cause 保留，但公共事件、矩阵和恢复逻辑不得依赖 Provider 私有错误文本。

### 4. Context、Memory、工具结果与 Artifact 使用同一证据链

ContextManifest 记录计划、投影和实际用量；CompactionRecord 记录压缩、重注入和降级；MemoryRecord 记录作用域、来源、策略与替代关系；大型或敏感工具结果通过 Artifact 引用进入上下文。正文默认不写入运行事件，只保存 Hash、尺寸、类型、引用与必要摘要。

这样可以在不建设 Studio 查询层的前提下，支持恢复、质量门禁、审计和成本归因。

### 5. Skill 与 MCP 共享渐进披露模型，但保留各自语义

两者统一使用 L0→L1→L2→L3 与披露游标，减少首轮 Token 并结构性阻止越级。Skill 的 L2 是指令、L3 是附属资源；MCP 的 L2 是 Tool Schema、L3 是执行。MCP 额外承担健康、熔断、鉴权轮换、审批和 Schema 漂移。

不把两者抽象成完全相同的数据对象，以免抹平安全与生命周期差异。

### 6. Sandbox 以 Backend 合同和 Fence 保持可替换性

上层只依赖 SandboxBackend 的能力声明、租约、Fence、Journal、取消、恢复与 Artifact 合同。Subprocess 是始终可跑的基线；E2B、KOP/私有后端使用相同 Conformance，缺少真实环境时明确为 not_configured。

业务逻辑不得引用特定后端对象，也不得以模拟实现替代真实后端通过证明。

### 7. Draft 与正式生命周期复用编译器但隔离事实源

DraftRuntime 复用正式 Revision 编译合同，使“保存即可试用”的结果可迁移；但 Draft 使用独立命名空间，不生成 BuildManifest、不注册 Route。正式路径保持不可变 Revision→Build→Approval→Deploy→Activate，回滚只切换到已验证历史 Deployment。

### 8. 多 Agent 使用父子运行图，不共享可变 Checkpoint

每个子 Agent 有独立 Run、Checkpoint、预算、取消域和 Artifact 集。父节点只通过结构化结果与 Receipt 合并事实；调度器根据依赖图和并发预算启动节点。恢复时跳过已有成功 Receipt 的节点，避免重复副作用。

### 9. 完成判定分层且不允许“跳过即通过”

每项能力输出离线合同测试、真实环境矩阵和缺失条件。核心离线回归必须通过；与当前发布目标相关的真实验证失败即 blocked；非目标外部后端未配置为 not_configured。发布目标和能力声明共同决定哪些 `not_configured` 会升级为 blocked。

## Verification Strategy

1. 为每个规格场景建立或映射确定性单元/集成测试。
2. 运行 Harness、Memory 与架构守卫回归，确认历史行为不退化。
3. 使用受环境变量门控的真实模型矩阵验证流式、工具、Usage、溢出与故障分类。
4. 使用真实 MCP SDK 服务验证发现、调用、Schema 重启漂移与断连恢复。
5. 对 Subprocess 始终运行 Sandbox Conformance；对配置好的远程后端运行同一套件。
6. 用真实本地进程验证正式生命周期激活与回滚；跨服务部分记录所需接口和阻断条件。
7. 运行定向 Secret Scan，确保报告、Fixture 和事件不含凭证或敏感正文。

## Migration Plan

1. 先冻结现有行为基线和测试映射，不改变默认公共入口。
2. 以 additive 字段扩展错误、能力声明和矩阵报告；读取端对未知字段保持兼容。
3. 分领域补齐合同和实现，每一阶段均运行局部测试与全量回归。
4. 最后启用汇总门禁；初期以报告模式运行，再对默认托管目标启用阻断。
5. 如出现回归，关闭新门禁并回退该领域增量；持久化数据只使用可向后兼容的 additive 迁移。

## Risks / Trade-offs

- [统一错误分类可能丢失 Provider 细节] → 保留原始异常为脱敏 cause，同时仅以稳定类别驱动策略。
- [真实矩阵受限流或外部服务波动影响] → 使用有界重试、记录环境元数据，并区分产品失败与环境不可用。
- [Conformance 过宽导致第三方 Runtime 难以接入] → 依据能力声明计算适用项，但安全和租户隔离项不可豁免。
- [多 Agent 恢复与副作用去重复杂] → 以独立 Checkpoint、幂等 Receipt 和 Fence 作为合并前提，禁止隐式共享状态。
- [事件证据增加存储量] → 正文外置，只记录 metadata、Hash 和聚合指标，并设置保留策略。
- [范围较大导致长期分支漂移] → 按任务分阶段、小提交、每阶段 fresh verification，不混入 Studio 或控制面工作。
