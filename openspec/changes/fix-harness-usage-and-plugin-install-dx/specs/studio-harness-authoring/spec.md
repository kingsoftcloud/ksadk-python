# Studio Harness Runtime 授权体验

## Purpose

定义在 Studio 中创建/编辑使用 Harness Runtime 的 Agent 时，DSH harness provider 所需权限（`process:host-user`）的授予体验：模板默认创建即开箱可运行，显式 spec 的授权语义保持不变，权限不足的失败必须可自助修复。

## ADDED Requirements

### Requirement: 模板默认创建必须预置 provider 必需权限

当创建 Agent 未显式提供 spec（使用默认模板）且 Runtime 为 harness 时，生成的 spec SHALL 包含 DSH harness provider 声明的必需权限（`process:host-user`）。

#### Scenario: 模板默认创建 Harness Agent

- **WHEN** 调用方以 runtime type=harness 且不提供 spec 创建 Agent
- **THEN** 生成 spec 的 `security.allowedPermissions` 包含 `process:host-user`，构建与运行前检查通过

#### Scenario: 非 Harness Runtime 不受影响

- **WHEN** 调用方以 runtime type=codex/adk/langgraph 且不提供 spec 创建 Agent
- **THEN** 生成 spec 的 `security.allowedPermissions` 不新增 `process:host-user`

### Requirement: 显式 spec 的授权语义不得被改写

调用方显式提供 spec 时，系统 MUST 原样保留其 `security.allowedPermissions`；未授予必需权限的 Agent 在运行前检查失败，但错误必须携带可执行指引。

#### Scenario: 显式拒绝必须被拒但可自助修复

- **WHEN** 调用方显式提供不含 `process:host-user` 的 spec 并运行 harness Agent
- **THEN** 运行前检查以权限拒绝失败，且错误信息包含需要授予的权限名称与修复途径（在 Agent 安全设置中允许后重新构建）
