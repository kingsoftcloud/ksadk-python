# Studio 云端交付工作台设计

## 目标

让 Studio 用户在同一 React 工作台中完成并看见真实的本地构建、不可变 Bundle 上传、
云端实例启动、状态刷新、重部署和回滚；同时把已有 Agent、创建/编辑、会话、
资源、运行资源、任务编排和可观测页面统一到同一套 React 信息架构与视觉契约。
页面不是第二套控制面：它只能调用 Studio API，由 Studio 使用既有的签名
`CreateAgent(Code)` / `UpdateAgent` 生命周期；SDK 客户端当前将创建映射到
`CreateAgentProduct`，但这不是新增的 Studio 或 Server Action。

## 范围与边界

- 本地构建仍由 `POST /api/v1/agents/{id}/builds` 发起，Bundle digest 是唯一可
  展示的构建事实。
- 云端部署仍由 `POST /api/v1/builds/{id}/deployments` 发起：Studio 将通过既有 KS3
  uploader 上传不可变 ZIP，再使用既有 `CreateAgent(Code)`；回滚复用 `UpdateAgent`
  指向历史 ZIP，状态刷新使用 `GetAgent` 的 Server readiness 投影。不得新增
  `CreateAgentArtifact` 或浏览器 trusted-header 路径。
- 生命周期首版包含部署、刷新状态、用指定历史 Build 重部署（回滚）。不把未验证
  的“停止/重启”按钮伪装成已有能力；删除云端 Agent 需单独复用并审计既有
  `DeleteAgent` Action 后再进入范围。
- 每一条部署记录保存在 Studio workspace 的 `.agentkit/deployments/`，并由 Server
  实例状态刷新；记录缺失或刷新失败必须显式显示未知/失败，不能显示 Ready。
- `origin/agentkit-studio-phase1` 是视觉和交互的参考实现，不是可直接合并的功能
  分支。只吸纳其 React、样式和可复用 UI 原语；不得覆盖本分支的 Bundle receipt、
  运行时兼容性或 Server API 语义。

## 视觉与信息架构

吸纳 `origin/agentkit-studio-phase1` 的 Soft Block 视觉系统，不整分支 merge。
颜色使用 `--canvas`、`--surface`、`--surface-subtle`、`--accent`、success 与 danger
等已有 token；无 box-shadow，用 outline 区分可独立阅读的区域。排版沿用该分支
的全局 Header、导航、PageHeader portal 和 document/workbench 两种布局。

页面的特征元素是“交付事实链”：`Bundle → CreateAgent → 云端实例` 三段状态，不把它画成
不可验证的百分比进度。每段显示从后端得到的 ID、digest 或实例状态；没有事实的
段显示“未开始”或“未知”。同一套 Header、导航、页面标题、表单、数据表、空状态、
错误状态和窄屏规则覆盖所有既有模块，不能仅让构建/部署页采用新样式。

## 参考来源与归属

本工作台视觉吸纳自 `origin/agentkit-studio-phase1` 上 Liubin 的两个提交：

- `bffda8fe1593aea97b98cc64b75981daf47880e5`
  `feat(studio): 完善生产级 WebUI 与运行完整性校验`
- `cdc2b1733bee51cb9cc896398067e1dd80c186a3`
  `feat(studio): 统一工作台视觉与交互层级`

它们不是可直接 cherry-pick 的提交：其中同时包含 builder、runtime、capability、cloud
和 Studio API 行为，直接合并会覆盖本分支已经接入的 Bundle receipt
以及 Server 投射状态。实现应在新提交中以 `Co-authored-by: liubin9
<liubin9@kingsoft.com>` 和 `Portions-from` trailers 记录来源，保留代码归属与可追溯性。

