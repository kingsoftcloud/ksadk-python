# Studio 云端交付工作台设计

## 目标

让 Studio 用户在同一 React 工作台中完成并看见真实的本地构建、预发 Bundle
准入、云端实例启动、状态刷新、重部署和回滚。页面不是第二套控制面：它只能
调用 Studio API，由 Studio 使用既有 Server Action 完成 Artifact admission 与
`CreateAgentProduct`。

## 范围与边界

- 本地构建仍由 `POST /api/v1/agents/{id}/builds` 发起，Bundle digest 是唯一可
  展示的构建事实。
- 云端部署仍由 `POST /api/v1/builds/{id}/deployments` 发起；Studio gateway 只使用
  `CreateAgentArtifact`、`CreateAgentProduct` 和 `FetchAgentInstanceStatus`。
- 生命周期首版包含部署、刷新状态、用指定历史 Build 重部署（回滚）。不把未验证
  的“停止/重启”按钮伪装成已有能力；删除云端 Agent 需单独复用并审计既有
  `DeleteAgent` Action 后再进入范围。
- 每一条部署记录保存在 Studio workspace 的 `.agentkit/deployments/`，并由 Server
  实例状态刷新；记录缺失或刷新失败必须显式显示未知/失败，不能显示 Ready。

## 视觉与信息架构

吸纳 `origin/agentkit-studio-phase1` 的 Soft Block 视觉系统，不整分支 merge。
颜色使用 `--canvas`、`--surface`、`--surface-subtle`、`--accent`、success 与 danger
等已有 token；无 box-shadow，用 outline 区分可独立阅读的区域。排版沿用该分支
的全局 Header、导航、PageHeader portal 和 document/workbench 两种布局。

页面的特征元素是“交付事实链”：`Bundle → 准入 → 云端实例` 三段状态，不把它画成
不可验证的百分比进度。每段显示从后端得到的 ID、digest 或实例状态；没有事实的
段显示“未开始”或“未知”。

```text
全局 Header: 当前页面 | [构建当前 Agent] / [刷新]

构建页 (document)
  [Agent / revision / Bundle digest / Runtime / 状态]  stat strip
  [解析 -> 锁定 -> Bundle]                             事实链
  [最近构建历史]                                        table

部署页 (document)
  [已部署 / 部署中 / 失败或未知 / 预发区域]             stat strip
  [Bundle -> 准入 -> 实例]                              事实链
  [部署记录 table: 状态、实例、Build、digest、时间、更多操作]
```

页面级主要动作通过 Header portal 注入；行操作使用 MoreActionsMenu。所有图标操作有
可访问名称，窄屏按现有 responsive.css 收缩成单列，不另造布局类型。

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
4. 预发部署后，真实浏览器从 Studio 创建 Agent、构建、点击部署到预发、等待
   Server admission 和云端实例、刷新状态，并通过云端 Agent 调用验证。浏览器证据
   只使用真实登录态；不使用伪造的 trusted identity header。
