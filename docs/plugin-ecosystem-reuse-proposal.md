# 提案：ksadk 插件生态复用与兼容架构（dsh Sidecar + Codex 数据面）

> **状态**：架构提案 / 已进入实现（Phase 2.1–2.2 首版落地于 commit `d0e6bab5`，见 §11 实现状态）  
> **作者**：ksadk runtime 团队  
> **对齐文档**：`docs/phase2-plugin-compatibility.md`、`contracts/plugin/v1/`、`AGENTS.md`  
> **目标**：以最小代价实现对 DeepSeek Harness (dsh) 插件生态与 OpenAI Codex 官方插件的深度兼容，同时明确两边协议本质、边界和落地路径。

> **协议版本说明**：整个插件架构仍在迭代、尚未对外发布，目前没有任何生产消费者。因此本文涉及的所有协议（含 sidecar 传输、capability seam、composition/lock 契约）**都属于插件契约 v1 的演进**——即使对现有契约做推倒重写式扩展，也仍记为 v1 契约集的内部迭代，**不存在、也不引入一个独立的 "v2 协议"**。只有等插件架构正式发布、产生真实消费者之后，才需要引入版本号概念来做兼容切分。（§5.3 记录了一次此原则的边界情形及裁决。）

---

## 1. 背景与核心判断

### 1.1 两个外部生态的协议本质

| 维度 | DeepSeek Harness (dsh) 插件 | OpenAI Codex 官方插件 |
|---|---|---|
| **协议本质** | **纯 TypeScript / Node.js 内存绑定**（无 wire format / IDL） | **两层协议**：控制面 = app-server JSONL RPC；数据面 = 文件约定 + 标准 MCP |
| **载体形态** | npm 包 / TS 源码，经 ESM `import()` 进主进程共享堆 | 磁盘资源包（`plugin.json` + `skills/` + `mcpServers` + `hooks`） |
| **契约机制** | Cordis IoC、TS Declaration Merging、`apply(ctx, config)` 声明 effect | manifest JSON（camelCase）+ 标准 MCP 协议 |
| **Python 进程内直接运行** | ❌ **不可能**（强依赖 V8 运行时特性与 Cordis 内存上下文） | ✅ **完全可行**（解析文件 + 启动 MCP server 均为语言无关） |
| **官方 Python 兼容做法** | 官方 `python/sdk` 自带 `deepseek-harness-runtime-bin`，以 **Node Sidecar** 进程运行 Cordis | 官方提供 stdio JSON-RPC app-server |

### 1.2 核心结论

1. **"复用 dsh 协议"是伪命题，真实目标是"复用 dsh 生态"**：dsh 没有语言无关的插件 wire protocol，在 Python 进程内重写 Cordis 无法直接运行 npm 上已有的 dsh 插件。
2. **唯一现实的 dsh 100% 兼容路径是 Node Sidecar**：同构 dsh 官方 `runtime-bin` 模式，由 Node 子进程承载 Cordis 宿主，Python 端经轻量 RPC 消费能力。
3. **Codex 官方插件无需 Sidecar**：数据面全是文件与 MCP，控制面 ksadk 已有 `CodexAppServerPluginBridge` 与官方协议逐字段一致，只需补齐数据面解析与装配。
4. **两边能力最终归一到 ksadk 的 Capability Seam**：宿主 PluginHost 只认自身契约，不直接感知外部方言。
5. **ksadk 不发明第三套插件协议**：官方自研插件也必须落在这两种生态形态之一——**Cordis/dsh 原生 npm 插件**（平台能力强 UI、强 Node 生态的）或 **Codex 格式插件**（文件包 + MCP + skills，轻量工具类的）。ksadk 侧的投入全部用于"做好这两种生态的宿主"，而非创造自己的插件格式。（§5.3 记录了宿主侧传输契约的一次例外裁决：宿主↔sidecar 的传输层允许按工程最优选择，它与"插件格式"是两个层面。）

---

## 2. 参考项目与 GitHub 仓库对照

本次架构设计以以下三个开源/同构项目为核心事实来源：

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
| **宿主通信协议** | 进程内内存调用 | HTTP (`/wework/executor/v1`) + Pipe | loopback MCP（实现裁决，见 §5.3） |
| **Codex 插件** | 桥接 hooks.json 到 agent loop | 前端解析 manifest，委托 executor 执行 | 前端/Bridge 解析 manifest，委托 App Server |

**关键结论**：ksadk 采用 **Node Sidecar 架构** 来兼容 dsh 插件，不仅是 Python 语言限制下的必然选择，**也是与同为 TS 生态的 wework 完全一致的生产级标准模式**。ksadk 与 wework 的架构骨架完全同构，仅在宿主侧的通信客户端语言（Python vs TS）上有所不同。

---

## 3. 总体架构设计

```
                         ┌──────────────────────────────────────┐
                         │   Studio / AgentBundle v2 / Draft    │
                         └──────────────────┬───────────────────┘
                                            │ CompositionCompiler
                                            ▼
                         ┌──────────────────────────────────────┐
                         │  CompositionProfile / PluginLock     │
                         └──────────────────┬───────────────────┘
                                            │ PluginHost (Python 微内核)
                         ┌──────────────────┴───────────────────┐
                         ▼                                      ▼
             ┌──────────────────────┐               ┌──────────────────────┐
             │  in-process Provider │               │   Sidecar Provider   │
             │   (Python Native)    │               │ (Language-Neutral)   │
             └──────────┬───────────┘               └──────────┬───────────┘
                        │                                      │ loopback MCP (§5.3)
       ┌────────────────┼────────────────┐                     ▼
       ▼                ▼                ▼          ┌──────────────────────┐
┌──────────────┐ ┌──────────────┐ ┌──────────────┐  │  dsh Node Sidecar    │
│ Builtin      │ │ Codex        │ │ Codex 官方   │  │  (Cordis 宿主)       │
│ Capabilities │ │ AgentProvider│ │ 数据面装配   │  │  - 真实 Cordis 运行时│
│ (Store/Ctx)  │ │ (复用Adapter)│ │ (MCP/Skills) │  │  - 加载 npm dsh 插件 │
└──────────────┘ └──────────────┘ └──────────────┘  └──────────────────────┘
```

---

## 4. 对四个核心问题的回答

### 4.1 建议是什么？（收敛与分工策略）