| 参考内容 | 吸纳方式 | 明确不吸纳的行为 |
| --- | --- | --- |
| Soft Block 基础层、KingCloud 视觉层、响应式和焦点规则 | 原样保留 token/组件选择器，再以交付事实链作最小扩展 | 任何把 UI 状态写成运行时事实的规则 |
| 全局 Header、导航分组、PageHeader portal、表单/表格/更多操作原语 | 保留 React 结构、可访问性和交互层级 | 改写当前 hash 深链接、既有资源 CRUD 请求或本地 Agent 选择逻辑 |
| Agent、创建/编辑、资源、会话、运行资源、编排、可观测和设置页面 | 逐页迁移视觉、空/错/加载态和真实错误可恢复体验 | `builder.py`、`framework_run.py`、`capabilities.py`、`cloud.py`、`api.py`、凭证收集与部署调用语义 |

其中设置页只展示非敏感 Region/Bucket 与签名账号是否来自启动环境；不录入、不持久化
AK/SK。运行资源和部署页只显示 API 返回的 receipt/实例事实，不把“本地配置存在”
描述为预发可用。

```text
全局 Header: 当前页面 | [构建当前 Agent] / [刷新]

构建页 (document)
  [Agent / revision / Bundle digest / Runtime / 状态]  stat strip
  [解析 -> 锁定 -> Bundle]                             事实链
  [最近构建历史]                                        table

部署页 (document)
  [已部署 / 部署中 / 失败或未知 / 预发区域]             stat strip
  [Bundle -> CreateAgent -> 实例]                        事实链
  [部署记录 table: 状态、实例、Build、digest、时间、更多操作]
```

页面级主要动作通过 Header portal 注入；行操作使用 MoreActionsMenu。所有图标操作有
可访问名称，窄屏按现有 responsive.css 收缩成单列，不另造布局类型。

## 全工作台吸纳矩阵

| 模块 | 必须保留的事实/行为 | 统一后的界面要求 |
| --- | --- | --- |
| Agent 列表、详情、创建和编辑 | 本地 Agent 定义、revision、Build/Deploy 入口 | 信息层级、空状态、表单、详情动作和深链接统一 |
| 构建和部署 | immutable Bundle、既有 Agent 生命周期、实例状态、回滚 | document 布局、事实链、状态与操作日志 |
| 会话与运行面板 | 实际 session、事件、运行错误 | workbench 布局、可见运行状态和可恢复错误 |
| 工程资源与运行资源 | model/tool/MCP/skill 的既有 CRUD 和路由 | 统一资源导航、表格、详情与表单反馈 |
| 任务编排与可观测 | 既有只读/编排 API 返回值 | 同样的空、加载、失败和窄屏体验，不能用静态假数据 |
| 设置与全局导航 | workspace、主题、连接状态 | 全局 Header、Navigation Rail 和 PageHeaderActions 一致 |

## 数据与错误语义

新增 Studio 的只读 `GET /api/v1/deployments`，它列出 workspace 中经过严格文件名
校验的本地部署 receipt，按 record id 返回；单条 `GET` 仍负责向 Server 刷新状态。
React 部署页先加载列表，再对可刷新项显式刷新；一条刷新失败只把该行标为 unknown
并给出重试，不遮蔽其他部署。

回滚沿用已有 `POST /api/v1/deployments/{id}:rollback` 和不可变 target build ID；
成功结果是新的 deployment receipt，不能篡改历史记录。前端在提交操作时展示真实
Operation 终态，只有 `SUCCEEDED` 后才跳转或声明“已提交”。

## 验收

1. 单测覆盖 deployment listing 的路径约束、排序、空集合和单条 refresh 失败。
2. React 测试覆盖构建/部署事实链、状态标签、回滚请求和失败提示；不 mock 成 Ready。
3. `npm run build`、UI 测试与 Studio API 测试通过。
4. 预发部署后，真实浏览器从 Studio 创建本地 Agent、构建、点击部署到预发，确认
   KS3 上传和既有 CreateAgent 受理、等待云端实例与 Kernel readiness、刷新状态，并
   通过云端 Agent 调用验证。浏览器证据只使用真实签名账号；不使用伪造的 trusted
   identity header。
