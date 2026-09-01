## Purpose

定义 Agent 从可即时试用的 Draft 到不可变正式版本的构建、审批、部署、激活、调用和回滚流程，保证调试便利性不会削弱生产发布边界。

## ADDED Requirements

### Requirement: Draft 必须保存即可试用
Harness SHALL 使用与正式构建等价的编译合同将合法 Draft 启动为隔离调试会话，无需创建 Build、Route 或部署记录。

#### Scenario: 保存合法 Draft
- **WHEN** 用户保存满足合同的 Draft 并发起试用
- **THEN** Harness 创建临时会话并允许使用其已绑定的模型、Skill 与 MCP

#### Scenario: Draft 配置非法
- **WHEN** Draft 引用格式或必填能力无效
- **THEN** Harness 在调试编译阶段返回可修复错误而不是延迟到正式 Build

### Requirement: 正式版本必须不可变且可追溯
正式路径 MUST 依次形成 Revision、Build 证据、审批结论、Deployment 与激活 Route；激活后对配置的修改 SHALL 创建新版本。

#### Scenario: 修改已激活 Agent
- **WHEN** 用户编辑一个已激活版本的配置
- **THEN** 系统创建新的 Draft 或 Revision，原激活版本保持不变

### Requirement: 发布与运行状态必须隔离
Draft 运行 MUST 不注册正式 Route、不影响线上实例，也不得被正式运行误用；正式调用 MUST 解析到唯一已激活版本。

#### Scenario: Draft 与正式版本并行测试
- **WHEN** 同一 Agent 同时存在 Draft 会话和已激活版本
- **THEN** 两者的会话、Checkpoint、事件和依赖引用互相隔离

### Requirement: 回滚必须恢复可调用的历史版本
Harness SHALL 支持将 Route 原子切回已验证的历史 Deployment，并在回滚后继续接受调用。

#### Scenario: 新版本激活后回滚
- **WHEN** 操作者回滚到上一已批准版本
- **THEN** Route 指向历史版本且后续调用实际由该版本处理

### Requirement: 生命周期 E2E 必须验证真实进程边界
Harness SHALL 以真实本地进程或已配置控制面验证 Build→Deploy→Approve→Activate→Invoke→Rollback；无法访问外部控制面时 MUST 说明未验证边界。

#### Scenario: 本地生命周期验证
- **WHEN** Conformance 启动两个本地 Runtime 版本
- **THEN** 激活与回滚后的调用分别命中预期进程和版本
