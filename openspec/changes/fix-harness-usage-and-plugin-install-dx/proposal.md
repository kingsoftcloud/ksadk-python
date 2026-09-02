# Fix: Harness 用量上报与 DSH 插件安装体验

## Why

2026-09-02 的 Studio 全流程实测（`docs/2026-09-02-harness-studio-e2e-report.md`）发现三个直接影响开箱体验与可信度的问题：DSH Harness 链路的 Token 用量在运行检查器中恒为 0（与 0.8.3 "report model token usage to Studio" 的目标相悖）；`agentengine plugin install` 传相对路径时会在 DSH Profile 目录下解析出死链依赖并安装失败；DSH 工具链版本不匹配/未安装的 CLI 报错不给出可执行的修复指引（本次实测中靠人工考古定位到 `AGENTENGINE_PNPM_BIN` 方案）。

## What Changes

- `_StudioHarnessReasoner.complete`（`ksadk/studio/plugin_runtime.py`）把 `ModelResponse.usage` 映射进 `HarnessReasoningTurn.usage`，使 Studio PluginHost/Harness 链路的每次模型调用用量进入 `usage.reported` 事件与运行记录（修复运行检查器「总 Token 0」）。
- `DshProfilePluginBridge._prepare_source`（`ksadk/plugins/bridges/dsh.py`）对"相对 cwd 可解析且存在"的本地源路径先绝对化再交给 dsh CLI；对形如本地路径但不存在的 `.tgz` 源直接给出明确错误，杜绝死链 `link:` 依赖被写入 Profile。
- `ksadk/cli/cmd_plugin.py` 的 DSH 工具链错误映射补充可执行指引：版本不匹配时透出期望/实际版本与 `AGENTENGINE_PNPM_BIN`/corepack 两种修复路径；工具链不可用时提示 `agentengine plugin toolchain install`。
- 推理内容透出：模型客户端捕获 `reasoning_content`（Responses wire 捕获 reasoning 输出项），经 `HarnessReasoningTurn.reasoning` 传入引擎；引擎在每次推理轮次产出非空推理文本时发射 `item_kind="reasoning"` 的 RuntimeEvent，Studio 既有投影将其呈现为 `thinking.*`（思考过程卡片）。
- Harness Runtime 授权体验：通过模板默认创建（未显式提供 spec）且 Runtime 为 harness 的 Agent，预置 DSH harness provider 必需的 `process:host-user` 权限；显式提供的 spec 不被改写（保持"显式拒绝必须被拒"的安全语义）。权限不足导致的 `PLUGIN_PERMISSION_DENIED` 错误携带可执行指引（在 Agent 安全设置中允许 `process:host-user` 后重新构建）。

不改变：DSH Profile 的发现/预检/注册语义、显式 spec 的授权语义（`process:host-user` 显式声明仍可被拒绝）、以及 openai 兼容模型客户端的用量解析行为。

## Capabilities

### New Capabilities

- `studio-plugin-runtime-observability`: Studio PluginHost（含 DSH Harness provider）运行的可观测事实——模型调用用量必须真实上报、推理内容必须作为 reasoning 事实透出，不得以 0/静默丢弃顶替。
- `dsh-plugin-toolchain-dx`: DSH 插件源安装与工具链诊断体验——本地源路径解析确定性、死链防护、版本不匹配与未安装的可执行修复指引。
- `studio-harness-authoring`: Harness Runtime 的授权体验——模板默认创建预置 provider 必需权限、显式 spec 语义不变、权限不足失败给出可执行指引。

### Modified Capabilities

<!-- 无：现有主 spec 尚未从 complete-ksadk-harness-core 同步，本次以新 capability 承载，避免与未同步 spec 产生 delta 冲突。 -->

## Impact

- 代码：`ksadk/studio/plugin_runtime.py`、`ksadk/plugins/bridges/dsh.py`、`ksadk/cli/cmd_plugin.py`（均为小步修改，无 schema/协议变更）。
- 测试：`tests/studio/`（reasoner usage 映射 + usage.reported 链路）、`tests/plugins/`（相对路径安装、不存在 .tgz 源）、`tests/cli/`（错误指引文案）按 TDD 先红后绿。
- 用户可见：运行检查器 Token 数值恢复真实；`plugin install` 相对路径可用；工具链报错自带修复指引。
