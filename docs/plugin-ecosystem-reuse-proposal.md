# 提案：ksadk 插件生态复用与兼容架构（dsh Sidecar + Codex 数据面）

> **状态**：实施交接方案；代码已有部分实现，尚未达到本方案的联合发布验收条件。
>
> **修订日期**：2026-09-05。
>
> **目标版本**：Python 分发包 `ksadk 0.8.4`（仓库 `ksadk-python`）与 `@kingsoftcloud/ksadk-web 0.3.5`。这是候选目标，不代表批准发布或已完成。
>
> **实施基线**：Python `feat/studio-chat-message-list-0.3.5@893730c4`；Web `feat/conversation-v1-unified-render@52fa2dc`。贡献者插件实现基线为 `d0e6bab5`。
>
> **读者**：接手实现的本地 Claude、后续 reviewer、发布维护者。
>
> **对齐文档**：`AGENTS.md`、`docs/phase2-plugin-compatibility.md`、`docs/public-release-workflow.md`、`contracts/plugin/v1/`。

**阅读顺序**：实施者先看 §0/§11/§12，再按任务查 §13–§16；架构背景见 §1–§9。给 Claude 的可直接复制任务在 §17.3，review 材料模板在 §17.1。

本文区分三类内容：**已核验现状**是截至上述基线的源码或实测事实；**本版要求**是实现必须达到的行为；**后续建议**不进入这次发布范围。测试名称存在、旧报告通过、代码合并，均不等于最终候选产物验收通过。

## 0. 实施约定与本版交付范围

### 0.1 接手后的第一步

1. 读取各仓 `AGENTS.md`，核对分支、HEAD、工作区、远端引用和本方案基线差异。
2. 保留已有贡献与用户改动。已知 Web 根目录有 `.agentengine.state` 和 `.worktrees/` 未跟踪内容，后者包含已登记 worktree；不得直接删除或用 `git clean` 清理。
3. Python 已完成 `github/main → 本地 master → 当前特性分支` 的合入。后续新增远端变更按相同方向评估；公开导出另走审核候选，不能把内部 master 直接推向 GitHub main。
4. 先实施 §12 的 T1/T6a，建立可信测试结果；不要为让门禁变绿而隐藏失败、放宽来源或权限策略。
5. 每个任务交付一个行为完整的小改动，记录命令、结果、受影响文件和剩余限制。发现本方案与更新后的源码不符时，修订对应事实与任务，不并存两套相反指导。

### 0.2 本版必须完成与明确暂缓

| 本版必须完成 | 暂缓到后续版本 |
|---|---|
| DSH capability 与 Studio UI sandbox 的真实前后端闭环 | 广泛的 Context/Hook/Store capability 扩展 |
| Codex 插件的受支持数据面与真实回合验证 | 没有可验证授权机制的 hooks 执行 |
| 真实上游工具插件和 UI 插件的兼容性证据 | 自研插件 monorepo、市场治理、脚手架全套建设 |
| 共享会话体验、审批/反馈、回显与性能验收 | 全量 CodexRuntimeAdapter 重写或 Legacy Runtime 删除 |
| 正确的发布门禁、干净安装与制品来源证明 | 独立应用插件的常驻进程/存储管理面 |

其中 UI 插件可以先用自研 Cordis fixture 做开发验证，但它不能替代真实上游兼容证据。若真实上游样本在本版边界内无法运行，应给出缺失依赖/接口与复现，交维护者决定缩减发布承诺或继续实现；不能自行把“不支持”改成“已完成”。

### 0.3 完成定义

- 用户能够在 Studio 安装、启用、绑定受支持插件，在真实 Agent 回合或插件面板中调用，禁用/卸载后权限与 UI 同步撤销。
- Studio 三种会话形态与 Hosted UI 使用共享的 `ksadk-web` 会话组件及状态逻辑。Hosted 历史 Agent 兼容继续保留。
- 自定义样式不要求复制消息、审批、滚动或会话恢复代码；主题覆盖有独立消费者验证。
- 流式、会话切换、刷新恢复、审批、多选、自定义反馈、终态失败均有真实浏览器证据。
- 发布候选的代码、依赖、静态资源、npm tarball、wheel/sdist 和证据中的提交一致。
- 缺少上游二进制、凭证或预发环境时，记录 `blocked/not-run` 与原因；不得计为成功。

## 1. 架构决策与支持边界

### 1.1 保留的核心决策

1. 对 DSH/Cordis 生态复用 Node 运行环境和上游插件形态，不在 Python 中重写 Cordis。
2. 对 Codex 插件复用文件数据面和 App Server 控制面，不另建 marketplace 认证/下载安装体系。
3. 插件作者只面对 Cordis/DSH 包或 Codex 插件包。内部传输契约、lock 和能力投影由 KsADK 宿主管理，不要求第三方作者再写一套 KsADK 插件 manifest。
4. 完整 Agent 执行与工具能力投影分开：`agent.provider/v1` 保留现有 JSONL；capability host 使用 loopback MCP，见 §5。
5. session、transcript、审批决策、事件持久化与审计归宿主；插件不能自建一套具有相同职责的主状态机。
6. 界面展示的通用行为归 `ksadk-web`；Studio 插件管理和工作台扩展属于 Studio 业务，不能为追求薄壳而全搬进共享聊天包。

### 1.2 三个必须分开的概念

| 概念 | 能证明什么 | 不能据此推导什么 |
|---|---|---|
| Node 子进程运行 Cordis | 可以复用对应 Node 生态执行环境，隔离部分崩溃 | 任意上游插件、Electron API、客户端 UI 都兼容；进程拥有真正 OS 沙箱 |
| 标准 MCP 工具传输 | 统一发现/调用的传输入口 | 工具已经获得用户批准、所有调用已经审计、所有 Provider 都已完成绑定 |
| schema/descriptor/artifact digest | 标识与校验具体结构或字节，拒绝漂移 | 自动向后兼容、可以放宽 lock、任何 schema 变更必须让全部历史 Agent 重建 |

### 1.3 本版支持矩阵

下表是发布承诺边界；“待验证”不是“已支持”。工具能被发现与 Agent 能执行该工具要分别记录。

| 组合 | 当前代码边界 | 本版交付要求 |
|---|---|---|
| Codex 格式插件 → Codex Provider | 存在 manifest/skills/MCP 装配与 App Server bridge | 用最终候选验证发现、选择、实际调用、失败恢复 |
| Codex hooks | 存在解析/快照/lock；执行受 trust 不可用限制 | 保留明确不支持的错误与 UI 提示，不偷偷执行 |
| Cordis 工具 → DSH capability host → Harness | 存在服务、Profile 投影与绑定 | 真实上游插件 A 通过安装至 Agent 回合闭环 |
| DSH Profile MCP → Codex Provider | `codex_agent_service.py` 明确拒绝，当前只支持 Harness | 本版保持明确禁用；若要新增支持，先单独设计受管绑定、凭证/生命周期和审批，不靠改错误文案或删校验完成 |
| DSH 完整 AgentProvider | 原 JSONL ABI 与 Provider 生命周期存在 | 新 capability host 不破坏旧 Provider 测试及支持的 bundle |
| Cordis UI → Studio | sandbox API 已有；前端尚未接通该路径 | 插件 B 经 sandbox + 宿主中继真实运行，不在顶层执行 |
| 任意 DSH 插件 → ADK/LangGraph/云端 | 不能因工具 schema 存在推定支持 | 按选定组合记录实测；云端必须装载真实插件资产和对应 Node 工具链 |

此矩阵不要求给 Codex 添加 KsADK 内置 Tool。Codex 继续只接原生工具、受支持 MCP 和 Skills；创建页、持久化 Draft、编译/Bundle 三层都要维持这一边界。

## 2. 参考项目与 GitHub 仓库对照

以下为原方案保留的上游调研线索。Wegent/wework 引用基线为本地镜像 `e29a4222a`；本次修订没有重新核验上游最新 HEAD。实施时先记录实际使用的上游 commit、包版本、许可证和平台要求，再据此更新兼容矩阵；不得把本节的历史观察当成所有新版本的保证。

