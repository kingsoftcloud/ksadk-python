# DSH 插件源安装与工具链诊断体验

## Purpose

定义 `agentengine plugin` 对 DSH 本地插件源的解析确定性与工具链故障的可诊断性：本地源路径的解析不得依赖 dsh CLI 的隐式工作目录；工具链版本不匹配或未安装时，CLI 报错必须给出用户可执行的修复指引。

## ADDED Requirements

### Requirement: 本地源路径解析必须确定

通过 CLI 安装 DSH 插件时，凡相对当前工作目录可解析且真实存在的本地源（目录或 `.tgz`），SHALL 按当前工作目录解析为绝对路径后再交给 dsh CLI，与 dsh 的内部 cwd 无关。

#### Scenario: 相对路径指向真实目录

- **WHEN** 用户在仓库根目录执行 `plugin install <相对目录路径>` 且该目录存在
- **THEN** 安装成功，Profile 依赖指向该目录的真实包名，且不出现 `link:` 死链

#### Scenario: 绝对路径行为不变

- **WHEN** 用户传入存在的绝对路径源
- **THEN** 行为与修复前一致（打包/安装成功）

### Requirement: 不存在的本地路径源必须显式报错

形如本地路径但不存在的 `.tgz` 源 SHALL 被拒绝并报"源不存在"类错误，不得静默交给 dsh 生成死链依赖。

#### Scenario: 传入不存在的 tgz 路径

- **WHEN** 用户执行 `plugin install ./dist/bundle.tgz` 且该文件不存在
- **THEN** CLI 报源无效/不存在错误，Profile 清单与 node_modules 不被改动

### Requirement: 工具链版本不匹配必须给出可执行指引

DSH 工具链或 pnpm 版本不匹配时，CLI 错误 SHALL 至少包含：期望版本、实际版本、以及两种修复路径（安装/启用 corepack，或用 `AGENTENGINE_PNPM_BIN` 指向固定版本 pnpm）。

#### Scenario: 全局 pnpm 版本不一致

- **WHEN** 环境无 corepack 且全局 pnpm 版本不等于固定支持版本
- **THEN** `plugin toolchain install` 的报错包含期望版本、实际版本与 `AGENTENGINE_PNPM_BIN` 指引

### Requirement: 工具链未安装必须提示安装命令

DSH 工具链未安装时，依赖工具链的 CLI 操作 SHALL 提示运行 `agentengine plugin toolchain install`。

#### Scenario: 未安装即调用插件操作

- **WHEN** 工具链未安装时执行 `plugin install`
- **THEN** 报错包含 `agentengine plugin toolchain install`
