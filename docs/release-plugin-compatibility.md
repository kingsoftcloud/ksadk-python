# Phase 2 插件化兼容与迁移说明

> 当前文档对应开发分支中的未发布能力。只有发布门禁全部通过后，才能把这里的实现标记为稳定版可用。

## 能力状态

| 能力 | 当前代码状态 | 稳定发布前仍需验证 |
| --- | --- | --- |
| DSH / Cordis 默认插件生态 | 受管 Profile 的 discovery/install/enable/disable/update/uninstall、dump-config 预检、不可变来源与失败恢复已实现；一个真实仓外 Node AgentProvider 已通过连续两轮及完整生命周期 E2E。唯一的语言无关 sidecar 执行 SPI 已冻结为 `dsh-agent-provider-host.schema.json` 与 golden transcript；它不是第三种插件分发格式。 | 任意新 Bundle 仍须分别通过权限、Provider 握手、会话投影和执行 conformance；一个参考 Provider 通过不等于任意第三方包自动受支持 |
| Codex 插件兼容 | App Server 的 list/read/install/uninstall、隔离宿主 E2E，以及真实 Codex MCP 多轮调用已实现 | 外部 marketplace 仍由 Codex 管理认证与权限；KsADK 不承诺所有第三方插件可用 |
| 旧 KsADK Python 插件实验实现 | `ksadk-plugin.yaml`、Python entry point、独立包管理/API/UI 和对应合同已删除；语言无关 Provider RPC 由 DSH Bundle 承载 | 不进入稳定产品规范，也不保留隐藏兼容命令。历史 0.8.2 Agent 本来就不依赖该实验格式 |
| Scheduler Lite | 本地 SQLite、once/interval/cron、IANA timezone、CRUD、run now、misfire、并发保护、occurrence 历史、全局页与 Agent 详情 Tab 已实现；真实浏览器已完成 accepted/terminal 与刷新回放对账 | 基于最终发布制品复验；云端 24×7 worker 属于后续阶段 |
| ConversationSurface / Renderer / A2UI | 合同、投影、identity reducer、核心 Renderer、A2UI action bridge 已实现；Studio 与 Hosted UI 复用 `ksadk-web` 的版本化 headless conversation 模块，新增未知事件保留在 replay/audit 但不污染对话；0.3.4 源码门禁、独立浏览器 E2E 与 Pages 演示 E2E 已通过 | `ksadk-web@0.3.4` 正式发布后必须从公开 registry 重建 Studio、预发 Hosted UI 与线上 Hosted UI；新 Agent 和历史 0.8.2 Agent 的最终真实部署回归、制品 provenance、公开审计和维护者审批仍是发布门禁 |

稳定产品只承认两种插件来源：默认的 DSH Bundle/Profile（由 Cordis 组合）和由 Codex App Server 管理的 Codex 插件。`ksadk plugin` 只是统一产品入口，不定义第三种安装包、manifest 或 ABI。

DSH Bundle 可以在同一个包中贡献 AgentProvider、Tool、Store、Renderer/A2UI、Studio route/sidebar/tab 或 Scheduler 能力；Cordis 负责依赖注入、生命周期和卸载清理。只有声明了 AgentProvider 且通过握手与执行 conformance 的 Bundle 才能出现在 Runtime 选择器中。小游戏或其它工作台扩展只是不同 slot 的贡献，不需要新增“游戏桥”或第三种插件格式。

### 会话事件与渲染的安全边界

聊天界面不是 Provider 原始事件的日志窗口。每条原生事件先由唯一的
`ConversationProjector` 映射为带 `itemId`、`sourceEventIds`、`kind`、
`payloadSchemaRef` 和生命周期的 `ConversationItem`；客户端只按
`itemId + sourceEventId` 去重并按首次出现顺序更新。工具调用与其独立结果以
稳定 `callId` 丰富同一张工具卡片，不能按正文、作者或最终文本去重。因此
`thinking → tool → thinking → answer` 会保留原顺序，重连或最终快照也只能更新
原卡片，不能再追加一条重复回答。