- **短期（P0）**：
  - **Codex 侧**：补齐数据面（`plugin.json` 双格式解析、local/git/npm 源物化、skills/mcp 装配），彻底打通 Codex 官方插件本地运行。
  - **dsh 侧**：在现有 dsh 契约（v1 契约集）上演进，继续走 Profile 级桥接与 `agent.provider/v1` 消费。
- **中期（P1）**：
  - 将 Sidecar 契约扩展为**多 capability 形态**（支持 tool 代理）——这是对现有 v1 契约集的直接演进，不是新起一套协议。
  - 打包最小化 Node Cordis Sidecar（复用官方 bundle 或轻量 runner），真正实现 npm dsh 插件在 sidecar 内运行并投影回 ksadk。
- **架构收敛**：
  - `CodexRuntimeAdapter` 的执行细节逐步下沉进 `CodexAgentProvider` 内部，kernel 层 `adapter.py` 退化为纯协议接口。

### 4.2 对目前的 Studio 有什么影响？

| Studio 模块 | 现状 | 本提案影响 |
|---|---|---|
| **API 路由** (`api_plugin_routes.py`) | 已提供 codex/dsh 的 list/install/uninstall 等 REST 端点 | **几乎零破坏**：沿用现有端点，数据面丰富后返回更全的 capabilities |
| **编译器** (`composition.py`) | 编译 `AgentDraft` → `CompositionProfile`，强制校验 plugin owner | **平滑扩展**：增加针对 dsh sidecar 提供的 capability 映射（如 `dsh://` 前缀资源） |
| **前端面板** (`React UI / Studio`) | 已有 Ecosystem 插件管理页 | **体验增强**：用户安装 dsh/codex 插件后，其暴露的 Tool/MCP 可直接在绑定面板被勾选 |
| **AgentBundle 格式** | v2 格式，基于 `composition.json` 与 `plugin.lock` | **零变更**：外部插件物化后仍作为标准 `PluginReference` 写入 lockfile |

### 4.3 dsh 插件如何被应用？

```
用户在 Studio 安装 dsh 插件 (如 @deepseek-ai/dsh-tools-websearch)
  → 写入 dsh profile (cordis.patch.yml)
  → Sidecar 启动并加载该 Cordis 插件
  → Sidecar ready/describe 暴露 capabilities (tool)
  → ksadk PluginHost 接收并注册到当前 Session 的 Tool Seam (mcp.connector)
  → Agent 执行时触发工具调用，经 loopback MCP 转发给 Sidecar 执行
```

### 4.4 是否还要安装 dsh loop 插件？

**不需要，且不应该安装。**
- **原因**：Agent Loop（思考-工具调用-上下文组装的主循环）归属于 **ksadk 自身或选定的 AgentProvider**（如 `CodexAgentProvider` 或用户 Python Agent）。
- 如果把 dsh 的 `agent-loop` 插件也装上，会导致**两个主循环竞争**（Python 一套 Loop、Sidecar 内一套 Loop）。
- **正确做法**：Sidecar 仅作为 **Capability Provider**（提供 Tools、Context），Loop 控制权始终保留在宿主端。只有当用户明确选择 "DSH 托管 Agent" 时，才由 Sidecar 接管完整 Agent Execution。（实现已遵守：sidecar overlay 只注入 capability-host 一个插件，全仓无 loop 插件引用。）

---

## 5. Sidecar 契约的多能力扩展设计

现有 `dsh-agent-provider-host` 契约固定 10 个方法、descriptor 锁死为单一 `agent.provider/v1` capability（`definition`/`slot` 是 schema 里的两个 `const` 字段，`methods`/`requests`/`responses` 均 `minItems/maxItems: 10` 硬编码）。为支持 Tool 投影，需在 v1 契约集内将其从单 capability 演进为多 capability 形态。

**先明确三层投影关系**（理解 digest 在哪一层算出来的前提，见 `providers/dsh.py` 头部 docstring）：

> sidecar descriptor 不是直接进 lock，而是**先投影成内部 `PluginManifest`**（descriptor 的 `definition`/`slot` 写进 manifest 的 `provides[]`，dsh.py:825-826），唯一目的是让现有 PluginHost 继续持有 admission / activation fencing / cleanup 所有权——dsh 插件作者不需要发布 ksadk manifest。digest 链因此是三层：**descriptor（`descriptor_digest()`）→ PluginManifest → PluginLock（`plugin_lock_digest()`）**。

演进涉及两层消息，注意不要混淆（现 schema 中根级 `contractFormat` 属于 transcript，`methods` 属于 handshake 消息体，descriptor 是独立对象）：

**handshake 帧**（`$defs/handshake`，`protocolVersion` + `methods[]` + `hostVersion`）——方法从 10 个增补到 11 个（`call_tool`；`invoke_hook` 不进首期，见下）：

```json
{
  "protocolVersion": "ksadk.dsh-agent-provider-host/v1",
  "methods": [
    "handshake", "describe", "preflight", "activate", "inventory",
    "execute", "cancel", "health", "drain", "dispose",
    "call_tool"
  ],
  "hostVersion": "..."
}
```

**describe 响应帧的 descriptor**（`$defs/descriptor`，现 `definition: agent.provider/v1` + `slot: agent.execution` 两个 const 字段）——演进为 `capabilities[]` 数组，且**每个 capability 必须映射到已存在的 seam**：

```json
{
  "descriptorFormat": "dsh.agent-provider-descriptor/v1",
  "ecosystem": "dsh",
  "providerId": "dsh-bridge",
  "capabilities": [
    {
      "definition": "agent.provider/v1",
      "slot": "agent.execution",
      "id": "dsh-agent-bridge"
    },
    {
      "definition": "mcp.connector/v1",
      "slot": "mcp.workspace",
      "id": "dsh-tool-bridge"
    }
  ]
}
```

> 演进要点：handshake 的 `methods` 从 `minItems/maxItems: 10` 放宽并增补 `call_tool`；descriptor 从单个 `definition`/`slot` const 对扩展为 `capabilities[]` 数组。契约标识保持 v1（§文首协议版本说明），由发版门禁（`test_contract_digest_preprod.py` 一类）锁定最终形态。

