## Purpose

定义长任务上下文与长期记忆的预算、压缩、重建、召回、纠错和降级边界，使关键目标与事实跨轮次保留，同时避免错误或越权记忆污染运行。

## ADDED Requirements

### Requirement: Context 构建必须受预算和信任边界约束
Harness SHALL 为指令、最新输入、历史、记忆、附件和工具结果分配显式预算，并禁止低信任内容进入高信任指令层。

#### Scenario: 输入超过上下文预算
- **WHEN** 候选上下文总量超过可用模型窗口
- **THEN** Harness 按优先级压缩或外置内容并生成可审计的上下文清单

### Requirement: 压缩必须保留任务连续性
Harness MUST 在压缩后保留当前目标、用户最新纠正、未完成计划、关键约束和完整工具调用关系；无法可靠保留的关键事实 SHALL 被重注入或阻断质量门禁。

#### Scenario: 摘要遗漏关键约束
- **WHEN** 压缩摘要未包含已标记的关键约束
- **THEN** Harness 重注入该约束并按最终模型输入计算保留率

#### Scenario: 摘要模型失败
- **WHEN** 压缩模型不可用且系统使用降级摘要
- **THEN** Harness 继续可用并产生明确的降级事件与原因

### Requirement: Working Context 必须可重建且冲突可见
Harness SHALL 能从持久化 Transcript、Checkpoint 和确定性 Patch 重建工作上下文，并 MUST 拒绝静默覆盖版本冲突。

#### Scenario: 乐观版本冲突
- **WHEN** 两个写入基于同一旧版本更新 Working Context
- **THEN** 后到写入收到冲突结果且原状态不被静默覆盖

### Requirement: 长期记忆必须可治理
Harness MUST 对记忆执行租户、用户和 Agent 作用域过滤，支持纠正、遗忘、锁定、TTL、来源和审计，并且自动写入不得覆盖锁定事实。

#### Scenario: 用户纠正已有偏好
- **WHEN** 用户明确纠正已保存偏好
- **THEN** Harness 以可追溯更新替代旧事实并在后续召回中使用新值

#### Scenario: 记忆服务不可用
- **WHEN** 记忆读取或写入服务失败
- **THEN** 主对话继续运行，记忆能力显式降级且不跨作用域回退

### Requirement: Memory 质量必须通过可重复评测证明
Harness SHALL 提供带金标的数据集与指标，至少衡量写入精确率、有效事实召回率、压缩保真度和证据保真度，并区分合成与真实模型结果。

#### Scenario: 真实模型评测条件缺失
- **WHEN** 未配置真实模型凭证或端点
- **THEN** 评测结果标记为 not_configured 或 skipped，并不得计为通过