Phase 2 的共享 `ksadk-web` renderer 目标负责文本、思考、工具、审批、产物、错误和 A2UI；
在共享包收敛前，Studio 的同一套内建语义不得自行扩展为另一条协议。Provider 或 DSH/Codex
插件可以贡献新的**事件投影**，但不能从 Runtime payload
下载或执行 JSX、JavaScript、URL 或任意 React 组件。若将来要支持新的富卡片，先
随受信任的 `ksadk-web` 版本发布精确 `kind + payloadSchemaRef` renderer，再允许
Provider 产生该 schema；renderer 只获得冻结的 item 和受控
`SubmitInteraction` 回调。`hidden`/`internal` 或未声明为用户表面的未知事件只进
trace/replay，不进入聊天；明确的公开未知 surface 才使用固定安全卡片。A2UI 继续
只接受已校验版本、catalog digest 和 allowlist 组件。

Phase 2 的插件化能力是增量能力，不要求已有 Agent 重建或升级 Runtime。兼容边界如下：

| 已有形态 | Phase 2 行为 |
| --- | --- |
| 历史 framework Bundle | 继续由原有 ADK/LangGraph Runtime 路径解析和启动，不进入 PluginHost；0.8.2 的真实 fixture 是 LangGraph Bundle v2，未带 `compositionMode` 的历史 v2 被只读归一化为 legacy execution，另有更早 v1 fixture 覆盖旧格式 |
| 历史 Harness Bundle | 只有来源摘要命中显式 legacy allowlist 才进入旧 adapter；默认 allowlist 为空，未知 v1 fail closed |
| 无 DSH Profile/Bundle 绑定 | 历史 framework Bundle 保持旧 Runtime；新 Harness v2 不伪造绑定并 fail closed |
| 无 SDK version/source/commit 信息 | 允许启动，管理面显示未知；仅新 Bundle v2 的严格准入要求完整来源 |
| 未设置 `AGENT_KERNEL_ENABLED` | 保留原有 HTTP、Session 和 Runtime 路由；Kernel 控制能力不可用但不阻断启动 |
| 无 PostgreSQL | 本地默认使用 SQLite，也可显式使用 memory；多副本/HA 才要求外部一致性存储 |
| Runtime 不支持 DSH CompositionHost | 已登记的历史 Bundle 继续运行；新的 Harness Bundle v2 缺少就绪 DSH registration 时 fail closed，不回退 legacy adapter |

兼容逻辑由 Bundle/Runtime 的适配与解析边界负责，Studio 页面或插件 UI 不应散落历史格式判断。新建 Bundle v2 会明确写入 `compositionMode: legacy | composed`：只有 `composed` 必须携带 `compositionProfileDigest`、PluginLock 和对应的 Host 准入。已有 Agent 不会被静默改写；历史 v2 缺少该字段时只读归一化为 legacy execution，绝不因缺字段被送入 PluginHost。

## 不在 Phase 2 支持声明中的能力

- Claude Code、小游戏或任意第三方能力只有被标准 DSH Bundle 承载，或由 Codex 插件宿主管理时才进入产品；不会新增独立生态枚举。
- 一个参考 DSH AgentProvider 的真实连续多轮已经通过；DSH Codex Provider 已验证真实 App Server、MCP 两轮与同一 Thread，Codex 插件生命周期和隔离 one-shot child 的取消/清理也有独立 E2E。它们不自动放开任意第三方 Provider，更不把持续后台任务或云端 worker 声称为本地稳定能力。
- Codex 插件桥接不把 Codex 的宿主权限、认证状态或私有事件提升成 KsADK Kernel 协议。
- 云端 PluginHost admission、Operator 投射、云端 Scheduler worker、跨 Pod claim 和端云任务迁移属于后续阶段。

未来能力优先包装为标准 DSH Bundle，通过 Cordis service/slot 组合。只有像 Codex App Server 这样必须保留原生宿主与生命周期语义的生态才保留专用兼容层；不为每一种功能或外部项目增加 Bridge。所有 Provider 仍必须通过权限、生命周期、会话投影、失败回滚和历史 Agent 兼容 conformance，不能修改 Kernel 的 SessionEvent、lease、fencing 或 authorization 合同。

## 本地预检

在干净 checkout 中安装完整测试依赖和 Chromium，然后运行唯一的发布入口：

```bash
uv sync --extra all
uv run playwright install chromium
make release-preflight
```

该入口会先清空 `dist/`、生成绑定当前 commit 与 clean source tree 的 provenance，再构建唯一一份 wheel 和一份 sdist。门禁会拒绝旧版本残留、dirty 来源、commit 不匹配或两个制品 provenance 不一致，避免误审计或误上传历史制品。