**Hook 投影的架构决策：第一期不做。** 当前 capability seam 全集只有 5 个 builtin（`builtins.py`：`session.event-store`、`mcp.connector`、`skill.source`、`context.contributor`、`session.item.renderer`，加 `agent.provider` 共 5+1）——**不存在 hook seam**。原示例中的 `hook.interceptor/v1` 是不存在的 capability，写进去意味着要么先在 `capability-definition.schema.json` 新增 seam（走 §9.2 治理流程），要么砍掉。决策为**砍掉**，理由：(a) Cordis hook 是进程内同步拦截语义，跨进程后每次工具调用关键路径增加一次 RPC 往返，破坏热路径；(b) 新增 seam 是架构负债，应在有真实阻断需求（如审批拦截）时按需立项，而不是为对称性预铺。因此首期 `capabilities[]` 只投影 `agent.provider/v1` + `mcp.connector/v1`（tool 走 mcp.connector 通道投影），`invoke_hook` 方法不进 handshake。

### 5.1 已发运 bundle 的迁移策略（必答，不能只靠 digest）

多 capability 扩展对已发运 DSH bundle 的真实破坏链条（**不是** `manifest.json` 的 aggregate_digest——它只在发版门禁测试里用，bundle 运行时校验不读它）：

> `dsh-agent-provider-host.schema.json` 变更（descriptor 的 `definition`/`slot` const 对变为 `capabilities[]`）→ sidecar 上报的 descriptor 形状变 → `DshAgentProviderDescriptor.descriptor_digest()`（dsh.py:207）变 → 投影进内部 `PluginManifest` 的 `provides[]` 变（dsh.py:825-826）→ `plugin_lock_digest()`（contracts.py:438）变 → bundle 的 `manifest.plugin_lock_digest` 失配 → resolve 阶段报 `plugin_bundle_lock_digest_mismatch`（`ksadk/plugins/bundle.py` 校验 `bundle_digest` / `composition_profile_digest` / `plugin_lock_digest` 三项）。

必须在落地前选定一条迁移路径并写进实现：

| 选项 | 做法 | 适用 |
|---|---|---|
| **A. 一次性重编译（推荐）** | 契约变更合入时，同步重编译全部已发运 bundle（重新生成 plugin.lock 内的 plugin digest）并重新发运 | 当前 bundle 数量少、无外部消费者，迁移成本最低 |
| B. lock 校验放宽 | lock 校验从精确 digest 比对降级为 major 契约族比对 | 仅当历史 bundle 不可重编译时使用，长期不建议 |
| C. 双 descriptor 兼容期 | sidecar 同时上报单/多两种 descriptor 形态，宿主兼容读取 | 增加长期包袱，除非有不可控外部依赖否则不选 |

推荐 A，并在 CI 中加一条门禁：**contracts/ 下任何 schema 变更必须伴随 bundle 重编译 checklist**，防止再次出现"契约改了、发运物没跟上"的漂移。（`d0e6bab5` 按此执行：manifest.json 新增条目的 digest 已独立复算通过，`plugin-lock.schema.json` 同步更新。）

### 5.2 能力转发的执行语义（详细设计的前置边界）

`call_tool` 跨进程后必须明确以下语义（hook 已砍出首期，见 §5 决策；若未来按需立项，其时序/性能约束见原讨论——Cordis hook 进程内同步语义跨进程后破坏热路径，`drain`/`dispose` 需先等所有在途 hook 返回或超时）：

- **取消粒度**：现有 `cancel` 语义是"取消当前 turn"。新增 `call_tool` 后需要两层取消——`call_tool.cancel`（单次调用）与 turn 级 `cancel`（级联取消所有在途 call_tool）。
- **超时**：每个 `call_tool` 携带独立 deadline；宿主默认超时（建议 60s 起步）+ 工具自定义上限。
- **流式**：v1 不做工具结果流式，`call_tool` 为 request/response；流式需求出现后再在契约集内演进。
- **错误分类**：跨进程错误统一三分类——工具业务错误（可回传给 LLM）、sidecar 进程错误（触发 health/重启）、超时（可重试策略交给宿主）。不做分类会把宿主熔断逻辑打穿。

### 5.3 实现决策记录：宿主↔sidecar 传输层改走 loopback MCP（2026-09，commit `d0e6bab5`）

实现没有按 §5 原设计在 `dsh-agent-provider-host/v1` 的 JSONL ABI 上加 `call_tool`，而是**新起了一份 capability-host 契约** `ksadk.dsh-capability-host/v1`（`contracts/plugin/v1/dsh-profile-capability-host.schema.json`）：

- **传输形态**：sidecar 在 `127.0.0.1` 随机端口起 **streamable-http MCP 端点**，工具调用直接走标准 MCP `tools/call`；启动握手退化为 stdout 单发 ready 行（含 `profileDigest`/`inventoryDigest`/`dshVersion`/完整 tools）+ 独立 token 行。
- **能力投影**：只投影 `mcp.connector/v1`（比原设计更窄，可接受）；无 hook，符合 §5 决策。
- **旧契约关系**：`dsh-agent-provider-host.schema.json` 零改动，`agent.provider/v1` JSONL ABI 链路原样保留，模块自述 "deliberately orthogonal"。

**裁决：认可这条路线，更新本方案。** 理由：

1. **传输层 ≠ 插件格式**。§1.2 第 5 条禁止的是"发明第三套**插件格式**"（面向插件作者的 manifest/装配契约）；宿主↔sidecar 的传输是内部实现细节，插件作者对此无感知——他们写的仍是标准 Cordis 插件。
2. **MCP 传输是工程上的更优解**：浏览器 UI 宿主（§8.3 的 dsh client 容器）无法直接消费 stdio JSONL，但天然消费 HTTP MCP；工具投影走标准 MCP 协议恰好落进 `mcp.connector/v1` seam，语义比自定义 `call_tool` 方法更标准。
3. **与 §9.2 治理的关系**：这是"digest 是唯一真相"门禁的第一次真实触发——新契约已登记进 `manifest.json` 并锁定 digest，属于有记录的变更而非漂移。但应吸取教训：**架构级偏离（新契约名/新传输）应在实现前先进方案补决策记录，而不是实现后追认**。

后续约束：`dsh-agent-provider-host/v1`（JSONL ABI）与 `ksadk.dsh-capability-host/v1`（MCP 传输）**职责分立、不合并**——前者服务 `agent.provider/v1`（完整 Agent 执行），后者服务 capability 投影（tool）。若未来出现第三种宿主↔sidecar 传输需求，须先走 §9.2 评审。