| 参考项目 | 仓库地址 / 路径 | 在本架构中的参考意义 |
|---|---|---|
| **DeepSeek Harness (dsh)** | [`https://github.com/deepseek-ai/deepseek-harness`](https://github.com/deepseek-ai/deepseek-harness) | 插件生态源头（Cordis IoC 运行时、`cordis.patch.yml` Profile 体系、`@deepseek-ai/dsh-*` npm 生态；官方 `python/sdk` 的 `deepseek-harness-runtime-bin` Sidecar 模式） |
| **Wegent / wework** | [`https://github.com/wecode-ai/Wegent`](https://github.com/wecode-ai/Wegent)（子目录 `wework/`） | 同构桌面工作台实证（Electron + TS 宿主通过独立 Node 进程跑官方 dsh CLI，前端自解析 `.codex-plugin/plugin.json`，统一插件市场 UI 对标终态） |
| **OpenAI Codex (app-server)** | [`https://github.com/openai/codex`](https://github.com/openai/codex)（本地 `codex/` 镜像） | Codex 官方插件协议标准（两层协议：控制面 `plugin/*` / `marketplace/*` stdio JSONL RPC + 数据面 `plugin.json` / MCP / Skills / Hooks 文件包） |

### 2.1 wework 是怎么做的：一个同构的生产级实证

即使 wework 本身是 TypeScript / Electron 生态，**它也没有在 Electron 主进程中 `import` Cordis 容器**，而是同样采用了独立 Node 进程的 Sidecar 架构：

```
┌──────────────────────────────────────────────────────────────┐
│  Electron Main 进程 (TS/Electron)                            │
│  - 职责: 宿主管理、窗口生命周期、Host Pipe、Capability Router  │
│  - 不加载 Cordis，不 import dsh 插件                          │
└──────────────┬───────────────────────────────┬───────────────┘
               │ spawn(node, dsh/bin.js)       │ spawn(node, dsh/bin.js)
               ▼                               ▼
┌──────────────────────────────┐┌──────────────────────────────┐
│ DSH Core 进程 (独立 Node 进程) ││ Workbench DSH 进程 (Per-Tab) │
│ - 跑官方 @deepseek-ai/dsh CLI ││ - 跑官方 @deepseek-ai/dsh CLI │
│ - 内部 in-process 运行 Cordis││ - 内部 in-process 运行 Cordis│
│ - 加载 @wegent/dsh-* 插件     ││ - 每个工作区独立隔离         │
│ - 经 HTTP / Pipe 向宿主暴露服务││ - 崩溃不影响 Core            │
└──────────────────────────────┘└──────────────────────────────┘
```

- **源码实证**：`wework/package.json` 依赖 `@deepseek-ai/cordis@4.0.1`，`wework/dsh/` 下 14 个子包以 Cordis 为 peerDependency（编写的是真 Cordis 插件）；但 `electron/src/runtime/core-dsh-runtime.ts` 通过 `spawn(nodeCommand, node_modules/@deepseek-ai/dsh/lib/bin.js, --profile wework-core)` 将它们拉起为**独立 Node 子进程**。（引用基线：本地镜像 Wegent/wework HEAD `e29a4222a`，2026-09 核实；正式定稿前如镜像更新需重新 pin。）
- **为何 TS 宿主也要起独立进程**：
  1. **崩溃隔离**：插件代码中的 native 模块崩溃、死循环或 OOM 不会带崩宿主 UI；
  2. **多版本并存**：wework 通过 `harness-runtime/` 目录分版本管理运行时资产，不同 Tab 可加载不同版本的 DSH 运行时；
  3. **官方 CLI 零改写复用**：直接以子进程方式运行官方 `@deepseek-ai/dsh` 的 CLI entrypoint，传入 `--profile` 即可完成装配。

### 2.2 wework 对 Codex 插件的做法

- **自己解析 Manifest**：wework 前端（`PluginImportDialog.tsx`、`codexPlugins.ts`）直接解析校验 `.codex-plugin/plugin.json` 文件。
- **委托 Executor 执行**：`managed-executor-runtime.ts` 配置 `CODEX_HOME` 与 `CODEX_BINARY_PATH`，将执行交由 Rust Sidecar (executor) 或本机 codex 二进制完成。

### 2.3 跨项目模式对照与结论

| 维度 | deepseek-harness (dsh) | Wegent/wework | ksadk (本提案) |
|---|---|---|---|
| **宿主语言** | Node.js / TS | Node.js / TS (Electron) | Python |
| **DSH 插件宿主** | 进程内 Cordis 容器 | **独立 Node 进程** 跑官方 dsh CLI | **独立 Node 进程** 跑 Node Sidecar |
| **DSH 插件形态** | 真 npm 包，Cordis `apply(ctx)` | 真 npm 包，Cordis `apply(ctx)` | 真 npm 包，在 Sidecar 内部加载 |
| **宿主通信协议** | 进程内内存调用 | HTTP (`/wework/executor/v1`) + Pipe | loopback MCP（当前决策，见 §5.1） |
| **Codex 插件** | 桥接 hooks.json 到 agent loop | 前端解析 manifest，委托 executor 执行 | 前端/Bridge 解析 manifest，委托 App Server |

**参考结论**：独立 Node 进程承载 Cordis 值得沿用；实际宿主服务、工具注册接口、客户端模块依赖、平台权限和持久化仍需逐项适配。相似的进程结构不能证明插件兼容，也不能证明安全隔离。

---

## 3. 总体架构与代码职责

```text
Studio 插件管理 / Agent Draft
    └─ CompositionCompiler → CompositionProfile + PluginLock + Bundle
          └─ PluginHost（准入、激活、生命周期）
               ├─ Codex AgentProvider → App Server → skills / MCP
               ├─ DSH AgentProvider → 原 JSONL 执行宿主
               └─ DSH capability host → loopback MCP → Node/Cordis 插件

Studio 插件面板
    └─ sandbox iframe ↔ MessagePort ↔ Studio 可信宿主页
          └─ apiFetch + Studio API → 授权/审计 → capability service → MCP

本地 / Studio 云端 / 远程 LangGraph / Hosted 历史 Agent
    └─ transport / compatibility adapter
          └─ 共享事件投影与会话状态 → ksadk-web 的 timeline/composer/interaction
                └─ Studio 或 Hosted 提供路由、目标、主题与少量业务组合
```

### 3.1 代码定位表

以下路径相对 `ksadk-python`；标明 Web 的路径相对兄弟仓 `ksadk-web`。这些是现有入口，不要求机械地按表重命名或新建目录。

| 责任 | 现有入口 |
|---|---|
| DSH 安装、来源、Profile 生命周期 | `ksadk/plugins/bridges/dsh.py` |
| capability host 描述/启动/通信 | `ksadk/plugins/providers/dsh_capabilities.py` |
| 发运 Node capability host | `ksadk/plugins/providers/bundles/ksadk-dsh-capability-host/` |
| DSH 完整 AgentProvider | `ksadk/plugins/providers/dsh.py` |
| Studio capability 调用与 generation | `ksadk/studio/dsh_capability_service.py` |
| UI session API 与插件 API | `ksadk/studio/api_plugin_routes.py` |
| UI session 校验、TTL、iframe 文档 | `ksadk/studio/dsh_ui_sandbox.py` |
| 工作台插件前端与 slot registry | `ksadk/studio/react-ui/src/dsh-runtime/` |
| 插件展示/绑定与 Agent 编译 | `ksadk/studio/react-ui/src/pages/PluginsPage.tsx`、`ksadk/studio/plugin_composition.py`、`ksadk/studio/compiler.py` |
| Codex manifest 与已安装组件 | `ksadk/plugins/codex_manifest.py`、`ksadk/studio/codex_manifest.py`、`ksadk/studio/codex_plugin_store.py` |
| Codex 运行与绑定校验 | `ksadk/studio/codex_agent_service.py`、`ksadk/studio/codex_run.py` |
| 本地与云端会话桥 | `ksadk/studio/shared_web.py`、`ksadk/studio/cloud_shared_web.py`、`ksadk/studio/api.py` |
| 共享状态、流式、交互 | Web：`src/core/conversation/`、`src/core/run/`、`src/core/stream/`、`src/core/interaction/` |
| 共享会话生命周期/展示 | Web：`src/hooks/useSessionLifecycle.ts`、`src/hooks/useRunAgent.ts`、`src/components/chat/` |
| 发布与证据 | `scripts/phase2_release_preflight.py`、`scripts/phase2_release_candidate_gate.py`、`Makefile`；Web：`scripts/release-preflight.mjs`、`scripts/release-provenance.mjs` |

### 3.2 Studio 与共享 Web 的界线

- `ksadk-web`：纯展示组件、可连接的组合组件、headless 状态、事件适配、审批/反馈状态、分页/滚动、主题变量、无障碍与动效。
- Studio：workspace/Agent/部署目标选择、认证/API 注入、插件安装和绑定、工作台扩展页、主题配置。
- Hosted：入口与认证、配置和路由、历史运行时兼容能力选择；历史协议归共享 adapter 收敛，不复制一套卡片/消息 renderer。
- A2UI/会话卡片只消费已校验的声明式数据和已发布的 renderer；不能让运行时消息携带 JavaScript 在主页面执行。
- 插件完整 UI 走隔离容器，不能以“共享 renderer 扩展”绕开容器。需要展示审批时，仍使用宿主交互协议与共享卡片。

### 3.3 已有 Web 公共入口与样式覆盖

| 已有 package subpath | 用途 | 本版要求 |
|---|---|---|
| `@kingsoftcloud/ksadk-web/conversation` | headless reducer/client、类型、历史重建 | 保持 Node/SSR 可导入，不引入 React/DOM 或 Studio API |
| `@kingsoftcloud/ksadk-web/chat/timeline` | AgentConversationTimeline、ChatMessageList、ProcessingBlocksView 等 | 共享消息分组、工具/思考与回放展示 |
| `@kingsoftcloud/ksadk-web/chat/composer` | AgentConversationComposer、ChatComposer、InteractionTray、各菜单 | 共享审批/问题卡、发送及模型/权限控件 |
| `@kingsoftcloud/ksadk-web/components`、`/hooks`、`/runtime` | 其他公共组件与状态组合 | 不因内部迁移意外删除既有 export/类型 |
| `@kingsoftcloud/ksadk-web/styles` | 发运 CSS | 独立消费者引入即可工作，主题覆盖无需复制 CSS 全文件 |

Studio 根据业务消费所需组件；Hosted 可消费更完整的 workbench。组件数量不同不影响会话语义统一。优先使用公共入口，不能从包内部 `src/...` 深导入让本地链接可用、发布包不可用。

样式沿用现有 token/类名/props 能力，缺口再做最小增量。至少覆盖背景、前景、弱文本、边框、焦点、强调色、圆角、内容宽度和间距；为 Portal 设置明确的主题作用域。`className/style` 与受控组件替换可以开放，但替换纯展示不能跳过 interaction 验证或 durable 状态。

独立消费者样例需同时展示默认主题与自定义主题，并真实操作菜单、审批、自定义反馈和滚动。主题测试只验证 CSS/视觉，不用样式快照去约束内部业务实现。

## 4. 安装、编译与运行链路

### 4.1 Codex 数据面

区分“来源坐标校验”“读取宿主已解析/安装的内容”“自行下载源码”三个动作。当前 KsADK 不负责从 Git/npm 抓取 Codex marketplace 插件；它读取宿主已经解析的内容，并锁定坐标。不得在交付说明写成三个下载器均已实现。

本版必须覆盖：manifest 解析与负例、选择组件、skills/MCP 注入、实际 App Server 回合、安装失败回滚和已有插件不被误移除。Git 坐标仍需不可变 commit，npm 坐标仍需精确版本及 integrity；不能把可变 tag/range 记录成可重现产物。hooks 维持 §1.3 的执行边界。

### 4.2 DSH 安装及绑定

```text
Studio 展示来源/精确版本与宿主权限说明
 → 用户明确安装授权
 → plugins:install（registry 精确版本）
 → 安装成功但保持禁用，显式启用后 reconfigure Profile
 → capability descriptor / Catalog Resource 投影
 → 根据当前 Provider 支持矩阵展示可选绑定
 → 编译时校验 resource、owner、digest、runtime compatibility
 → Agent 执行调用 → 结果/错误/取消进入宿主会话事实
```

- Studio REST 安装限定 registry 精确版本；本地开发 CLI 支持绝对路径的既有能力独立保留，不把本地路径开放到浏览器安装入口。
- 更新失败保留上一份有效 Profile/lock/运行资产；不能留下“安装完成但无可用配置”的半状态。
- 默认不安装或启动额外 Agent loop。只有用户选择完整 DSH AgentProvider 时，才由该 Provider 接管回合执行。
- `api_plugin_routes.py` 的现有返回结构继续复用；确需新增字段时同步模型、契约、fixture、前端类型和兼容说明。
- Bundle 外层版本不必随每个能力新增升级；内部 lock 或投影变更必须进行兼容性分析，不能一概称“Bundle 零变更”。

## 5. 契约、执行语义与迁移

### 5.1 当前采用的双通道决策

| 通道 | 协议/用途 | 本版约束 |
|---|---|---|
| 完整执行 | `ksadk.dsh-agent-provider-host/v1`，JSONL 生命周期与 execute/cancel | 保持现有 descriptor/方法集合与消费者兼容 |
| 工具能力 | `ksadk.dsh-capability-host/v1`，启动 ready/token + loopback Streamable HTTP MCP | 只投影当前支持的 `mcp.connector/v1`，不添加假想 hook seam |
| 插件 UI 中继 | `agentkit.dsh-ui/v1`，MessagePort + Studio API | 是宿主内部 UI 通信，不是要求插件作者再维护的新安装格式 |

旧提案曾计划给 AgentProvider JSONL 增加 `call_tool`、把 descriptor 改为 `capabilities[]`；此设计已被独立 capability host 替代，不再作为实施任务。§2 中调研结论不能恢复这条已替代路线。

### 5.2 执行语义

1. **身份与归属**：每个请求有稳定 requestId/callId；UI callId 由已有 `dsh_ui_mcp_call_id` 纳入 uiSession 作用域。不可跨 session 取消其他调用。
2. **超时**：复用现有 deadline 字段与服务端上限。当前 UI `callTool` 默认 30 秒、最大 120 秒；不在前端偷偷添加无限等待，工具专属上限应经配置与契约明确。
3. **取消**：单调用取消与整个回合取消区分；浏览器 abort 不自动等于工具已停止。返回值与 terminal 状态依据服务端事实。
4. **重试**：连接失败可恢复连接，已经发出的副作用工具不能自动重放。超时或丢失响应不等于未执行，需要查询结果或告知执行状态未知。
5. **错误**：至少区分参数/业务失败、用户拒绝/反馈、策略拒绝、取消、超时、sidecar 不可用、generation 变化；不能统一转成“网络问题”。错误码优先复用现有定义。
6. **结果**：首版工具最终结果采用请求/响应；Agent 思考/文本流仍按共享事件协议输出，不为制造动效把工具 JSON 拆成 token。
7. **回收**：drain/disable/uninstall/stop 撤销授权并处理在途调用；同一任务只能落一个确定终态，迟到事件不能覆盖新的 generation 或已结束的 run。

### 5.3 digest 与兼容层级

| 层级 | 作用 | 演进要求 |
|---|---|---|
| `contracts/plugin/v1/manifest.json` 的 aggregate digest | 契约集发布/一致性证据 | schema 变更同步更新登记及 conformance，不把它当所有 runtime 的统一协商字段 |
| descriptor / inventory digest | 描述当前宿主能力与工具清单 | 校验能力来源，运行期变化使旧授权失效 |
| 内部 PluginManifest / PluginLock / Bundle digest | 锁定编译选择和发运资产 | 不放宽精确校验；分析受影响资产，必要时重编译并记录 |
| npm integrity / 文件 SHA-256 | 校验下载包和实际代码字节 | 不可变产物；不能用 schema digest 代替包校验 |
| 协议版本与兼容测试 | 判断读写语义是否受支持 | 加法扩展、破坏性变更分别评估；有摘要仍要测语义 |

旧 AgentProvider descriptor → 内部 PluginManifest → PluginLock 的投影链继续存在。新增独立 capability schema 本身不证明所有历史 bundle 的 lock 已失效；重算 manifest digest 也不证明任何 bundle 已重建。

### 5.4 兼容及迁移决策

`v0.8.3` 标签已包含插件契约文件。本次不以“没有已知生产消费者”为理由任意重写 v1，也不为普通加法扩展自动引入 v2。

- 无关的历史 framework Agent、Hosted 历史会话继续按既有兼容路径读取，不要求用户整体重建。
- 对每个受影响的 schema/descriptor/lock 变更，记录旧/新 fixture、读写兼容、受影响产物、迁移方式与错误提示。
- 若现有 reader 可安全兼容，保留精确来源校验后只读归一化；不可兼容时使用显式新版本或明确迁移。不能删字段伪装兼容。
- 严禁把 lock 校验降级为仅比较 major 或忽略 digest；严禁运行时偷偷改已发布 bundle。
- 对确实需要重编译的已知产物，记录原版本/摘要、新版本/摘要及验证结果；对无影响产物保留回归 fixture。
- 贡献者测试插件 `.codex-plugin/plugin.json` 的版本按用户已确认的 **`0.1.0`** 保留，不为配合 SDK 版本号改成 `0.8.4` 或其他版本。

## 6. Node 运行时与部署边界

### 6.1 当前代码基础与仍需验收的内容

代码已有 Node 最低版本检查（22.19.0）、受控 NODE_OPTIONS、环境过滤、POSIX 进程组回收、bundle integrity、descriptor/inventory 校验和漂移拒绝。它们分别提供启动/资源/完整性控制，不能合并描述成“沙箱全部完成”。

| 环境 | 本版要求 | 验证 |
|---|---|---|
| 本地 Studio | 已安装工具链可探测；缺少或版本不符有明确可操作错误 | 干净 uv 环境 + 干净 Profile 启动；测试缺 Node/旧 Node/退出异常 |
| CI Linux | 安装并固定 Node/Codex/DSH 所需版本，记录实际可执行路径与版本 | 不能只看 npm install 成功；实际运行 CLI 和回合 |
| 云端 Harness/plugin Agent | 运行镜像与 bundle 包含或可靠装载选定工具链及不可变插件资产 | 预发真实 Pod/进程就绪与调用成功；镜像配置不等于实际运行状态 |
| 未选 DSH 的已有 Agent | 不因新能力要求安装 Node 或重建 | 历史 ADK/LangGraph 产物回归 |
| 其他 OS/架构 | 按公开支持范围验证或明确限制 | POSIX 清理通过不能当 Windows 已验证 |

### 6.2 云端未就绪诊断

`runtime agent kernel is not ready` 必须沿实际链路分类：Bundle 准入/解包、Provider 注册、Node/入口启动、依赖/凭证装载、Kernel ready、路由连接。收集对应 build/runtime/image/日志时间，向 UI 返回稳定分类；不能仅延长前端超时或无限重新部署掩盖原因。

本任务不授权自动覆盖生产部署。真实预发验证使用独立测试 Agent/会话与明确目标；生产变更、重新发布由维护者决定。

## 7. 信任、授权、审批与审计

### 7.1 四种权限不可混为一谈

| 权限 | 控制内容 | 本版要求 |
|---|---|---|
| 插件安装信任 | 是否允许所选来源代码在宿主执行 | 展示来源/版本与 `acceptHostPermissions`，不后台代替用户同意 |
| 执行沙箱 | 文件、进程、网络等实际执行能力 | 与工具是否只读分开建模，不把文件只读必然等同禁网 |
| 会话审批偏好 | 哪些可执行行为需要确认 | 从选择器到请求 metadata、runtime effective policy、卡片闭环都一致 |
| 单次交互决策 | 批准/拒绝/会话授权/反馈/表单答案 | 精确对应当前 interaction 和 run，不将任意非空输入当批准 |

“完全访问”选项的文案和内部映射可能分别指沙箱或审批；实施时读现有映射，分别显示实际含义。默认行为建议工作区可写、对风险操作确认；不能未经迁移覆盖用户已保存的明确限制。执行权限被更高层限制时，应展示有效策略及原因，禁止出现 UI 显示请求批准但 runtime 收到 `never` 且无说明。

### 7.2 进程与 UI 隔离

- 同一 OS 用户下的 Node 子进程仍可能访问宿主文件/网络。当前安装授权意味着信任代码可使用宿主权限；环境过滤、heap 上限、进程组回收不能当作 OS 文件隔离。
- 若产品要求执行不受信任代码，另行采用真正的 OS/container 隔离与权限配置；本版不把尚未实施的能力宣传成已具备。
- UI iframe 使用现有 `sandbox="allow-scripts"`、opaque origin、CSP、credentialless/referrer 限制与 bundle 校验；不得通过加 `allow-same-origin`、去掉 CSP 或开放任意 eval 解决兼容问题。
- 私有 token、宿主 Cookie、CSRF 不进入插件 URL、DOM、持久化状态、错误文本或日志。可信宿主页用现有 `apiFetch` 中继，插件不直连 sidecar。

### 7.3 UI 工具调用的授权闭环

现有路由校验 UI session、token、来源、descriptor/generation、工具白名单，并直接调用 capability service。**这并不单独证明业务审批和审计已完成**；T2 必须核对复用的服务路径。

- session 的有效 tool 集合应由服务端根据插件归属、当前 workspace/Agent 绑定、可用 descriptor 与授权共同计算，不能仅把浏览器请求的 toolIds 与全量工具名取交集后视为充分授权。
- 插件 UI 点击不是对任意副作用操作的自动授权。风险调用需要复用宿主策略；若已有组件缺接口，先补一个集中服务入口，再由 Agent/UI 共用。
- 审计至少关联来源（Agent/UI）、plugin/profile、Agent、run 或 uiSession、callId、有效策略/决策与结果；参数按现有脱敏规则记录。
- 如果某工具尚无法接入所需审批，明确不支持并拒绝该调用，不能执行后补记“已批准”。

## 8. 分阶段交付与验证对象

### 8.1 阶段状态

| 阶段 | 本次核验状态 | 下一步 |
|---|---|---|
| Phase 2.1 Codex 数据面 | 有实现和测试资产；真实回合门禁存在排除项 | T6 恢复证据可信性，T7 验证回合与交互 |
| Phase 2.2 DSH capability | 后端首版存在；有 5 项旧测试失败；上游插件证据不足 | T1、T5a/T5b、T6 |
| Phase 2.3 Studio 插件 UI | sandbox API 存在，前端未接线 | T2/T3/T4、T5c、T6b |
| 会话体验收口 | 两分支已有大量修复，不能据此假定全部完成 | T7/T8 跨形态复验与针对性修复 |
| 联合发布候选 | Web 来源证明滞后、Python 构建版本未统一 | T9 冻结并验证，维护者 review 后发版 |
| Phase 2.4 及生态扩展 | 后续建议 | 不作为本版前置依赖 |

### 8.2 Codex 数据面验收对象

| 对象 | 必测内容 | 证据限制 |
|---|---|---|
| manifest | snake/camel 输入、冲突字段、类型错、路径逃逸、symlink | parser 测试不证明实际发现或执行 |
| skills | 两个实际选用的上游 skill 样本，发现、选择、触发 | 自制 marketplace fixture 保留为确定性测试；须标明其来源 |
| stdio MCP | 列举、注入、调用真实结果、失败恢复 | 不只断言 RPC 方法被调用 |
| hooks | 解析/快照/lock 完整；不受支持时明确阻断 | 不宣称执行已交付；若升级上游，重查 trust 支持再立项 |
| 来源 | 宿主解析后的 local/git/npm 坐标与 immutable lock | 与下载能力分开记录 |
| 回滚 | 失败安装不破坏已有 inventory/选择 | 重新开启真实 failed-install 用例 |

### 8.3 DSH 双插件样本

| 样本 | 选择要求 | 完成条件 |
|---|---|---|
| A：工具插件 | 实际存在、可取得不可变版本与许可证的上游 Cordis 工具包；不修改包源码 | 安装→启用→发现→绑定→Agent 调用→结果与取消/错误；同一包可在其上游支持的宿主运行 |
| B：UI 插件 | 实际上游带 client UI 的包，提前列出 Cordis/client-runtime/浏览器/Electron 依赖 | 安装后声明式入口出现，iframe 渲染，UI 调用经过宿主，卸载与失效回收 |
| 本地 fixture | 现有 `@ksadk-test/dsh-node-tool-plugin` 与 sandbox fixture | 验证协议、错误路径和 CI 确定性，不代替 A/B |

包名如 `@wegent/dsh-ui-git` 仅作为原调研候选，不意味着已经公开发布或能在当前浏览器运行。T5a 先完成样本可行性探测，避免把缺失宿主 API 的风险拖到前端收尾。若为兼容补充宿主适配，必须是通用 API，不写插件名特判。

### 8.4 两条 UI 通道

- 工作台插件：DSH client bundle + sandbox + 声明式 slot + 受控宿主中继。
- Agent 输出：ConversationItem / Interaction / A2UI 校验后由 `ksadk-web` 渲染。

可共享视觉基础、主题和受控 interaction callback；不能让第二条通道装载第三方可执行脚本。Shadow DOM 只能隔离部分样式，不可替代 iframe 安全边界。

### 8.5 延后交付的独立应用形态

可在将来承载多面板和较长生命周期，但 Store capability、持久化目录隔离、自有后台进程管理都要独立设计。当前不能宣称“所有应用数据已通过 ksadk:// Store 强制托管”。本版只保留稳定 extensionId、session 生命周期与受控通信的演进空间，不提前实现通用应用平台。

### 8.6 任务顺序

```text
T0 基线核对
  → T1 旧测试修复 + T6a 门禁真实失败/skip 语义
  → T5a 上游样本可行性与依赖清单
  → T2 sandbox 宿主与授权生命周期
  → T3 声明式扩展点消费 → T4 顶层加载退役
  → T5b 工具 Agent 闭环 / T5c UI 上游闭环 / T6b CI 接入
  → T7 共享会话与交互验收 → T8 性能与主题验收
  → T9 两仓制品候选 → reviewer 验收 → 维护者发布
```

T5b 的后端部分不依赖 UI sandbox，可在 T5a 后提前完成；T7 的独立回归也可早发现问题。本图是依赖顺序，不要求启动多个 agent。避免同一工作区并发修改相同文件。

## 9. 长期迭代机制（不阻塞本版）

### 9.1 上游跟踪

| 节奏 | 内容 | 产出 |
|---|---|---|
| 每次候选发布前 | 实际使用的 Codex/DSH/Cordis/client runtime 版本、支持的 OS/架构、相关协议变化 | 版本矩阵、变更摘要、对应 fixture/真实 E2E 结果 |
| 依赖升级时 | 比较本地上游镜像 commit；探测现有 manifest/RPC/工具/UI 宿主 API | 兼容差异，不静默跟随 latest |
| 定期轻量检查 | 可选每周检查版本/重要变更，仅形成升级建议 | 待评估列表；不自动升级或自动发布 |
| 每季度 | 评估架构边界、弃用项、独立应用/新 seam 是否有实际需求 | 一页决策与下阶段范围 |

Codex 关注 plugin/marketplace RPC、数据面、hook trust；DSH 关注 CLI/Profile、Cordis、tool 注册、client runtime 和发行物；wework 作为实现与交互参考，不引入其私有宿主协议。具体快照写到 `docs/upstream-notes/`，记录日期和 commit。

### 9.2 契约治理与 conformance

- schema 改动同步正例、负例及必要旧版本样本；受影响编译产物要有迁移说明与再验证。
- conformance fixture、真实上游样本、故障注入样本分别标识。录制数据脱敏，不提交真实 workspace、用户内容或凭证。
- RPC/模型录制回放能证明适配行为，不替代真实二进制的回合验证。
- 不要求每个插件声明 KsADK 内部全局 aggregate digest。插件声明其上游运行环境/API 依赖，宿主负责锁定与内部协议适配。
- 新 capability 优先复用现有 seam；新增 seam 要有真实调用方、生命周期、权限、失败和兼容设计。

### 9.3 自研插件交付建议

长期可建立独立 Node workspace，将金山云领域工具等低耦合能力先做成原生 Cordis 或 Codex 格式插件，再逐步迁移合适的存量能力。仓库名、npm scope、公开/私有 registry 和模板命令在建立前确认，不把本提案中的候选名称当已存在资产。

插件独立版本 + 精确 lock/integrity；既验证 KsADK 宿主，也验证声明支持的上游宿主。需要 KsADK 私有服务的插件要如实说明依赖，不能宣传为所有上游宿主直接可用。

session/transcript、审批决策、事件总线不迁出宿主。CodexRuntimeAdapter 逐步将 Provider 专属实现下沉，但只在实际职责重复或测试困难的具体边界切分，不做为了行数下降而重写。

### 9.4 度量

跟踪安装/装配成功率、冷启动、桥接额外耗时、工具 P50/P95/P99、崩溃、取消收敛和 session 泄漏。不要用“高于进程内工具 2 倍”作为通用退出阈值：对极快工具比例会失真，对慢网络工具又会掩盖桥接开销。分别测绝对桥接开销和工具自身耗时，依据实际交互预算决定优化或迁移。

## 10. 本版用户体验约束

这些约束来自本轮用户反馈，T7/T8 直接按此验收，不再自行设计第二套风格。

1. Studio 工作台和会话列表保持简洁；会话列表不添加品牌、调试指标或多余介绍。
2. 输入区域、审批/问题卡的外层左右边界一致，使用紧凑圆角矩形、实色背景、细边框；取消黄色大警告卡和大面积绿按钮的默认审批风格。
3. 审批偏好菜单为三个紧凑选项行；“上传附件 / 计划模式 / 设定目标”也为三行。说明作为同一选项的辅助文字或 tooltip，不各自套一张卡变成六行。
4. 模型选择参考用户给出的列表式弹层：直接显示模型列表和选中态，避免“模型→模型”的冗余嵌套；实色背景、合适层级，Portal 下也不透明穿透。
5. 发送后立即展示轻量呼吸占位，不能要求收到第一条 SSE 才有反馈；不在会话中央插入突兀的“正在连接 Agent”文字。
6. 收到真实 reasoning/思考状态后，显示带清晰流光动效的“正在思考”；内容输出后展示内容，不额外插入运行时长/ev/token 调试胶囊。
7. 已离开最新位置时显示圆形回到底部按钮：正在流式输出用三个跳动点，非流式用下箭头；在最新位置隐藏。点击或成功接受新的发送动作后滚到最新，用户主动向上阅读时不被持续拉回。
8. 审批支持明确选项、多题/单选/多选和自定义反馈。自定义反馈是一种独立语义，例如“请改成 echo 你好”不能批准原命令。
9. 自定义输入 Enter 可提交，Shift+Enter 换行；输入法 composition 阶段 Enter 不提交。空答案和重复提交被阻止。
10. 失败/取消/完成后不再展示可操作的过期审批卡；历史可以保留紧凑结果记录，不能留下空白框或刷新后再次待审批。
11. 主题、尺寸、间距、字体、背景与动效遵从共享包覆盖机制。遵守 reduced-motion，关闭动效时仍有可读状态；普通模式下不得被 Studio 全局 CSS 意外取消动画。
12. 模型、目标、计划与审批配置的可见性由真实 capability 决定。暂不支持的能力说明原因，不显示可选但永远不能生效的控件。

## 11. 当前基线的核验记录

本节是 **2026-09-05 的基线记录**，实施后应追加对应提交的结果，不把这里的失败提前改成通过。

| 项目 | 核验结果 | 处理任务 |
|---|---|---|
| Python 工作区 | `893730c4`，干净；已包含贡献者 main 与本地 master | T0 重新确认 |
| Web 工作区 | `52fa2dc`；跟踪文件干净，两个未跟踪位置需保留 | T0/T9 独立干净候选 |
| DSH 回归 | `uv run pytest -q tests/plugins/test_dsh_plugin_bridge.py tests/plugins/test_dsh_source_policy.py`：**25 passed / 5 failed** | T1 |
| 失败原因 | 两个错误文案断言过时；三个测试以未固定版本包名安装，先被新来源校验拒绝 | 更新 fixture/断言，不放松策略 |
| Codex 原生发布门禁 | `run_release_test_gates` 通过 `-k not ...` 排除两个真实回合/回滚用例，即使已设置 E2E 环境变量 | T6a |
| DSH 发布门禁 | 失败被作为 0.8.3 advisory 捕获，函数最后统一返回 `passed` | T6a；不能沿用到 0.8.4 |
| 新 capability host E2E | 文件存在，但不在当前 `MANAGED_DSH_TOOLCHAIN_TESTS` 列表 | T6b |
| Studio responsive smoke | Makefile 命令带忽略失败前缀，非阻塞 | T6a/T8 |
| UI sandbox | REST/session store 有实现；React 没有 `ui-sessions` 消费；独立浏览器 fixture 明确不挂真实 Studio API | T2–T5 |
| 版本输入 | Python 为 0.8.3；Studio dependency 为 Web 0.3.5；Makefile/CI/release-check/publish 默认仍取 Web 0.3.4 | T9 |
| Web 正式来源校验 | `RELEASE_PROVENANCE.json` 指向 `66d30a6...`，后续源码已改变；正式 check 实际失败 | T9 冻结后再生成 |
| 公开 registry | 当日目标 npm 0.3.5 与 PyPI ksadk 0.8.4 查询为 404 | 发布前重新查，不能据此永久认定可用版本号 |

原方案曾记录 317 项通过等局部测试数字。这些历史结果不作为当前发布凭证；本方案没有重跑全部 E2E、全量测试或云端部署，也没有据此宣称所有会话问题已经修好。

### 11.1 codex review 后的修复记录（2026-09-05，提交待定）

针对 codex review 的 Standards/Spec 阻塞项，本轮修复：

| 阻塞项 | 修复 | 状态 |
|---|---|---|
| Standards #1 E2E 用 sync Playwright | 改用 `async_playwright` | ✅ |
| Standards #2 生命周期测试未触发销毁 | 补真实 unmount + `onChannelDisposed` 断言 | ✅ |
| Standards #3 `DshUiSessionProvider` 未用 | 删除 | ✅ |
| Spec #1 生产握手 postMessage 目标错 | 改 `'*'`（opaque-origin 唯一可达，接收方仍校验 source+origin） | ✅ |
| Spec #2 授权边界未收窄 | 后端从 `dsh.client.tools` 声明服务端计算 allowed set，前端只收窄 | ✅ |
| Spec #3 session 到期无重建 | `DshUiSandboxFrame` 提前 60s 触发 `onSessionExpired` | ✅ |
| Spec #4 单插件故障阻断 | 改为跳过+降级 tab，不抛错 | ✅ |
| Spec #5 upgrade + scoped routing | `server.on('upgrade')` 已接；per-route scoped token 收窄已实现（scoped token payload 增 `routes` 白名单，非 root 且无 routes 的请求被拒） | ✅ |
| Spec #6 上游插件激活 | 换用 `@npm_thanks-for-forest/my-dsh-tool@0.1.2`（纯 tool 插件，无 settings 依赖），E2E 通过：`read_file` 工具真实调用返回 `upstream-roundtrip`。bridge 新增无 `dsh.bundle.patch` 插件的自动 patch 生成 | ✅ |
| Spec #6 发布门禁 | DSH advisory 改 blocking；新 E2E 进 preflight | ✅ |

未解决：无。两个 P1 阻塞项（授权边界、上游插件激活）均已修复并 E2E 验证。

## 12. 可执行任务清单

每项使用“现状 → 变更 → 验证 → 交付”的格式。新增文件名可以按现有工程风格调整；禁止通过检查源码中是否出现某个字符串，代替关键行为测试。

### T0. 接手与证据基线

**入口**：两个仓的 Git 状态、`AGENTS.md`、本方案 §11。

- 记录两个 HEAD、分支、远端包含关系、dirty/untracked 清单、Node/npm/uv/Python/Codex/DSH 实际版本。
- 对照此方案新增 commits，标记哪些问题已由其他人修复；相关验证通过后复用，避免重复实现。
- 确定测试 workspace 与测试 Agent，真实输入和 token 不入 fixture。不要使用用户已有会话做破坏性测试。
- 工作报告记录 `baseline_python_commit`、`baseline_web_commit`；后续测试都绑定最终候选或明确的中间 commit。

**交付**：一份基线表、实施任务状态表；不修改用户或其他 agent 的工作树内容。

### T1. 修复 DSH 旧测试与来源边界

**入口**：`ksadk/plugins/bridges/dsh.py`、`tests/plugins/test_dsh_plugin_bridge.py`、`tests/plugins/test_dsh_source_policy.py`、`tests/studio/test_dsh_plugin_api.py`。

- 正例改为与 fixture manifest 相符的 `package@exact-semver`；Runner 的参数匹配同步，确保测试真正进入安装/回滚分支。
- 两个 `file:/link:` 拒绝用例更新为当前语义断言；继续验证 Git/tag/range/相对路径、换行与凭证化 URL 等非法源。
- `failed_first_install` 要再次证明失败后新建 Profile 清理；unmanaged directory 用例要证明用户目录没有被修改，不能只断言提前抛 ValueError。
- 保留 Codex 测试插件版本 `0.1.0`；SDK、Studio 私有 package、插件 fixture 是不同版本对象。

**验收**：原 5 项失败修复；新的版本政策仍严格；安装、禁用默认值、更新失败恢复和用户目录保护的行为断言通过。

### T6a. 先修门禁的真实性

**入口**：`scripts/phase2_release_preflight.py`、`tests/packaging/test_phase2_release_preflight.py`、`Makefile`、`.github/workflows/ci.yml`、`.github/workflows/release-check.yml`。

- 删除针对本版必需用例的 `-k not ...` 排除、advisory catch、Makefile 忽略错误前缀；同时定位原 CI 环境问题，而不是单纯恢复后把红灯留给维护者。
- Codex 二进制/Profile/marketplace fixture、DSH 工具链、PATH、可执行权限、Node 平台版本先独立预检；`exit 127` 输出工具路径和可操作错误，日志脱敏。
- 使用 pytest JUnit/结构化报告或等价机制，核对预期关键用例实际收集并执行。某个测试进程 exit 0 但关键测试全部 skip/deselect/未收集，应失败。
- `failed`、`blocked`、`skipped`、`not-run` 不写成 `passed`。适配现有 evidence schema 和测试；确需新增状态，更新全部消费者，不只改日志。
- 加故障注入测试：DSH 子命令失败必须非零；原生 suite 被跳过不能生成可发布证据；某个浏览器门禁失败不能由后面的成功覆盖。
- 环境准备的网络下载可有限重试；业务断言失败不能靠无限重跑挑一遍绿的。重试次数及初次失败保留在报告中。

**验收**：有一条成功路径和至少“测试失败/未执行/工具链缺失”三类非成功证据；必要检查真实阻止候选通过。

### T5a. 提前探测真实上游插件

**入口**：现有 DSH capability E2E、`tests/fixtures/dsh-node-tool-plugin/`、§2 上游线索。

- A/B 各记录包/源码来源、精确版本或 commit、integrity、许可证、入口、所需宿主 API、支持平台、所需外部服务。
- 优先选择不依赖真实账号和副作用的工具样本；验证不可用版本/入口问题，不能编造一个包名写进安装测试。
- 在上游宿主与 KsADK 宿主用同一包对照。Node/CLI/Cordis 均固定版本，开发机偶然全局依赖不得成为隐含条件。
- UI 样本先列模块 import/inject/IPC 依赖，判断是否需要 Electron/desktop API。缺失 API 应通用适配或记录不支持，不能只换成自制 fixture 后宣称兼容。

**验收**：样本可行性表和最小真实加载结果。若 B 的要求超出当前 sandbox 通信模型，先给出明确适配设计与范围评估，再开始 T2/T3。

### T2. 接通 UI sandbox 宿主、生命周期和调用授权

**现状**：现有 POST create / POST messages / DELETE 可复用；尚无续期 endpoint。UI 模型、签名和验证以 `dsh_ui_sandbox.py` 为准。

**主要入口**：`api_plugin_routes.py`、`dsh_ui_sandbox.py`、`dsh_capability_service.py`、`react-ui/src/dsh-runtime/`。

**建议职责拆分**（逻辑模块，可合并小文件）：

- API client：创建/撤销 session、转发消息、规范错误、AbortSignal。
- session controller：绑定 plugin/clientDigest/Agent/generation，维护创建、握手、有效、失效和销毁。
- iframe surface：只负责 frame、MessageChannel 与轻量状态 UI。
- contribution adapter：把服务端的安全声明转换为现有 registry 条目，见 T3。

**实现要求**：

1. 复用 §13 的请求响应字段；未知字段/方法按 schema 拒绝，不由前端拼另一种工具协议。
2. 绑定具体 `iframe.contentWindow`、专属 MessagePort、服务端 sourceId 与 nonce。可信宿主页保存的来源值来自 create 响应，不能把插件消息自报的 sourceId 当鉴权证据。
3. capability token 仅留内存，经现有一次性握手送达；外层 Studio Cookie/CSRF 留在可信宿主，frame 内不获得。
4. React StrictMode、快速路由切换、关闭未完成创建时保持资源对称回收：abort fetch、关 port、移除 listener、撤销已创建 session。迟到的 create 响应立即撤销，不重新激活旧页面。
5. session hard TTL 到期采用**撤销后重建并重新握手**，不绕过硬期限。活动前按到期时间刷新会话；空闲页暂停，回到前台时确认有效再重建。未来若加 renew 需独立定义上限/吊销，不假定已有接口。
6. 更换 Agent、plugin disable/uninstall/update、clientDigest/descriptor/generation 改变均撤销旧 session。旧响应按 generation 丢弃，不回投新 frame。
7. 有在途副作用调用时不自动“重建 + 重发”；先取消或查询已知结果，状态未知要如实显示。不会因为网络重试执行两次。
8. HTTP 非 2xx 与 `ok:false` 都进入统一错误路径；无效授权禁用面板动作并给出重新打开入口，不无限白屏或 spinner。
9. 核实并补齐 §7.3 的服务端 tool 授权与审计；需要人工确认的 UI 调用复用宿主 interaction。未支持这类操作时明确拒绝，不把 token 校验当批准。
10. 不改变现有 iframe 限制来迁就插件。若需要兼容 loader，仅在受控 frame/client runtime 内实现；禁止顶层注入。

**验证**：fake clock 测 hard/idle TTL，React 渲染测关闭/切换/双挂载，API 集成测撤销和 generation，真实 Studio 浏览器测 token 不入 DOM/URL/持久化/日志、跨 frame 请求拒绝、实际 tool 结果返回。

**交付**：状态转移表、实际接口字段差异（无差异也注明）、授权入口说明和端到端证据。

### T3. 消费 extension points，管理动态入口

**入口**：`studioContributions.ts`、`studioDshCompositionHost.ts`、`DshWorkspaceSurface.tsx`、`App.tsx`、插件管理页。

- Registry 已有 7 种 slot，但 create API 当前仅投影 navigation/route/workspace.tab 三类；本版先闭合三类，不扩展为任意可执行注册。
- 现有 workspaceTab 要求 React Component，API renderer 是 `sandboxed-iframe` 描述。由可信 adapter 包装为现有 Component，不能把服务端 JSON 当函数或动态 import URL 执行。
- 安装/启用成功后刷新插件元数据，注册稳定 extensionId。入口发现与活动 frame session 分开：按需打开时创建 session，不能每次 rerender/轮询给所有插件创建 session。
- route/path 必须处于允许的扩展命名空间，ID 冲突/非法声明明确失败；一个插件失败不能清空内建 Provider、其他插件或整个工作区的 registry。
- 禁用/卸载后撤销该插件的注册和授权；若当前路由已失效，回到稳定插件页或工作区首页，不触发循环跳转。
- 更新采用 staging→成功切换→释放旧资源；支持重载不累积 event listeners、tabs 或 sidecar 进程。

**验收**：安装与卸载时入口正确出现/消失，刷新恢复，快速切换稳定；没有任何具体插件名称的 UI 特判；其他 registry slot 的旧消费者保持工作。

### T4. 退役 Studio 顶层插件代码执行

**入口**：`studioDshCompositionHost.ts` 的 `loadDshClientBundle` 和相应测试、`select_dsh_client_bundle_execution`、旧浏览器 smoke。

- 删除或迁移从 Studio 主文档 `document.head.append(script)` 加载插件的路径，改为 T2/T3 的隔离渲染。
- 先列出现有消费者和宿主 API 依赖；新的 sandbox 不是天然兼容超集，迁移不能假装旧功能都自动可用。
- 若上游 client bundle 仍使用 `__ModuleLoader__`，其兼容实现只允许在隔离 runtime 内存在。验收检查“顶层不可执行插件”，不要求粗暴删除全仓同名字符串。
- 后端 legacy 显式 opt-in 只在确有受支持消费者且有迁移设计时暂留，默认仍关闭；无人使用时清理对应路径/测试/文档。
- 检查 public exports、打包入口与旧示例，避免源码删掉但发运包仍引用旧路径。

**验收**：真实浏览器验证插件脚本不能读取/修改 Studio 顶层 DOM；上游样本有正确隔离运行结果或明确不支持原因；无全局污染和重复菜单。

### T5b/T5c. 真实插件的最终闭环

- **T5b 工具**：通过 Studio REST 安装精确 registry 包→启用→面板选择兼容 Provider 绑定→编译→新会话实际触发→工具结果/错误/取消可见→刷新恢复。后端 CLI 本地路径安装只能补充开发测试。
- **T5c UI**：同一真实上游 B 包→安装/启用→动态入口→真实 iframe→工具调用→关闭/重开→到期/更新→禁用/卸载。必须挂真实 Studio 路由和前端，不能使用另写的 HTTP fixture 服务器替代。
- 原有独立 `dsh_ui_sandbox_browser_e2e.py` 保留为隔离协议浏览器测试，不称后端单测，也不称产品 E2E。
- A/B 各保留包来源、版本、运行路径、调用证据、失败样本和限制。测试不需要真实业务写入或发送消息。

**验收**：成功不仅是 tab 存在或方法被调用，还要断言工具实际结果、权限/取消终态与刷新后的状态。

### T6b. 把新增闭环接入 CI

- 将 `tests/plugins/test_dsh_capability_host_e2e.py` 与 T5 新用例加入明确的必需 suite；环境变量由 job 显式设置。
- Codex、DSH、Studio 产品浏览器分组可以独立运行，但汇总必须等待所有本版必需项，并绑定同一候选版本与依赖基线。
- CI paths 覆盖 `contracts/plugin/v1/`、UI sandbox、发运 Node 资产、测试 fixture、Web 消费 lock 与发布脚本；避免只改契约不触发门禁。
- Web `release:preflight` 当前默认包含 conversation/demo E2E。补充或明确调用本版要求的 interaction/reconnect suite；每项新增门禁都应解决实际验收风险。
- publish workflow 若保留轻量预检，必须引用同一发布提交的完整成功 evidence；不能假定“以前某次 PR 检查绿过”。

**验收**：本版关键用例不被排除、skip 或 advisory 吞掉；失败能阻止合并/候选放行。

### T7. 会话、审批、反馈与共享组件收口

**入口**：Web `src/core/{conversation,run,stream,interaction}/`、`src/hooks/useSessionLifecycle.ts`、`src/components/chat/`；Python `shared_web.py`、`cloud_shared_web.py`、`codex_run.py`、`codex_agent_service.py`、实际事件持久化/API 入口。

- 按 §14 回归全部用户问题，先复现并关联真实事件与 API；已修复且通过的路径只补缺失验收，不整体重写。
- 统一 live/replay 的身份与投影，不能按正文字符串去重导致合法相同消息消失；工具开始/结果用 callId 合并，思考与正文保持原顺序。
- 用户消息使用稳定临时关联 ID 显示并在创建会话/持久化成功后对账；过时列表回填不能覆盖本地新会话。这里的“乐观显示”仅指先显示已提交输入，不伪造服务端成功。
- sessionId/runId/目标 + 请求 generation 共同隔离异步结果；快速 A→B→A 切换、首次新建与后台 hydrate 都必须防串会话。
- 审批偏好从 UI 到 runtime effective policy 实测；完全访问、风险确认、请求批准各自按语义工作，不借关闭审批让测试过。
- 自定义反馈、多选、编辑请求均使用共享 interaction 类型，见 §13.4；“请改成 echo 你好”明确不能批准旧命令。
- `[Errno 32] Broken pipe` 追踪 subprocess/stdin 生命周期、请求是否已被接受、run 是否终态。修失效 transport 和 durable 状态，不只隐藏异常。
- runtime 失败/完成后不恢复待处理旧卡；网络暂时失败时不能无依据清除仍有效审批，先查服务端状态。
- Codex Provider 不保留 KsADK 内置 Tool 选择或旧 Draft 绑定；对不支持组合给出创建前/编译时明确提示。
- 用共享组件解决卡片宽度、三行菜单、实色模型弹层、动效、滚动与空白/重叠，Studio 不新增平行 renderer。

**验收**：§14 的四种消费形态与核心场景通过；CSS/JS 源码通过不替代真实浏览器检查。

### T8. 性能、动效和主题

**入口**：Web 会话 hooks、缓存、message mapper、virtualizer/scroll、`motion.css`、`embed.css`；Studio bootstrap/目标选择/API 调度。

- 先按 §15 分段计时，确认耗时出在资源加载、API、事件回放、React 渲染或模型/运行时。
- 目标 metadata 与 sessionId 切换分离；切会话不卸载整套 shell、不重复 bootstrap 整个 Agent。
- 最近消息/缓存先显示，canonical event hydration 后台完成；更早内容上滑按 cursor 加载，增量补全，不每次拉完全部事件才能显示。
- 流式到来只更新活动消息/块；避免每个 token 重放全历史、重新解析全部 Markdown、重排整个列表。
- virtualizer 使用稳定 key 与测量高度；展开思考、工具结果、图片/字体加载触发相应测量，防止截图中的重叠和空白占位。
- 两个实际消费者验证主题：Studio 简洁主题 + 独立自定义消费者。Portal 弹层也要继承/传递 CSS variables，不能只在聊天局部容器设置颜色。

**验收**：计时报告、可见动效录制、全尺寸截图与受限网络场景；达到 §15 预算或给出不能达到的实测原因与决策请求。

### T9. 两仓发布候选准备

**入口**：两仓版本/lock、Python Makefile 与 workflows、Web provenance/preflight、Python public release workflow。

- 先冻结代码和依赖，构建候选 tarball 并在干净消费者中安装；不复制 `dist-lib` 到已有 node_modules 充当发运验证。
- 对齐 Python 构建/CI/发布使用的 Web 版本，详见 §16；源码、registry、缓存、wheel 中资产必须相符。
- 生成来源证明前先提交冻结源码；来源证明单独提交后不再混入未重新验证源码。
- 候选阶段标明“未发布”；版本号/CHANGELOG/公开提交/tag/npm/PyPI 动作遵守 AGENTS 与维护者授权，不能凭本计划直接发布。
- 准备 §17 的 reviewer 交付包；review 通过后才进入正式发布顺序。

**验收**：可复现候选、完整测试结果、无虚假成功、没有遗漏环境限制或未处理失败。

## 13. 接口、状态与关键时序

### 13.1 UI session API：复用字段，不重新定义

下表对应基线中的真实 API；未来若修改，必须同时更新实现、类型、fixture 和本表。

| 请求 | 输入 | 成功响应/语义 |
|---|---|---|
| `POST /api/v1/plugin-ecosystems/dsh/ui-sessions` | `pluginId`、`clientDigest`、`toolIds`、可选 `agentId` | 201，返回 uiSessionId/sourceId/expiresInSeconds/protocolVersion/descriptorDigest/inventoryDigest/allowedTools/handshake/frame/extensionPoints |
| `POST .../ui-sessions/{id}/messages` | 外层 `sourceId`、`frameOrigin: "null"`、`message` | envelope 中 `ok:true/result` 或 `ok:false/error`；鉴权、失效等也可能 HTTP 非 2xx |
| `DELETE .../ui-sessions/{id}` | 路径 session ID | 204，撤销 session 并处理在途资源 |

现有 request envelope：`protocolVersion: "agentkit.dsh-ui/v1"`、`kind: "request"`、`sessionId`、`capabilityToken`、`sourceId`、`requestId`、`method`、`payload`。

| method | payload | 注意 |
|---|---|---|
| `listTools` | 空对象 | 只返回该 session 获准工具 |
| `callTool` | `callId`、`toolId`、`arguments`、`deadlineMs` | 不授予其他插件/Agent 的工具；执行结果不能靠 requestId 到达顺序对账 |
| `cancelTool` | `callId` | 限定本 session 自有调用 |

create 的 `toolIds` 与内部 tool ID 正则目前限制不完全相同。T2/T5a 应用真实上游名称验证边界，若合法上游名称被拒绝，统一受控规范与投影，而不是放开任意字符串。别名映射要稳定且可追溯原始工具名。

前端不得假设接口存在 renew。续期/重建采用 §12 T2 约定。`expiresInSeconds` 是授权有效期，不是用户聊天 session 的有效期；UI session 与 conversation session 是两个不同对象。

### 13.2 UI session controller 的建议状态

这是前端控制器内部状态建议，**不新增 wire 枚举**。

```text
idle → creating → handshaking → ready
              ↘ failed          ├─ 到期/禁用/更新/目标变化 → revoking → closed
                                 └─ transport/generation 异常 → stale
stale → 清理旧授权/port → creating（仅恢复面板，不重发副作用工具）
任意状态 → 页面卸载 → 清理资源 → closed
```

| 事件 | 必须动作 | 不能发生 |
|---|---|---|
| create 完成时页面已关闭 | 立即撤销迟到 session | 重新出现 tab 或后台保留授权 |
| frame 重载 | 新握手；旧 port 关闭；旧 nonce 不复用 | 两个 port 同时操作同一授权 |
| Agent/descriptor/generation 变化 | 清理旧状态并重新准入 | 使用新目标标签显示旧 Agent 的结果 |
| Idle/Hard TTL | 按服务端期限失效，用户再次使用时重建 | 前端轮询无限延长硬期限 |
| 429/并发上限 | 明确节流与受控重试 | 瞬间重发形成请求风暴 |
| 403/409 授权失效 | 停止动作，刷新有效状态或重新打开 | 反复重试同一个失效 token |
| 卸载/禁用 | 入口、授权、在途资源一致收回 | 已消失的插件还能在后台调用 |

### 13.3 会话状态与历史恢复

**身份**：target/agent、session、run、message/item、call、interaction、revision 各自用途不同；保留来源映射，不把 agent 名称、工具展示名或正文当主键。

**发送时序**：

1. 接受输入动作后立即在当前会话显示用户消息，产生稳定 client correlation；输入清空与发送中状态同步。
2. 若需创建 session，创建 promise 只由一个流程拥有；新会话标题/选择不能被较早的后台 list 覆盖。
3. 显示轻量呼吸占位，自动跟到最新。服务端确认 run 后关联真实身份；失败时保留用户输入和可操作错误。
4. 真实思考事件到达才切到“正在思考”流光。等待/工具执行/审批/文本输出分别显示对应状态，不能一直冒充思考。
5. 持久化事实回填时补齐当前项；相同 eventId/itemId 的重放幂等，合法重复内容仍可存在。

**历史恢复时序**：

1. 切换时立即更新选中项，从目标+session 作用域缓存显示最近内容；未命中显示局部加载占位。
2. 请求最近 message/item 页；并行或后台取得该页关联的 durable events。存在投影时先显示投影，再按身份补工具、思考、交互，不能整体清空替换。
3. 旧结果通过请求 generation/AbortSignal 丢弃。旧线程的慢网络不阻塞新线程渲染。
4. 向上接近页边缘时加载更早 cursor，已有页去重、同 cursor 合并在途请求；插入后保持首个可见 item 及偏移。
5. 分页跨一个 run/tool/interaction 边界时，按稳定 ID 在后续页补齐状态，不能把缺少 result 暂定为成功或重新执行工具。
6. 需要判断 pending interaction 时以服务端权威状态为准，不要求全量历史拉完才能清掉已终结的旧卡。

**分页约束**：页大小固定上限；返回/持有可靠 nextCursor/hasMore。某些历史服务仅提供 offset 或旧接口时，由共享 adapter 保持兼容，不在 Studio 增加另一套全量拉取渲染。未知事件保留审计/回放，但不制造空卡。

### 13.4 审批、多选与自定义反馈

现有共享 `Interaction` 有 interactionId/sessionId/runId/revision、requestSchema、status；提交使用 `action + response + expectedRevision + idempotencyKey`。现有 action 为 `approve/reject/submit/cancel`，**不要直接新增未被后端支持的 `feedback` action**。

| 用户行为 | 语义要求 | 与现有提交模型的关系 |
|---|---|---|
| 批准本次 | 只批准此 interaction 对应操作 | `approve`，不得扩展为整个 session |
| 本会话批准 | 仅在服务端明确提供该选项时允许；范围可审计 | 按该交互 schema/adapter 支持的值映射，不凭文案增加权限 |
| 拒绝 | 该操作不执行，模型收到拒绝事实 | `reject` 与明确拒绝响应 |
| 取消确认 | 结束本次等待，按运行时语义取消/继续 | `cancel`，不等于批准或工具已成功 |
| 自定义反馈“请改成 echo 你好” | 原操作不执行；文字原样反馈给模型，继续下一轮 | 当前卡片使用 `cancel` 携带 `response.feedback`；必须核实 adapter/backend 一路保留，不能只发空 cancel |
| 单选/多选/多题 | 只提交实际选中值，按 schema 约束校验 | `submit` + 结构化 response；不能转换为 boolean approved |

自定义反馈有两种可接受执行方式：Provider 能原生接收反馈并在当前任务继续；或宿主先明确结束旧等待，再以幂等用户反馈开启下一轮。选择哪一种必须由现有 Provider 能力决定，并在实现报告写清；两种都要求原始工具不被误执行、不重复追加用户反馈。

审批状态仍使用共享已有模型：`pending → resolving → resolved/cancelled/expired`，提交错误可进入 `failed`。这里要区分：

- **请求暂时失败**：不知道服务端是否已接受，保留待核对状态；重试同一逻辑提交使用同一 idempotencyKey，先读取权威 revision/终态。
- **旧 run 确认已失败/取消/结束**：其审批不可再操作，刷新也不能复活；宿主落 durable 失效/终态事实，前端只留紧凑历史结果。
- **请求已接受但响应丢失**：再次点击或刷新只恢复现有结果，不能第二次执行命令。
- **修改了答案后再次提交**：遵从最新 revision/幂等语义；不能用相同已接受请求标识偷偷提交另一种决定。

键盘、视觉、反馈原文和终态都进入 §14；不能只测“点批准后 API 为 200”。

## 14. 验收矩阵与回归用例

### 14.1 消费形态

| 标识 | 环境 | 必须核对的特别项 |
|---|---|---|
| L | Studio 本地创建的 YAML/Agent | 本地 persistence、Codex/所选 Harness、审批执行权限、CLI 启动 |
| C | Studio 创建并部署云端的 Agent | build/runtime/Provider readiness、会话持久化、真实执行与错误分类 |
| R | 远程 `ksadk deploy` 的 LangGraph Agent | 框架事件适配、interrupt/resume、历史回放能力 |
| H | Hosted UI 的历史 Agent | 老 Bundle/老事件接口支持范围，不要求为了新 UI 重建 |

同一交互行为在 L/C/R/H 按能力适配；若某历史协议从未支持某种审批，明确显示“不支持”，不伪造按钮。用例报告要写实际 runtime/version/transport 与 capability，不能四列都填写同一次模拟测试结果。

### 14.2 会话和交互用例

| ID | 操作 | 可观察通过条件 |
|---|---|---|
| C01 | 新建空会话并发送 | 只创建一个 session，用户输入立即出现，创建后不消失 |
| C02 | 快速连续创建/发送并让旧 list 晚返回 | 新会话仍被选中，不被旧列表替换；不串内容 |
| C03 | 发送后等待首个服务端事件 | 下一帧或预算内有呼吸反馈；没有静止绿条和全屏连接文案 |
| C04 | 真实 reasoning→工具→reasoning→正文 | 顺序正确、思考流光可见；同一回合不重复插入 agent 标题 |
| C05 | chunk 与最终 snapshot 同时含正文 | 同一内容项只更新一次；用户合法重复发送的同文消息不被删 |
| C06 | 工具开始、结果、嵌套/调度器工具 | callId 正确配对，参数/结果归属正确，无凭空的 tool 或 ID 卡 |
| C07 | 完成后刷新 | 用户消息、工具、思考和正文仍在，运行态转终态，无内容重叠 |
| C08 | 流式中刷新/断网重连 | 按实际能力恢复 run 或提示中断；不再次提交原用户输入 |
| C09 | A→B→A 快速切会话 | 点击及时反馈，无全屏目标载入；迟到响应不污染当前会话 |
| C10 | 长会话向上滚动、多次碰触页边缘 | 只加载缺失页；无重复请求/重复项；滚动锚点不跳 |
| C11 | 阅读旧内容时继续流式 | 不被强拉到底；圆形按钮为跳动三点 |
| C12 | 流式结束仍在旧位置 | 圆形按钮转下箭头；点击后到底并消失 |
| C13 | 在旧位置发送新消息 | 自动到新消息；恢复跟随；不等待长 history 请求 |
| C14 | 改审批偏好后触发对应风险工具 | 发出的 metadata 与 effective policy 一致，真正出现等待与卡片 |
| C15 | 批准一次并刷新 | 原操作只执行一次，卡片已收起，历史显示决策 |
| C16 | 拒绝/取消 | 原操作未执行；runtime 得到明确结果，不描述成网络故障 |
| C17 | 自定义输入“请改成 echo 你好”并 Enter | 原命令未执行；模型收到原文并进入下一轮；若新操作需审批则产生新 interaction |
| C18 | 输入法候选确认/Shift+Enter | 候选确认不提交，Shift+Enter 换行，普通 Enter 正确提交 |
| C19 | 多选、单选、多题前后切换 | 答案保留且按 schema 校验；不是统一 approve/deny 下拉框 |
| C20 | 连点提交/丢失提交响应 | 同一 idempotencyKey 防重，重新读取实际结果，不重复执行 |
| C21 | 提交前后 run 失败或 Broken pipe | 有真实错误分类；旧 transport 不继续写；旧审批失效且刷新不复活 |
| C22 | 只读设置下执行只读联网查询 | 文件权限、网络能力、MCP 策略分别决定结果；明确拒绝原因，无错误的“用户拒绝”归因 |
| C23 | Codex Agent 携带历史 KsADK Tool Draft | 创建/编译正确消除不支持绑定，不到运行时才泛化报错 |
| C24 | 云端 kernel 未就绪 | 展示真实 readiness/Provider 原因，重试受控；部署页只有一组对应按钮 |
| C25 | 模型/审批/加号菜单 | 实色弹层、正确层级，三行紧凑条目；键盘可用 |
| C26 | 展开工具/思考、加载图片、改变窗口尺寸 | virtualizer 高度更新，无重叠、空白大框或跳动 |
| C27 | 两个主题/Portal/深浅背景 | 文字可读，菜单不透明穿透；样式覆盖无需 fork renderer |
| C28 | reduced-motion | 信息完整且状态可感知，动效遵从系统设置 |
| C29 | 重复问题或两个工具输出相同字符串 | 合法相同内容均保留，仅相同身份事件去重 |
| C30 | 历史服务返回未知事件或缺失新字段 | 按兼容 adapter 处理，不破坏老 Agent 会话，不执行任意动态代码 |

纯前端用例可先用确定性 transport 测试，最终关键链路还需真实后端/Provider 证据。审批中的测试命令仅使用无害输出与临时测试目录，不能把部署生产或发送外部消息作为样例。

### 14.3 插件容器与发布门禁用例

| ID | 场景 | 通过条件 |
|---|---|---|
| P01 | 精确 registry 安装与更新失败 | 完整来源/版本，默认禁用；失败恢复已有 Profile |
| P02 | 非法源与用户目录 | 非法输入拒绝，用户原目录内容不变 |
| P03 | 真实上游 A | 包不改码，绑定后 Agent 实际调用成功 |
| P04 | 真实上游 B | 动态入口、真实 iframe、工具结果与权限事实 |
| P05 | 跨 frame/伪造 sourceId/重复 nonce | 请求拒绝，不能跨 session 用 token |
| P06 | 非授权 toolId/其他 Agent 工具 | 服务端拒绝，不能仅靠前端隐藏 |
| P07 | hard/idle TTL | 旧授权失效；重建后可用；不自动重放工具 |
| P08 | generation/clientDigest 更新 | 老 port/响应失效，旧 tab 状态清理 |
| P09 | disable/uninstall/route close/StrictMode | 无遗留 listener、授权或在途任务；其他插件继续工作 |
| P10 | sidecar 崩溃/旧 Node/缺 CLI | 明确错误、有限恢复，必需 E2E 非零退出 |
| P11 | 节流/超时/取消 | 有界处理，无无限 spinner 和请求风暴 |
| P12 | CI 人为失败/全 skip/未收集 | 不能生成 passed 发布证据 |
| P13 | npm tarball/静态资源缺失或来源错 | 包安装或来源门禁失败，不使用旧缓存兜底冒充新版本 |
| P14 | 旧 framework/Provider bundle | 支持的旧产物正常运行，不受独立 capability schema 误伤 |
| P15 | UI 风险操作 | 宿主授权与审计可追溯；不会因为共享 MCP 通道自动放行 |

### 14.4 已有验证入口与命令

命令在各自仓库根执行。此处是**实施者运行清单，不是已运行结果**。先读对应脚本的当前参数和环境要求；缺少条件时记录，不通过改变命令跳过必需验证。

Python 相关基线与服务测试：

```bash
uv run pytest -q tests/plugins/test_dsh_plugin_bridge.py tests/plugins/test_dsh_source_policy.py
uv run pytest -q tests/studio/test_dsh_plugin_api.py tests/studio/test_dsh_ui_sandbox.py tests/studio/test_dsh_capability_service.py
uv run pytest -q tests/plugins/test_dsh_capability_host.py tests/studio/test_dsh_agent_binding.py tests/studio/test_dsh_provider_registration.py
uv run pytest -q tests/plugins/test_codex_manifest.py tests/studio/test_codex_manifest.py tests/studio/test_codex_plugin_store.py
uv run pytest -q tests/packaging/test_phase2_release_preflight.py tests/packaging/test_phase2_release_candidate_gate.py
uv run pytest -q tests/contracts/test_phase2_schema_compatibility.py tests/compat/test_phase2_legacy_compat.py
```

真实二进制 E2E（准备固定版本工具链后）：

```bash
KSADK_DSH_TOOLCHAIN_E2E=1 uv run pytest -q tests/plugins/test_dsh_capability_host_e2e.py tests/plugins/test_dsh_node_provider_e2e.py tests/e2e/test_dsh_managed_toolchain_e2e.py
KSADK_CODEX_PLUGIN_E2E=1 KSADK_CODEX_PROVIDER_E2E=1 KSADK_CODEX_SUBAGENT_E2E=1 uv run pytest -q tests/e2e/test_codex_plugin_bridge_e2e.py tests/e2e/test_codex_provider_app_server_e2e.py tests/e2e/test_codex_subagent_provider_e2e.py
```

Studio 前端：

```bash
npm --prefix ksadk/studio/react-ui test
npm --prefix ksadk/studio/react-ui run test:ui
(cd ksadk/studio/react-ui && npx tsc --noEmit)
npm --prefix ksadk/studio/react-ui run build
```

类型检查在 Studio 前端目录执行。正式锁定前的 tarball 消费按 §16，发布后干净 `npm ci` 必须单独验证。

已有浏览器脚本：

```bash
PYTHONPATH=. uv run python tests/studio/e2e/dsh_ui_sandbox_browser_e2e.py
PYTHONPATH=. uv run python tests/studio/e2e/conversation_reconnect_browser_e2e.py
PYTHONPATH=. uv run python tests/studio/e2e/conversation_items_browser_e2e.py
PYTHONPATH=. uv run python tests/studio/e2e/studio_browser_smoke.py
PYTHONPATH=. uv run python tests/studio/e2e/studio_responsive_smoke.py
```

T5 新增的真实 Studio+上游插件 E2E 由实施者按现有命名放置，并在报告记录确切命令；不要引用一个尚不存在的新脚本作为已通过证据。

Web 相关验证：

```bash
npm ci
npm test
npm run test:node
npm run lint
npm run build:all
npm run test:e2e:interaction
npm run test:e2e:conversation
npm run test:e2e:reconnect
npm run test:e2e:demo
```

按受影响路径在开发过程中选择必要测试；冻结候选时运行全部必需门禁。最终正式 `release:preflight` 的覆盖范围以修正后的脚本为准，避免重复跑相同测试多次却漏掉真实插件/审批链路。

## 15. 性能测量与体验预算

### 15.1 分段计时

至少分别记录下列时间戳/耗时；不要只给一个从点击到完整答案的总时间。

| 指标 | 起止 | 用途 |
|---|---|---|
| 发送本地反馈 | 用户提交被接受→用户消息+呼吸状态 paint | 判断主线程/React 是否阻塞 |
| Session 创建 | create 请求发出→服务端响应 | 区分创建延迟与 UI 状态管理 |
| bootstrap | 目标解析请求→可用目标元数据 | 区分目标切换和会话切换 |
| 首事件 | run 请求发出→首个服务端事件 | 排队/建连/运行时开销，不能全归模型 |
| 首 reasoning / 首正文 | run 请求→对应事件 | 模型/Provider 阶段耗时 |
| 事件显示延迟 | 浏览器收到事件→相关块 paint | 前端处理和渲染开销 |
| 会话切换 | 选中会话→最近内容 paint | 缓存命中与未命中分别统计 |
| 上滑分页 | 发出下一页请求→插入并稳定滚动位置 | 网络、mapper、virtualizer 分别定位 |
| bridge 开销 | 进入宿主调用→sidecar 接收/返回→宿主完成 | 排除工具自身网络等待 |

可以使用浏览器 Performance API、网络记录、React profiler 与脱敏服务端日志。调试指标默认不出现在面向用户的对话胶囊里；日志关联 run/requestId，不输出 token 或用户全文。

### 15.2 建议验收预算

以下数字是**本版拟定预算，不是现有实测结果或对所有设备的 SLA**。以记录硬件/浏览器的参考环境验收；达不到时先给分段证据，禁止无记录地调大阈值。

| 场景 | 拟定预算 |
|---|---|
| 发送、点击菜单等本地反馈 | P95 ≤ 100 ms |
| 已缓存会话显示最近内容 | P95 ≤ 150 ms |
| 网络数据已到达→最近页可见 | P95 ≤ 100 ms |
| 流式事件到可见内容 | P95 ≤ 100 ms；允许一帧至短时间窗批量更新 |
| UI 单次后台任务 | 避免持续 >50 ms 主线程长任务；若出现记录来源与优化前后 |
| 上滑加载 | 锚点不跳；同 cursor 不重入；已知无更多页时不继续请求 |

未缓存会话的总耗时包含网络，不能用同一个 150 ms 标准判失败；模型首 token 也不能由前端保证。普通网络、延迟网络分别记录 API 和本地渲染耗时。

### 15.3 基准负载与验收方法

- 小会话：20 条可见项；长会话：至少 1,000 条消息/工具项与 10,000 条事件的确定性 fixture，包含长 Markdown、表格、多段思考和工具展开。
- 区分冷页面加载、热页面首次会话、缓存切换、流式长会话；不要只测试空会话。
- 每个热路径至少重复 20 次，报告 P50/P95、最大值、请求次数、响应字节与长任务；真实模型回合只需足够证明行为，确定性性能采样不反复消耗模型。
- 记录禁用缓存/正常缓存和网络模拟参数。CSS/JS 下载只在实际瀑布图中确认，不能用猜测解释切会话慢。
- 测量动效时检查 computed style、animationName 和运行截图/短录屏；CSS 文件有 keyframes 不证明动画没被覆盖。
- 不用定时输出假的思考字数、事件数或 token 数营造忙碌。没有真实 token usage 时省略对应数字。

### 15.4 优化边界

先解决重请求、全量回放、过期响应覆盖和 DOM 规模，再处理局部 memo/批处理。缓存键包含目标与会话，设置合理淘汰；不能把所有历史永久保留内存。重资源如图表/代码编辑器可按需加载，但不得将输入反馈和基础会话恢复卡在懒加载后面。

不把“虚拟化”本身作为成功标准：滚动锚点、动态高度、辅助技术阅读、工具展开和恢复位置需要一起通过。

## 16. 联合发布与制品一致性

### 16.1 版本来源清单

| 对象 | 必须对齐的位置 |
|---|---|
| Web 包版本 | Web `package.json`、lock、CHANGELOG、exports、`RELEASE_PROVENANCE.json` |
| Studio 消费版本 | Python `ksadk/studio/react-ui/package.json` 与 `package-lock.json` 的实际 resolved/integrity |
| Hosted 静态消费版本 | Python `Makefile` 的 `KSADK_WEB_VERSION`、构建输入、缓存和 static 校验 |
| CI/正式发布默认值 | Python `.github/workflows/ci.yml`、`release-check.yml`、`publish-pypi.yml`；检查其他出现的 0.3.4 默认值，历史说明不要盲目批量替换 |
| Python SDK 版本 | `pyproject.toml`、`ksadk/version.py`、发布文档/CHANGELOG；别名包同步策略按仓库规则 |
| 部署消费 | 实际 Hosted UI 构建/镜像、SDK wheel 内静态资产；各自保留源码与制品来源 |

### 16.2 候选阶段：先完成联合验收，再正式发布

1. 开发完成后提交两仓源码；若工作区有用户保留内容，从冻结提交建立干净候选，不删除原工作区内容。
2. Web 全部构建通过，生成来源证明。`source_commit` 指向冻结代码提交，之后仅允许来源证明提交；再改源码必须重新冻结/生成/验证。
3. 运行正式 Web provenance check 与候选门禁。正式证据不得使用 `--allow-dirty`、`--allow-unreleased` 或等价绕过参数。
4. 构建 tarball，记录 SHA-256、npm integrity、包内版本和 provenance；分别验证公开 subpath exports、类型、CSS 与 `dist-ksadk` 资产。
5. 在隔离的 Python/Studio 消费工作区，用同一个 tarball 构建 Studio 与 Hosted 静态资产。当前 Makefile 已支持 `KSADK_WEB_TARBALL`，优先复用。
6. 对 Studio dependency lock 不提前伪造公开 registry integrity；候选可使用独立临时消费者/隔离 lock，正式 npm 发布后再生成真实锁定结果。
7. 运行 §14 和干净 wheel/sdist 安装验证。真实云端验证记录 runtime/build/image 关联；缺少环境则记录未验收，不伪造 deployment evidence。
8. 交 reviewer 检查任务与证据。只有维护者批准后进入发布，实施交接不等于发布授权。

可复用的候选命令（路径为占位符，实际执行前替换；在相应仓库运行）：

```bash
# Web：冻结源码后，按现有脚本生成并提交来源证明，再做正式检查
npm run release:provenance -- generate
npm run release:provenance -- check
npm run release:preflight

# Web：产物输出到候选目录；npm pack 不是 npm publish
npm pack --pack-destination /absolute/path/to/candidate-artifacts

# Python：同一候选 tarball 同时用于 Hosted 静态与 Studio 构建
make build-frontend KSADK_WEB_VERSION=0.3.5 KSADK_WEB_TARBALL=/absolute/path/to/candidate-artifacts/kingsoftcloud-ksadk-web-0.3.5.tgz
make verify-ksadk-web-static KSADK_WEB_VERSION=0.3.5
```

生成命令会修改来源文件，必须按冻结/提交顺序执行；不能在一个未完成实现的分支上先生成证明再继续改代码。候选测试不代表 npm registry 上已存在该包。

### 16.3 正式发布顺序

1. 维护者确认候选与版本未被占用，通过可信发布 workflow 发布 Web `0.3.5`。
2. 从公开 registry 重新下载同版本包，核对 provenance/版本/exports/integrity 与已验收候选；正式 tarball 字节与候选若不同，检查差异，不能只比版本字符串。
3. Python/Studio 更新为 registry 的真实 lock；在无本地 tarball 覆盖、无手工 node_modules 的干净环境执行 `npm ci`、重新构建/校验静态资产。
4. 通过公开导出候选流程，运行 `make public-preflight`、必要的 `phase2-release-candidate-gate` 以及干净安装验证；证据对应最终公开候选提交。
5. review/门禁通过后由维护者按 `docs/public-release-workflow.md` 发布 `ksadk 0.8.4`，检查最终下载 wheel/sdist 的内容。
6. Hosted 部署升级另记录实际版本/镜像/就绪与历史 Agent smoke。npm/PyPI 成功不代表线上 Hosted 已升级。

Web 发布后若发现 Python 独有问题，保持 Python 未发布继续修，不为赶进度放过门禁。Web 已发布版本不可覆盖；发现共享包阻塞问题需明确申请后续修复版本，不能静默重传同版本。

### 16.4 证据、回滚与公开边界

- `phase2_release_preflight.py` 的 `--skip-tests` 只能生成不完整开发证据，不能用于正式通过。
- `phase2_release_candidate_gate.py` 已有 local/registry/deployment/preprod 输入；按其真实 schema 生成，不手填几个 `passed`。
- Python 内部 master 与公开 main 分开，clean export 中检查源代码/构建产物归属和公开内容；不把工作区内部目录、日志、账号或凭证带入公开仓。
- 发布前保留上一版消费者版本、可回退静态资产与已部署镜像记录；回退不能改写用户会话事实或已发运 bundle。
- 生产部署、GitHub Release、tag/push、npm/PyPI 上传均由维护者授权和仓库发布流程控制。本交接要求实施者完成可评审候选后停止发布动作。

## 17. 实施报告与下一轮 review 交付物

建议实施者维护一份 `docs/plugin-ecosystem-reuse-implementation-report.md`（**待实施时新建**）。报告保持简洁，原始大量日志放 CI artifact 或本地证据目录；提交到公开文档前脱敏。

### 17.1 必填报告结构

```markdown
# 插件生态与共享会话实施报告

## 基线与提交
- Python baseline/final commit、分支：
- Web baseline/final commit、分支：
- 上游 Codex/DSH/Cordis/插件 A/B 的精确版本或 commit：
- 未提交/未跟踪内容及归属：

## 任务状态
| 任务 | 状态 | 提交/主要文件 | 测试命令与结果 | 剩余限制 |
|---|---|---|---|---|
| T1 | pending | | | |
| T6a | pending | | | |
| T5a | pending | | | |
| T2/T3/T4 | pending | | | |
| T5b/T5c/T6b | pending | | | |
| T7/T8/T9 | pending | | | |

## 行为与接口变化
- 采用的 UI session 生命周期与授权入口：
- 自定义反馈如何终止旧操作并进入下一轮：
- schema/lock/旧 Bundle 的兼容分析：
- Studio 留下哪些业务代码、Web 新增/变更哪些公开入口：

## 验证
- 每条命令：执行目录、时间、commit、exit code、passed/failed/skipped/deselected 数量、artifact 路径。
- C01–C30 / P01–P15：适用消费形态、实际结果、未测原因。
- 性能：硬件/浏览器/数据规模/网络设置、P50/P95、优化前后。
- 浏览器：启动命令/端口、截图或短录屏、相关脱敏事件与后端日志位置。

## 产物
- Web tarball 路径、版本、SHA-256/integrity、provenance：
- Python wheel/sdist、内含 Web/Studio 版本与来源：
- 干净消费者/公开候选门禁结果：

## 待 reviewer 决策
- 未解决项、具体阻塞、建议下一步：
- 相对方案的变更及理由：
- 是否执行过发布/部署（默认没有）：
```

状态使用 `pending/in-progress/passed/failed/blocked/not-applicable` 或团队已有等价词；`not-applicable` 写明 capability 依据。`passed` 必须有结果，不用“代码看起来没问题”代替。

### 17.2 分批提交建议

推荐按行为组织提交：

1. DSH 来源测试修复。
2. 发布门禁真实性和工具链修复。
3. sandbox API/controller/授权生命周期。
4. registry 与安全加载迁移。
5. 真实插件/CI 验证。
6. 共享会话与审批、反馈、回显修复。
7. 性能和主题收口。
8. 经维护者允许的版本/依赖/来源证明与候选材料。

可合并紧密相关提交，不需要为了凑数量拆分。跨仓提交在报告建立对应关系；每批完成后 targeted 验证，最终冻结再跑必要全套。不要反复发布多个中间版本。

### 17.3 给本地 Claude 的交接任务

> 请按本方案在现有 Python `feat/studio-chat-message-list-0.3.5` 与 Web `feat/conversation-v1-unified-render` 分支继续实施。先读取各仓 AGENTS.md 和当前差异，保留用户与贡献者工作；贡献者测试插件版本固定为 0.1.0。按 T0→T1/T6a→T5a→T2/T3/T4→T5/T6→T7/T8→T9 推进，已有修复通过验证就复用。通用会话与审批 UI 保持由 ksadk-web 提供，Studio 只做业务装配。必须修复测试失败被写成 passed 的门禁问题，不能通过 skip、放宽权限/来源校验或修改测试语义掩盖失败。自定义反馈不得批准原命令，刷新不得恢复终态旧审批；真实插件和实际 npm/wheel 消费链路需要证据。完成后按 §17.1 写实施报告并按职责提交，仅启动已确认的本地测试 workspace/端口并保留运行入口供用户查看，然后等待 review；不自行发布 npm/PyPI、打正式 tag、合并公开主线或部署生产。遇到外部环境/能力限制，记录具体阻塞并继续其他独立任务，不虚构通过结果。

### 17.4 Reviewer 检查重点

- 首先核对最终 Git diff 与任务报告，确认本轮实现没有超出声明范围或覆盖贡献者改动。
- 查看失败/skip 的传播测试、真实二进制回合日志；确认 CI evidence 不再产生假通过。
- 重点审查授权边界、custom feedback、Broken pipe 后终态、旧 session 的迟到响应和 history 幂等。
- 打开真实 Studio 和 Hosted 消费者验证卡片/菜单/滚动与历史 Agent；不是只运行组件测试。
- 检查真实插件来源与未改码证据、UI 宿主 API 依赖、顶层代码执行是否退役。
- 核对 npm/wheel 内的字节、版本、公开入口与 provenance，再判断是否可以发布。