预检会验证：

- 真实 0.8.2 LangGraph Bundle v2 与更早的 Bundle v1 fixture 在缺少插件 sidecar 时仍可被 Studio 管理并选择原有 Runtime；Harness legacy adapter 只接受显式登记的精确历史来源摘要；
- Kernel 未启用、没有 PostgreSQL 时，本地 Runtime 可启动并以 SQLite 管理 Session；
- 从公网 npm 安装固定版本 DSH 工具链，验证标准 Bundle 的创建、校验、打包、重新校验和官方 Codex subagent 包的格式/安装边界；同一预检也显式启动真实 App Server，验证 Codex 插件、Provider MCP 两轮和隔离 child conformance；
- Studio 从已安装 DSH Profile 加载 client bundle，挂载 route/sidebar/workspace slot，并在停用后完整移除贡献；
- ConversationItem 的 reasoning、tool、approval、A2UI、identity 去重、终态替换和双窗口幂等提交通过真实 Chromium 验证；
- wheel 与 sdist 同时包含共享 Web 和 Studio 的编译静态文件；
- React/TypeScript 前端源码和 `node_modules` 不进入 Python 制品，生成的静态文件不被 Git 跟踪；
- 复用公开制品审计，拦截本机绝对路径、内部服务地址、凭证和私钥形态。
- wheel 与 sdist 的 provenance 记录同一源码 commit 和 clean source tree；dirty 或过期来源会被拒绝。

该预检只证明当前源码与本地构建制品。仓库中的 0.8.2 历史 Bundle fixture 会自动验证旧 Agent 的非破坏性解析和本地 Session 路径，但这不等于云端、预发、多副本或已经在线的真实 Agent 已完成验收；真实线上 Agent 的打开、会话和状态仍是独立部署门禁。

当前 0.8.3 候选还额外完成了真实部署浏览器门禁：同一份显式固定的
`ksadk-web@0.3.4` 候选制品已通过本地共享静态载荷校验和独立浏览器 E2E。
npm 正式发布后必须从公开 registry 重建 Hosted UI，并在预发对 Studio 创建的
Codex Agent 与一个 0.8.2 历史 Agent 分别完成两轮会话、上下文续接、刷新回放、
reasoning、工具和审批卡片验证；只有绑定最终镜像 digest 的证据才计入发布门禁。

## Codex 生态桥接验证

KsADK 不复制或解释执行 Codex 插件，而是把 marketplace 和插件的读取、安装、启用状态及卸载交给 Codex App Server。仓库内置了一个不访问外网的最小 marketplace，用于验证真实宿主协议：

```bash
KSADK_CODEX_PLUGIN_E2E=1 \
  uv run --extra codex pytest -q tests/e2e/test_codex_plugin_bridge_e2e.py
```

该测试使用隔离的临时 `CODEX_HOME`，经真实 App Server 完成 marketplace 添加、插件发现、安装状态回读和卸载。也可以通过 `KSADK_CODEX_PLUGIN_E2E_MARKETPLACE` 与 `KSADK_CODEX_PLUGIN_E2E_NAME` 指向待验收的外部 Codex marketplace；外部插件的代码、权限与认证仍由 Codex 宿主管理。

## DSH / Cordis 默认插件生态验证

DSH 是默认插件 Host。`DshProfilePluginBridge` 管理隔离 Profile，安装、启停、升级和卸载均委托给受管理的原生 `dsh plugin`；每次修改后用 `dsh --dump-config` 预检。目录使用固定 pnpm 按 npm `files` 语义打包，目录与 `.tgz` 再按 SHA-256 固化到不可变存储；启用、更新或投影前发现摘要漂移会 fail closed。失败升级会恢复原 manifest、lock、状态和可执行旧包。Cordis CompositionHost 读取受限贡献并启动 AgentProvider sidecar，再把输出投影为稳定 SessionEvent；DSH 插件不能直接写 Kernel store。

CLI 使用方式：

```bash
export KSADK_DSH_HOME=/path/to/an/isolated/dsh-home
export KSADK_DSH_PROFILE=studio

agentengine plugin list
agentengine plugin install @deepseek-ai/dsh-subagent-codex --accept-host-permissions
agentengine plugin disable @deepseek-ai/dsh-subagent-codex
agentengine plugin enable @deepseek-ai/dsh-subagent-codex
agentengine plugin profile
agentengine plugin uninstall @deepseek-ai/dsh-subagent-codex
```