---

## 6. Node 运行时分发与部署边界

Sidecar 路线最大的隐性成本是"Node 从哪来"。ksadk 的三种运行形态约束不同，决策如下：

| 运行形态 | 策略 |
|---|---|
| **本地 Studio（用户机器）** | 探测宿主 Node，缺失则拒绝装配并给独立错误码；不内置二进制（Mac/Windows/三架构体积不可控） |
| **托管 serverless pod** | sidecar 镜像层内置平台匹配的 Node dist，对齐官方 `deepseek-harness-runtime-bin` 的打包方式；镜像体积增量需单独评估并设上限 |
| **hermes 等第三方 runtime 镜像** | 不主动引入。hermes 镜像已发生过 ksadk 依赖冲突（openai pin），Node 叠加风险更高；仅当用户显式启用 dsh capability 时按需拉起，且文档标注镜像要求 |

所有形态统一要求：sidecar 版本与契约 digest 绑定（sidecar 启动时上报其实现/遵循的 digest，宿主不匹配即拒绝装配），避免"宿主契约新、sidecar 实现旧"的静默漂移。

**实现落地情况（`d0e6bab5`）**：Node ≥ 22.19.0 启动前探测（`dsh_capabilities.py` `_require_node_version`）；NODE_OPTIONS 固定 512MB heap 且禁止调用方注入；env 白名单过滤 secret 类 key；POSIX 进程组隔离 + SIGTERM→SIGKILL 回收；bundle 自身 sha256 对照 `dsh.bundle.integrity` 自校验；ready 行含 profile/inventory digest，Python 侧用跨语言 canonical encoding（IEEE-754 位级对齐）独立重算校验；运行期 inventory 漂移将 /health 置 503 并拒绝工具调用。"宿主不匹配拒绝装配"落在版本/digest fence 上，达标。

---

## 7. 安全与隔离立场

npm 插件即任意代码执行，本提案的安全基线：

1. **进程隔离是唯一屏障**：dsh 插件永远只在 sidecar 进程内运行，宿主 Python 进程不 `import` 任何插件代码——这也是选择 Sidecar 架构的安全红利，必须在实现中作为硬约束（code review checklist 项）。
2. **sidecar 权限收敛**：sidecar 进程以最小权限运行（无宿主凭证注入；MCP workspace 相关的凭证按 capability 显式授予，而非进程级透传）。
3. **来源可见性**：profile（`cordis.patch.yml`）中安装的每个插件必须能在 Studio UI 中被完整列出（包名、版本、来源 registry），不允许静默装配。
4. **来源校验**：插件安装限定显式 registry 源且必须 `pkg@精确 semver`，禁止 git URL/tag/range（比 Codex 官方 marketplace 的信任模型收紧一档）；签名机制随上游 dsh 生态演进跟进，不提前自造。
5. **资源限额**：sidecar 进程设内存/超时限额，崩溃自动重启但计入 health 降级，连续崩溃触发熔断（复用现有 health/drain 语义）。

**实现落地情况（`d0e6bab5`）**：安装源 `validate_dsh_registry_source` 强制 `pkg@exact-semver`（bridges/dsh.py）；REST 安装需显式 `acceptHostPermissions`；MCP 端点仅 loopback、Bearer 为 HMAC 作用域 token；熔断器、并发/请求体限额全套接入。Codex 侧：Studio 无 marketplace/add 路由，git/npm 源 ksadk 从不 fetch（只锁宿主解析后的坐标，git 强制 40 位 SHA、npm 精确版本+integrity），凭证不序列化明文。

---

## 8. 实施路线图

1. **Phase 2.1** ✅（`d0e6bab5` 基本完成）：补齐 Codex 数据面（manifest 解析 + 本地 skills/mcp 注入），按 §8.1 验收矩阵验证。残留：hooks 触发被有意阻断（见 §8.1 修订口径）；关键 E2E（skills/stdio MCP）默认 skip，需在 CI 门禁中显式开启。
2. **Phase 2.2** ✅（`d0e6bab5` 基本完成）：sidecar 契约与 Node Cordis sidecar 落地（传输层偏离已裁决，见 §5.3）。残留：**react-ui 未接线**（见 §8.3）；插件 A 证据用的是自制 fixture（见 §8.2）；5 个存量测试回归待修（见 §11）。
3. **Phase 2.3**（下一阶段，展开见 §8.6）：react-ui 接线 UI sandbox `ui-sessions` 路径，打通 dsh 插件工具面板勾选与实时调用的前端闭环。
4. **Phase 2.4**：完成 CodexAdapter 下沉与 Legacy Runtime 清理。

### 8.1 Phase 2.1 验收矩阵（Codex 官方插件"可用"的完成标准）

以 fixture 集合而非口头"100%"作为完成定义，最小矩阵（含 `d0e6bab5` 核验结果）：

| 数据面 | 验证内容 | 通过标准 | 核验结果 |
|---|---|---|---|
| `plugin.json` 解析 | 双格式（snake/camelCase）+ 必填/可选字段 | schema 校验 + 负例（缺字段/类型错/双写/路径逃逸/symlink）全部拒绝 | ✅ `codex_manifest.py`，负例覆盖全面 |
| skills | 至少 2 个官方插件携带的 skills 目录 | skill 被 agent 实际发现并触发一次 | ✅ 装配+触发 E2E 均有（E2E 需 `KSADK_CODEX_PLUGIN_E2E=1`，默认 skip） |
| mcpServers | 至少 1 个官方插件的 stdio MCP server | tool list 注入成功且调用返回 | ✅ 单测 + 真实 App Server E2E |
| hooks | 官方插件的事件 hook | ~~hook 在对应生命周期被触发~~ **修订（2026-09）**：解析 + 快照 + lockfile 记录完整，运行时对含 hook 绑定 fail-closed 阻断（`CODEX_PLUGIN_HOOK_TRUST_UNAVAILABLE`），理由是官方无可验证 hook trust API。触发路径推迟到 Codex 提供信任 API 或自研受控执行后交付 | ⚠️ 按修订口径达标，按原口径未交付 |
| 源物化 | local / git / npm 三种源 | 三源均能 materialize 出标准 PluginReference 进 lockfile（git 强制 40 位 SHA、npm 精确版本 + integrity，可变源全拒） | ✅ 含 5 组可变源拒绝负例 |

