## Purpose

定义不同 Sandbox Backend 在隔离执行、租约、取消、恢复和产物处理上的共同最低合同，使后端可以替换而不改变上层 Agent 的安全语义。

## ADDED Requirements

### Requirement: Sandbox Backend 必须声明能力
每个 Backend SHALL 提供机器可读的能力声明，覆盖执行、文件、网络、租约、取消、恢复、Artifact 和限制；上层 MUST 不调用未声明能力。

#### Scenario: Backend 不支持网络控制
- **WHEN** 任务要求网络策略而 Backend 未声明支持
- **THEN** Harness 在执行前返回 capability_unsupported，而不是无隔离运行

### Requirement: 租约与 Fence 必须阻止过期执行者写入
Sandbox Runtime MUST 使用租约和单调 Fence 保护状态及 Artifact 写入，并拒绝过期或已被接管的执行者提交结果。

#### Scenario: 租约过期后旧进程返回
- **WHEN** 新执行者已接管任务且旧执行者随后提交结果
- **THEN** Runtime 拒绝旧 Fence 的写入并保留新执行者状态

### Requirement: 取消、重连与恢复必须有一致语义
Backend SHALL 在能力允许时传播取消并支持从 Journal 或 Checkpoint 恢复；无法保证恢复时 MUST 返回明确的不可恢复状态。

#### Scenario: 执行期间连接中断
- **WHEN** 客户端与远程 Sandbox 暂时断开
- **THEN** Runtime 重连并查询确定状态，或以明确分类结束而不重复执行副作用

### Requirement: Sandbox 输出必须安全落盘
Sandbox 产生的文件和大型输出 MUST 通过 Artifact 合同返回，路径须被约束在允许根目录，日志与事件不得包含 Secret 正文。

#### Scenario: 任务尝试写出工作目录
- **WHEN** Sandbox 任务请求写入未授权路径
- **THEN** Backend 拒绝操作并记录脱敏的策略违规

### Requirement: Backend Conformance 必须覆盖真实实现
Harness SHALL 对本地 Subprocess 执行完整 Conformance，并对已配置的 E2B、KOP 或私有 Backend 运行同一合同；未配置项 MUST 明确报告 not_configured。

#### Scenario: E2B 未配置模板
- **WHEN** 缺少 E2B 模板标识或凭证
- **THEN** 报告将 E2B 标记为 not_configured 且不计入通过率