`agentengine plugin dsh ...` 仅是早期脚本的兼容别名；新脚本使用上面的顶层命令。

### 安装后如何在 Studio 使用

“已安装”只说明制品进入隔离 Profile，不会改写已有 Agent，也不等于插件已经可执行。稳定的使用路径是：

1. 在 **插件** 页面确认来源与宿主权限，完成安装；新插件默认保持停用。
2. 显式启用插件，等待其状态从“已启用”进入“可用”；AgentProvider 还必须完成 Host 握手和 conformance，不能只看安装成功。
3. 按插件贡献的 slot 使用：

| 插件贡献 | Studio 中的使用入口 |
| --- | --- |
| `agent.provider/v1` | 在创建或编辑 Agent 时选择“外部 Provider”，再选择该插件提供的 Runtime；构建会锁定精确 `providerRef`，已有 Agent 不会自动切换 |
| route / sidebar / workspace tab | 启用后由 DSH Client CompositionHost 自动挂载；停用或卸载后对应入口必须消失 |
| MCP / Skill / Tool / Store / Context | 先进入能力目录，再在 Agent 编辑页显式绑定；安装插件本身不会把能力注入所有 Agent |
| Renderer / A2UI | 只渲染声明且被当前会话引用的内容；未知 Item 安全降级，不执行任意 payload |
| Codex plugin | 在 Codex Runtime 内由 Codex App Server 加载；不会成为 DSH Runtime，也不会影响 ADK/LangGraph Agent |
| SubagentProvider | 只供支持该 child/subagent 合同的顶层 Provider 调度，不会出现在顶层 Runtime 选择器 |

插件详情必须区分 `installed → enabled → ready → bound`，并显示贡献能力、Provider 引用和使用中的 Agent；失败状态不能伪装成“已启用”。当前 `@deepseek-ai/dsh-subagent-codex` 属于 SubagentProvider，不是顶层 AgentProvider，不能用它替代 KsADK Harness/Codex Runtime。

DSH package 可能执行安装脚本和宿主代码，而其 bundle manifest 不提供完整运行时权限清单，所以安装和升级必须显式传入 `--accept-host-permissions`。`KSADK_DSH_BIN` 可指定已审核的精确 DSH 可执行文件；未配置且 `PATH` 中不存在 `dsh` 时返回 typed unavailable，不会降级为自行解释 DSH 包。

开发者无需检出或编译 DeepSeek Harness 源码。KsADK 管理的工具链会从公网 npm 安装固定版本的官方 DSH，并用它验证标准 Bundle：

```bash
# 一次安装固定、隔离的 DSH/pnpm 工具链
agentengine plugin toolchain install

# 创建、校验、打包一个标准 DSH Bundle
agentengine plugin create ./my-plugin --name @example/my-plugin
agentengine plugin validate ./my-plugin
agentengine plugin pack ./my-plugin
```

普通插件开发、CI 和发布只能以上述固定 npm 版本为兼容基线。本地 DSH 源码若包含尚未发布的改动，编译结果可能与 npm 包不同；只有 DSH 核心开发者验证下一版兼容性时，才通过 `KSADK_DSH_BIN` 显式覆盖，并且版本不匹配会 fail closed。KsADK 包装官方命令，不复制 DSH 的编译器或维护另一套 Bundle 语义。

```bash
KSADK_DSH_TOOLCHAIN_E2E=1 \
uv run --extra all pytest -q tests/e2e/test_dsh_managed_toolchain_e2e.py
```

该门禁验证生成的标准 Bundle 源目录和 `.tgz`，并重新安装、校验官方 `@deepseek-ai/dsh-subagent-codex` 的包边界；它不宣称该 Codex Bundle 的 Provider/child 执行链已经完成。独立 Provider E2E 使用同一个仓外 local-dir reference 完成 install → enable → 两轮状态延续 → disable 后拒绝执行 → 损坏升级失败并回滚 → re-enable 后旧 Provider 再执行 → uninstall/dispose。发布总门禁还会用真实浏览器验证 Studio 装载已安装 client bundle 及停用清理，并验证 ConversationItem、审批和 A2UI 投影；未通过对应 conformance 的能力不能在 Studio 标记为可用。