### 8.2 dsh 生态兼容性验证：双插件打样

用两个代表性插件作为 dsh 生态兼容性的**实证锚点**，比跑通 N 个普通插件更能暴露契约缺口：

| | 插件 A：纯 capability 插件 | 插件 B：带 UI 插件 |
|---|---|---|
| 代表 | 真实上游 tool-only 插件（如 `@deepseek-ai/dsh-tools-*`），不改源码 | `@wegent/dsh-ui-git` 同类（client 注入式带 UI 插件），自研打样兜底 |
| 验证面 | Sidecar 装配 → describe/ready 投影 → 工具调用全链路 | UI 投影：安装后 Studio 出现扩展面板 + 面板与 sidecar 双向通信 |
| 通过标准 | 安装零宿主改动即可在绑定面板勾选；调用成功且有契约级错误分类 | 新 tab/面板出现、能渲染插件 UI、UI 触发的 tool 调用走同一条 sidecar 通道 |
| 暴露的风险 | 执行语义（§5.2） | UI 投影的安全边界与宿主耦合 |

**实现现状（2026-09）**：插件 A 当前用的是**自制 fixture**（`@ksadk-test/dsh-node-tool-plugin`），E2E 装的是真实上游 toolchain（CLI + Cordis runtime），但被验证的插件本体不是真实上游插件——**"不改上游插件源码即可复用"这一方案核心命题尚未被证明**，需补一个真实上游 Cordis 插件的不改码 E2E。插件 B 的后端链路完整、前端断线（见 §8.3）。

**UI 投影的上游实证**（源码核实）：

> **实证一（dsh 原生 client 注入）**：`@wegent/dsh-ui-git` 等 wework `dsh/ui-*` 子包**就是 Cordis dsh 插件且自带 React UI**（`.tsx` 组件），机制是 package.json 的 `dsh.client.inject` 字段——注入 `@deepseek-ai/dsh-client-runtime`（`platform: "web"`），即 **dsh 生态本身存在客户端 UI 投影通道**：插件前端代码经 dsh client runtime 在宿主前端容器内运行，与 Cordis 后端共享上下文。
>
> **实证二（wework workbench manifest）**：`electron/src/host/workbench-plugin-manager.ts` 定义了另一套独立机制——`frontend: { entry, export, sha256 }`（前端 JS 模块）+ `desktop: { command, args, sha256, capabilities }`（本地命令 sidecar），带 `required`/`pinnedToClientVersion` 版本锁定。这是**宿主自有契约**，绕过 Cordis，用于更重的工作台级插件。
>
> 结论：带 UI 插件有上游模式可循，ksadk 采纳 dsh client 注入这条（与上游同构），不自研 UI 投影协议，wework workbench manifest 仅作参考。

### 8.3 UI 投影契约：Studio 里"多一个 tab"的准确定义

"带 UI 的插件装完是不是 Studio 多一个 tab"——**是，但 tab 只是表象，背后是三层决策**：

1. **UI 形态选型**（按来源分而非全局二选一）：
   - **路线一：dsh client runtime 容器（官方与上游带 UI 插件的统一路径）**——上游 `ui-*` 类插件与 wework 自研插件的 UI 走的是同一条通道：`dsh.client.inject` + `@deepseek-ai/dsh-client-runtime` 在宿主前端容器内运行。**ksadk 官方带 UI 插件也采用同一形态**（写真正的 Cordis 插件 + client 注入 UI），不自研 UI 投影契约。插件 UI 在容器内运行，postMessage ↔ Python 宿主 ↔ sidecar。
   - **现状与代价（实事求是）**：当前 Studio（`ksadk/studio/react-ui/`）是纯 React SPA，`PluginsPage.tsx` 只有 enable/disable/uninstall 列表，**不具备任何动态 runtime 容器能力**。引入 dsh client runtime 属于前端架构级演进（iframe/Shadow DOM 沙箱、跨域 postMessage 总线、样式隔离、client runtime 版本管理），预计是双插件验证中**最重的一块工作，工作量评估不应低于 sidecar 后端本身**。这是本路线的主要成本，但它买断的是"上游 + 自研带 UI 插件同一套机制"，且随上游 dsh client runtime 演进免费升级。
   - **路线二：A2UI 声明式渲染（agent 驱动 UI 的主线，不作为插件 UI 通道）**——A2UI 仍是 agent 输出侧 UI 的 canonical 方向（`a2ui.block.*` 事件 + renderer registry + catalog），但它**不用于插件 UI 投影**：插件面板/Tab 的入口注册（id、标题、图标）由 describe/ready 声明，容器内插件 UI 若为 agent 会话输出内容，可复用 A2UI 渲染资产——两者是宿主内并存的两条 UI 主线，不合并成新协议。
   - wework workbench manifest（实证二）是宿主自有契约的参考样板，不照抄也不引入——ksadk 不需要第三套重插件通道，重插件用 dsh 形态承载。
2. **扩展点位**：Studio 需要定义标准 extension point（tab 页 / 面板 / 面板内嵌卡片三级，tab 是最大粒度）。tab 的出现必须来自 describe/ready 的声明式注册（插件声明 id、标题、图标、入口），Studio 不为任何插件硬编码 tab——与 §9.4"能力投影声明式"同一原则。
3. **通信链路**：插件 UI ↔ sidecar 不直连，统一走 `Studio 前端 → Python 宿主 API → sidecar`，UI 触发的调用与 Agent 工具调用共用同一条 sidecar 通道并附带 UI 会话标识。这样权限、审计、熔断（§7）对 UI 触发的调用天然生效，不另开旁路。

**实现落地情况（`d0e6bab5`）**：后端已完整——opaque-origin iframe（`sandbox="allow-scripts"` + credentialless + no-referrer）、极严 CSP（default-src 'none'）、bundle 以 SRI 钉 digest、capability token 不进文档经 MessagePort + 一次性 nonce 转交、声明式 extension points（`StudioContributionRegistry` 7 个 slot，零插件名硬编码）、UI 会话独立 token/限速（60 req/min）/并发上限（4）/TTL/tool 白名单；UI 调用经 `POST /api/v1/plugin-ecosystems/dsh/ui-sessions/{id}/messages` → 同一 sidecar MCP 端点，与 Agent 调用同通道无旁路。**关键缺口**：react-ui 源码完全没有消费 `ui-sessions`/sandbox 路径，浏览器 E2E 用的是独立 fixture 不 mount studio/api.py；react-ui 现存的 `__ModuleLoader__` 顶层注入路径与后端默认 deny 策略互斥——**用户在真实 Studio 里两头都用不上，插件 B 的验证闭环不成立**。这是 Phase 2.3 的核心工作。

