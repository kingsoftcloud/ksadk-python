## Purpose

定义默认 Agent Loop 在模型、工具、流式输出、取消、恢复和故障场景下可依赖的运行行为，使同一请求能够安全结束、恢复或给出可分类的失败结论。

## ADDED Requirements

### Requirement: Agent Loop 状态机必须确定性收敛
Harness SHALL 将每次运行推进到 completed、failed、cancelled、awaiting_approval 或 interrupted 之一，并禁止在无活动模型或工具调用时保持悬挂。

#### Scenario: 正常工具循环完成
- **WHEN** 模型发起一个或多个合法工具调用并最终返回答案
- **THEN** Harness 完整记录模型与工具调用配对，并将运行终止为 completed

#### Scenario: 达到循环限制
- **WHEN** 模型持续请求工具且达到配置的步数或预算上限
- **THEN** Harness 终止继续执行并返回可分类的预算或循环限制结果

### Requirement: 流式工具调用必须正确重组
Harness MUST 在执行前重组分片的工具名称、调用标识和参数，并对缺失、冲突或非法 JSON 给出结构化错误。

#### Scenario: 参数跨多个分片返回
- **WHEN** Provider 将同一工具参数拆分为多个流式增量
- **THEN** Harness 仅在参数完整且可验证后执行一次对应工具

### Requirement: 故障、重试与溢出必须分类处理
Harness SHALL 区分瞬时 Provider 故障、限流、鉴权失败、上下文溢出、无效请求与不可恢复错误，并仅对允许重试的类别按有界策略重试。

#### Scenario: 上下文超过模型限制
- **WHEN** Provider 明确返回上下文长度超限
- **THEN** Harness 触发受控压缩或返回 context_length 分类，且不得将其误判为普通无效请求

#### Scenario: 不可重试的鉴权错误
- **WHEN** Provider 返回鉴权失败
- **THEN** Harness 不进行盲目重试并返回脱敏后的 auth 分类

### Requirement: 取消、中断与恢复必须保持副作用安全
Harness MUST 持久化恢复所需状态，并确保取消或恢复不会重复执行已确认完成的有副作用工具。

#### Scenario: 审批后跨进程恢复
- **WHEN** 运行在工具审批点中断并由另一进程恢复
- **THEN** Harness 从 Checkpoint 延续且只执行一次获批工具

#### Scenario: 调用方取消运行
- **WHEN** 调用方发出取消信号
- **THEN** Harness 传播取消、停止新工作并将最终状态标记为 cancelled
