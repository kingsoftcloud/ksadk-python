## Purpose

定义 Skill 与 MCP 在默认 Agent Loop 中按需发现、分层披露、安全执行和故障恢复的统一合同，降低首轮上下文成本并保持企业工具接入可控。

## ADDED Requirements

### Requirement: 能力必须渐进披露
Harness SHALL 默认仅提供 Skill 或 MCP 的 L0 目录，并要求模型按 L1 元数据、L2 指令或 Schema、L3 资源或执行的顺序访问。

#### Scenario: 模型完整加载 Skill
- **WHEN** 模型需要一个已绑定 Skill 的附属资源
- **THEN** Harness 允许 L0→L1→L2→L3 顺序披露并记录各层元数据事件

#### Scenario: 模型越级调用 MCP Tool
- **WHEN** 模型未读取目标 Tool Schema 即尝试执行
- **THEN** Harness 拒绝调用并返回可自纠的披露错误

### Requirement: 绑定、权限与风险策略必须在执行前生效
Harness MUST 拒绝未绑定能力，对高风险工具要求审批，并保证审计信息不暴露凭证或敏感结果正文。

#### Scenario: 高风险 MCP Tool
- **WHEN** 模型请求执行 high 或 critical 风险工具
- **THEN** Harness 在执行前进入 awaiting_approval 并携带脱敏目标和参数摘要

### Requirement: MCP 运行必须处理鉴权与服务漂移
Harness SHALL 支持凭证引用和轮换后的重新连接，识别 Schema 签名变化，并对超时、断连、熔断和恢复给出稳定分类。

#### Scenario: 服务重启后 Schema 变化
- **WHEN** MCP Server 重连后返回与缓存不同的工具 Schema
- **THEN** Harness 失效旧缓存、更新签名并要求按新 Schema 重新披露

#### Scenario: MCP 服务不可用
- **WHEN** Server 超时或断连
- **THEN** Harness 返回可分类工具错误、更新健康状态且不使 Agent Loop 悬挂

### Requirement: 大型或敏感工具结果必须受控外置
Harness MUST 将超过阈值或命中敏感策略的结果写入 Artifact Store，并只向上下文返回受限摘要、引用、Hash、类型与大小；无法安全外置时不得静默回填正文。

#### Scenario: 返回大型 JSON
- **WHEN** MCP Tool 返回超过配置阈值的 JSON
- **THEN** Harness 创建 Artifact 并在模型上下文中只保留安全摘要和引用

### Requirement: 真实兼容矩阵必须诚实报告
Harness SHALL 对代表性 Skill 包和 MCP Transport 验证发现、Schema、调用、审批、缓存、恢复与鉴权；缺少外部条件的项目 MUST 标记 not_configured。

#### Scenario: 无可轮换企业凭证
- **WHEN** 环境无法提供真实凭证轮换服务
- **THEN** 矩阵保留离线故障注入结果并将真实轮换项标为 not_configured