### 8.4 双插件验证的联动价值

插件 B 首选真实上游 `ui-*` 插件；若为验证 Studio 集成深度而自研打样，打样物也是**真正的 Cordis 插件**（不引入 ksadk 私有插件格式），同时成为 §9.4 自研插件机制的第一个实例（脚手架、双端同构测试、client 注入 UI 全链路走一遍）——既给 dsh 兼容性补上 UI 证据，又把自研插件基建在真实需求下磨出来。

### 8.5 插件的形态谱系：从工具到独立应用

双插件验证背后要明确一个认知：**"插件"在本架构里是一个形态谱系，独立应用是它的最大粒度端点**。宿主不按形态区分对待，只按 capability 投影消费：

| 形态 | 内容物 | 投影方式 | wework 对应实证 |
|---|---|---|---|
| **纯工具插件** | Cordis 工具逻辑（无状态、无 UI） | describe/ready → tool schema，MCP `tools/call` 调用 | `@wegent/dsh-*` 大部分子包 |
| **带 UI 插件** | 工具 + 前端模块（`dsh.client.inject`） | + 面板/tab 注册（§8.3） | `@wegent/dsh-ui-git` 等 `dsh/ui-*`（client 注入式） |
| **独立应用插件** | 完整前端 + 自有后端进程 + 数据 | + 自有进程生命周期（由 sidecar 宿主代管）+ 自有存储 | `@wegent/dsh-ui-plugin-center`——以 Cordis 插件形态交付的"插件市场"应用，有完整 UI 与自有数据 |

**独立应用插件的边界条件**（不是所有应用都该做成插件）：

1. **生命周期归 sidecar 宿主代管**：应用的后端进程由 dsh sidecar（或其 `desktop.command` 等价物）拉起，享受统一的 health/drain/dispose 语义——应用崩溃走 §7 的熔断降级，不单独发明进程管理。
2. **存储必须走宿主托管**：应用自己的数据（配置、历史）经 capability seam 暴露的 Store 通道持久化（`ksadk://` 资源），不允许插件私自落盘到宿主文件系统任意位置——否则托管环境（serverless pod）下数据会丢，也无法审计。
3. **入口仍是声明式**：应用在 Studio 的 tab/面板入口同样来自 describe/ready 注册，插件可以是一个应用，但 Studio 不需要知道它是应用——只知道它投影了哪些 capability。这是"插件化宿主保持薄壳"（§9.4）能成立的根本。
4. **何时越界**：如果应用需要独占端口对外提供服务、或需要跨宿主生命周期常驻（不是随 session 起/停），它就超出了插件形态，应升级为 ksadk 托管的独立 agent/服务（agentengine-server 侧的 artifact），不要硬塞进插件。

**对验证计划的含义**：双插件打样不必一开始就做独立应用形态，但 §8.3 的 UI 投影契约和通信链路设计必须**预判到独立应用量级**（多面板、有状态、长会话），避免第一个打样把契约锁死在"单面板无状态"上。

### 8.6 Phase 2.3 展开：react-ui 接线 UI sandbox（前端闭环）

§8.3 已明确这是双插件验证中最重的一块（工作量不低于 sidecar 后端本身）。拆成 6 个子任务，带各自验收标准与顺序依赖：

**T1. 修复存量测试回归（前置，非本 Phase 但必须先做）**

- 内容：`bridges/dsh.py` 的 `validate_dsh_registry_source` 收紧打挂 `test_dsh_plugin_bridge.py` 5 个存量测试——按新策略（`pkg@精确 semver`、禁 git/tag/range）更新测试 fixture 与断言，而不是放宽校验。
- 验收：`pytest tests/plugins/test_dsh_plugin_bridge.py` 全绿；策略语义不回退（负例仍全部拒绝）。

**T2. UI sandbox 前端接线（Phase 2.3 主体）**

- 内容：react-ui 消费后端已有的 `ui-sessions` 路径：
  1. 会话创建/生命周期管理（`POST /api/v1/plugin-ecosystems/dsh/ui-sessions`，含 TTL/续期）；
  2. iframe 挂载：按 sandbox 文档渲染 opaque-origin iframe，capability token 经 MessagePort + 一次性 nonce 转交（对齐后端 `dsh_ui_sandbox.py` 的设计，前端不重新发明）;
  3. 消息总线：iframe → react-ui → `POST .../ui-sessions/{id}/messages` → sidecar，响应经 MessagePort 回投。
- 验收：浏览器 E2E 改为 **mount 真实 studio/api.py** 跑通（现有独立 fixture E2E 保留为后端单测），断言：插件 UI 在 iframe 内渲染、UI 触发的 tool 调用到达 sidecar 并返回、token 不出现在 DOM/URL/日志。

**T3. 扩展点消费：registry 驱动的 tab/面板渲染**

- 内容：react-ui 的 `StudioContributionRegistry`（7 slot，已存在）接入 dsh 的 `extensionPoints` 声明（sidebar.navigation / workspace.tab 等）：dsh capability service 的 capabilities 响应 → 前端注册 contribution → `DshWorkspaceSurface` 或通用 surface 渲染。**零插件名硬编码**是硬约束（§8.3 原则）。
- 验收：安装带 UI 的 dsh 插件后，Studio 出现新 tab，无需前端发版；卸载后 tab 消失。

**T4. `__ModuleLoader__` 旧路径退役决策**

- 内容：react-ui 现存的顶层 `__ModuleLoader__` script 注入路径与后端默认 deny（`LEGACY_TOP_LEVEL` 需显式 opt-in 且 url 置 None）互斥。二选一：(a) 旧路径直接删除（推荐——sandbox 路径是其安全超集）；(b) 保留一个迁移窗口。删旧路径需同步迁移其现有消费方。
- 验收：全仓无 `__ModuleLoader__` 消费（或迁移完成）；后端默认策略无互斥残留。

**T5. 真实上游插件不改码 E2E（补兼容性证据）**

