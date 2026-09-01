## Purpose

定义 KsADK 默认托管引擎与 ADK、Codex 等外部 Runtime 的统一接入边界，通过能力声明和一致性验证支持多 Runtime，而不虚构平台无法控制的行为。

## ADDED Requirements

### Requirement: RuntimeAdapter 必须声明真实能力
每个 Adapter SHALL 声明其对指令、历史、压缩、记忆、工具、流式、审批、Checkpoint、恢复和事件的支持等级及责任方，声明 MUST 随运行证据保存。

#### Scenario: 外部 Runtime 不支持平台 Checkpoint
- **WHEN** Adapter 声明恢复由外部 Runtime 负责
- **THEN** Harness 不承诺平台级恢复，并在调用前与结果中暴露该能力差异

### Requirement: 默认托管路径必须提供完整合同
KsADK Harness 的默认 LangGraph 托管实现 MUST 满足本变更中 Agent Loop、Context/Memory、Capability、Sandbox、生命周期和多 Agent 的适用要求。

#### Scenario: 使用默认 Runtime
- **WHEN** Agent 未显式选择外部 Runtime
- **THEN** Harness 使用默认托管引擎并启用其声明的完整治理能力

### Requirement: 外部 Runtime 必须通过适用 Conformance
Adapter SHALL 运行统一一致性套件，并按 capability declaration 计算适用用例；安全隔离、工具配对和租户边界等强制用例不得被声明规避。

#### Scenario: Adapter 缺失强制安全行为
- **WHEN** 外部 Runtime 无法保证工具审批或租户隔离
- **THEN** Conformance 将其标记 blocked，且不得作为满足该策略的部署目标

### Requirement: 事件转换必须保持关联与事实语义
Adapter MUST 将可观测运行事实转换为稳定 RuntimeEvent，保留 Run、父子调用、工具调用、使用量和终态关联；无法提供的字段 SHALL 明确标记 unavailable 而非伪造。

#### Scenario: Provider 不返回 Token Usage
- **WHEN** 外部 Runtime 的模型调用不提供使用量
- **THEN** 事件将 usage 标记 unavailable，且发布就绪度不得将其误报为零消耗

### Requirement: 多 Provider 兼容矩阵必须覆盖关键协议差异
Harness SHALL 对已配置 Provider 验证非流式、流式、工具调用分片、Usage、溢出和错误分类，并将模型行为漂移与 Harness 缺陷分开报告。

#### Scenario: 模型只在流式模式调用工具
- **WHEN** 同一模型在非流式测试未产生预期 Tool Call、但流式测试成功
- **THEN** 矩阵分别记录两种结果并标注为模型或 Provider 行为差异