- 内容：选一个真实上游 Cordis tool 插件（不改源码），走完整链路：Studio 安装（pinned registry source）→ sidecar 装配 → 绑定面板勾选 → agent 会话中触发调用。如上游无纯 tool 插件可用，选最接近的并记录差距。
- 验收：E2E 通过且插件包零改动；"复用 dsh 生态不改源码"命题成立并留档（进 §9.3 上游样本 fixture）。

**T6. 关键 E2E 进 CI 门禁**

- 内容：skills/stdio MCP 的 Codex E2E（需 `KSADK_CODEX_PLUGIN_E2E=1` + 真实 codex 二进制）与 T5 的新 E2E，在 CI 中以独立 job 显式开启（带 retry 与环境预检），而不是默认 skip。
- 验收：CI 上这些 E2E 实际执行且失败能挡住合并。

**顺序**：T1 → T2 → T3（T3 依赖 T2 的会话/挂载基建）→ T4（可与 T3 并行）→ T5（依赖 T2/T3 的完整闭环）→ T6（收尾）。T1-T2 完成后插件 B 的后端+前端链路即闭环，T5 完成后 §8.2 双插件验证矩阵才算真正达标。

---

## 9. 长期迭代机制：如何持续跟进 harness plugin 生态

本方案不是一次性工程。dsh、wework、Codex 三条上游都在快速演进，ksadk 侧需要一套持续运转的机制，而不是每次上游发版都重新做一次架构决策。

### 9.1 上游跟踪（Quarterly Upstream Review）

| 上游 | 跟踪点 | 触发的 ksadk 动作 |
|---|---|---|
| **dsh (deepseek-harness)** | Cordis 版本、`--profile` CLI 参数变化、官方 `runtime-bin` 打包方式、npm 插件 API（`apply(ctx)` 契约）、`dsh.client.inject` client runtime 演进 | sidecar 升级评估；profile 语法适配；UI 容器 runtime 升级评估 |
| **wework** | sidecar 拉起方式、多版本并存管理（`harness-runtime/` 目录）、插件市场 UI 模式 | 借鉴实现模式，无协议耦合 |
| **Codex app-server** | `plugin/*` / `marketplace/*` RPC 方法与字段变更、**hook trust API 是否出现**（§8.1 hooks 行的前置依赖） | `CodexAppServerPluginBridge` 逐字段对齐更新 + fixture 重生成；hook trust API 出现后重启 hooks 触发交付 |

- 本地镜像仓（`codex/`、wework 镜像）在每次 review 时 `git fetch` + 记录 diff 摘要进 `docs/upstream-notes/`（每次一份带日期的笔记，不追求长期维护，只为当时决策留痕）。
- dsh 官方 Python SDK 的 `deepseek-harness-runtime-bin` 版本变化直接决定我们 sidecar 镜像的 Node/Cordis 基线，升级必须走 sidecar 版本 ↔ digest 绑定校验（§6），不做静默跟随。

### 9.2 契约演进治理

契约（`contracts/plugin/v1/`）是三方（宿主 PluginHost、sidecar、发运 bundle）的共同根，治理规则：

1. **变更窗口**：schema 变更只允许随 Phase 里程碑合入，不允许随手 PR；每次变更必须同步更新 fixtures + 触发 bundle 重编译（§5.1 门禁）。
2. **digest 是唯一真相**：任何两方（宿主/sidecar/bundle）对契约的理解以 `manifest.json` aggregate_digest 比对为准，运行时不做字段级协商。
3. **v1 → v2 的触发条件**（提前写死，避免临场争论）：出现首个**不可控外部消费者**（第三方按我们契约开发插件）之后，任何 breaking change 都升 v2；在此之前一切 breaking change 记为 v1 内部迭代。v2 引入时 v1 sidecar 与 v1 bundle 必须仍可运行（宿主双契约支持，只读旧不写旧）。
4. **能力扩展的默认答案**：新的 capability 形态（如未来的 context/a2ui 类投影）优先映射到现有 slot 体系（`capability-definition.schema.json`），只有塞不进现有 slot 才新增 definition——防止 capability 枚举野蛮生长。
5. **架构级偏离先记录再实现**：§5.3 的教训——新契约名/新传输层这类架构级选择，应在实现前先在本方案补决策记录（哪怕只有一段话），而不是实现后追认。宿主↔sidecar 传输契约属于内部实现，与"插件格式"（§1.2 第 5 条）分属两个层面，前者允许工程最优选择、后者永不自研。

### 9.3 conformance fixture 资产化

把"上游兼容性"从人工验证变成可重复运行的资产：

- `contracts/plugin/v1/fixtures/` 扩展为三层：**契约 conformance**（宿主↔sidecar 双端各自跑同一套 fixture，结果必须一致）、**上游样本**（从 dsh/wework/codex 真实插件抓取的 manifest/hook 样本，上游升级时刷新）、**负例集**（非法输入必须拒绝）。
- 每次上游跟踪 review 后如样本有变，fixture 更新作为 review 的固定产出之一。
- Codex 侧 conformance 以官方 app-server 的真实 JSONL 会话录制为基准（现有 bridge 已逐字段对齐，录制回放固化这个优势）。
- **新增（2026-09）**：dsh 侧增加一条规则——**每个 `@ksadk` 官方插件必须能被官方 dsh CLI 以 `--profile` 方式拉起**（在上游 npm 发布后验证），作为"没有走样成私有格式"的天然测试。

### 9.4 自研 harness plugin 的持续迭代

对标 wework 的 `@wegent/dsh-*`：wework 没有只消费上游插件，而是以真 Cordis 插件形态自研了 14 个子包（workspace、executor、市场等能力都以插件交付），宿主本体保持薄壳。ksadk 采用同样模式——**平台自有能力（studio 集成、金山云工具、托管 bind）以自研插件交付，且每个自研插件必须是 Cordis/dsh 原生插件或 Codex 格式插件之一，不发明 ksadk 私有插件格式**（§1.2 第 5 条）。形态选择规则：强 UI / 需要 Node 生态库 → Cordis 插件；轻量工具 / 纯 MCP+skills → Codex 格式。自研插件的迭代机制：

**1. 命名空间与仓库布局**

- npm scope 统一 `@ksadk/dsh-*`，与上游 `@deepseek-ai/dsh-*`、wework `@wegent/dsh-*` 形成惯例对齐，profile 中一眼可辨来源。
- 布局建议 monorepo（`ksadk-harness-plugins/`，pnpm workspace），每个插件一个子包，共享 tsconfig / eslint / 测试基建；与 Python 主仓分仓，避免 Node 工具链污染 Python CI。

**2. 交付链路与版本纪律**

- 版本独立于 ksadk 主版本：插件按 semver 独立发版，`plugin.lock` 记录精确版本 + integrity，发运 bundle 与插件版本解耦。
- 每个 `@ksadk/dsh-*` 的 release 必须带契约 fixture 声明：该插件依赖的 sidecar 契约 digest 与最小 Cordis 版本，CI 校验与主仓 `contracts/plugin/v1/manifest.json` 兼容——自研插件是契约的"第一消费者"，也是契约变更的第一个回归对象（§9.2 门禁的执行者）。
- 发 npm 私有 registry（对齐私有 `agentengine/` namespace 惯例），公开与否随整体开源策略走，不提前决定。

**3. 开发体验（决定生态能否长起来）**

- **模板与脚手架**：`pnpm create @ksadk/dsh-plugin` 模板包，内置单 capability（tool-only）最小示例 + schema 校验 + 本地 mock sidecar，做到 `create → dev → test` 三条命令内跑通。
- **双端同构测试**：自研插件的单测跑在真实 sidecar 容器内（而非 mock Cordis），复用 §9.3 的 conformance fixture；同时跑 Python 宿主侧集成测试（安装→装配→调用全链路），两边共用同一份 fixture 数据，杜绝"Node 侧绿、Python 侧红"。
- **能力投影声明式**：插件声明的能力（tool schema 等）自动生成 Python 侧的绑定面板元数据，Studio UI 不为每个自研插件手写适配——新增插件零宿主改动即可在面板勾选，这是插件化真正兑现的点。

**4. 迁移路径：现有 Python 内置能力逐步插件化**

不做一次性大迁移，按"新增能力走插件、存量能力按需搬"推进：

| 优先级 | 候选 | 说明 |
|---|---|---|
| 先行 | 金山云 domain tools（ksyun API 封装类） | 天然 tool 形态、无宿主状态耦合，最适合打样 |
| 中期 | MCP managed bind 相关的辅助能力 | 与 `mcp.connector/v1` slot 直接对应 |
| 不搬 | session/transcript、审批流、事件总线 | 宿主核心状态机，插件化只有成本没有收益 |

**5. 度量与退出标准**

- 每个自研插件跟踪：装配成功率、工具调用 P95 延迟（跨进程开销是否可接受）、崩溃率。跨进程 tool 调用若 P95 延迟显著高于进程内基线（建议阈值 2×），说明该能力不适合插件形态，允许降级回宿主内置——插件化是手段不是目的。

### 9.5 节奏建议

- **随版本走**：ksadk 常规版本中，plugin 相关改动保持向后兼容（digest 机制兜底）；自研插件独立按 semver 发版（§9.4）。
- **每季度**：上游 review（9.1）+ fixture 刷新（9.3）+ 一页决策记录（升级/不升级/为什么），进 `docs/upstream-notes/`。
- **持续**：契约变更走 9.2 治理门禁，CI 强制（schema diff → fixtures diff → bundle 重编译 三联动检查）。

---

## 10. 长期形态小结

终态下三种插件来源在同一机制下共存，宿主只认 Capability Seam；**ksadk 自身不维护第三套插件协议**（§1.2 第 5 条；宿主↔sidecar 传输契约的例外裁决见 §5.3）：

| 来源 | 形态 | 迭代方式 |
|---|---|---|
| npm dsh 生态插件 | sidecar 内加载，`@deepseek-ai/dsh-*` 等 | 上游跟踪（§9.1），我们不改 |
| Codex 官方插件 | 数据面装配 + AppServerBridge | 上游跟踪 + conformance 回放（§9.1/9.3） |
| **ksadk 官方插件** | **同样是上两种形态之一**（Cordis 插件或 Codex 格式包），不做私有格式 | **我们主导迭代**（§9.4），平台能力的默认交付形态 |

---

## 11. 实现状态与合入前清单（持续更新）

### 11.1 已落地（commit `d0e6bab5`，2026-09-04 review 核验）

- **Codex 数据面**：§8.1 矩阵 4/5 通过（解析/skills/stdio MCP/三源物化），hooks 按修订口径达标（解析+锁定+fail-closed 阻断）。安全面（§7）全部符合且边界干净。
- **dsh capability sidecar**：新契约 `ksadk.dsh-capability-host/v1`（§5.3 裁决记录）、loopback MCP 传输、digest 围栏（跨语言 canonical encoding）、Node 版本/环境/进程组全套加固、UI sandbox 后端（opaque-origin iframe + SRI + 声明式 StudioContributionRegistry + 限速/熔断）。
- **治理**：manifest.json digest 登记完整（22 文件独立复算通过）；旧 `dsh-agent-provider-host/v1` 消费链路兼容未破坏。
- **测试规模**：新增约 4700 行测试，317 项通过。

### 11.2 合入前遗留（按优先级；T1–T6 详细展开与验收标准见 §8.6）

1. **修 5 个测试回归**（= T1）：`bridges/dsh.py` 的 `validate_dsh_registry_source` 收紧打挂 `test_dsh_plugin_bridge.py` 5 个存量测试，策略正确但测试未同步更新。
2. **react-ui 接线 UI sandbox**（= T2–T4，Phase 2.3 核心）：react-ui 无 `ui-sessions` 消费，`__ModuleLoader__` 旧路径与后端默认 deny 互斥，插件 B 闭环不成立。
3. **补真实上游插件不改码 E2E**（= T5）：当前插件 A 证据是自制 fixture，"复用生态不改源码"命题未被证明。
4. **CI 门禁开启关键 E2E**（= T6）：skills/stdio MCP 的 E2E 默认 skip（需环境变量+真实 codex 二进制），门禁中不生效。
5. **代码质量小项**：`codex_plugin_store.py` 的 `_retain_selected_components` 巨型函数拆分；`_load_installed_plugin` 7 元组改 dataclass。

### 11.3 决策待办

- hooks 触发路径：等 Codex 官方 hook trust API（§9.1 已列入跟踪点）还是自研受控执行，需要产品层面确认。
