# 平台插件、Codex Provider 与社区贡献研发执行计划

更新时间：2026-09-08。本文是本次工作的持续任务清单，任务完成必须补充提交与实际验证，不能只更新为“完成”。

## 目标与工作边界

在保留社区贡献的基础上，接续平台资源插件实现，并把 Codex 的产品能力收敛到 Agent Provider 插件。Runtime 只保留必要的生命周期、协议与框架适配；身份、权限、密钥、审批和审计继续由可信宿主管理。插件化不能产生第二套会话、事件流或审批执行链。

- 工作树：`codex/platform-resource-plugins-checkpoint`；不切换或覆盖其他会话的 checkout。
- 起点：贡献者 fork 的 `89a59c48`；首次合入 main `ff505c58` 后提交 `15609732`。社区 #64/#42 合并后，再合 main `700a7f87`，提交 `dfb37769`。
- 保留社区原作者的提交及 Author 字段，维护者补修单独提交。优先 merge commit，避免重建或 squash 掉原贡献。
- 不改公开版本号，不触发正式发布，不绕过 main 的保护检查；更新 main 后再回合到本分支。
- 测试、真实本地工具链、浏览器、真实平台访问分别记录；mock 通过不能替代平台验收。

## 执行顺序与验收

| 编号 | 工作 | 状态 | 验收与依赖 |
| --- | --- | --- | --- |
| P01 | 定位 checkpoint、双轴评审、合入最新 main | 已完成首轮 | `15609732`，六处冲突保留双方有效改动；后续 main 更新仍需回合 |
| P02 | 完成合并后针对性回归 | 已完成首轮及真实 Core | 资源相关 462 passed / 12 skipped；API 补测 25 passed / 1 skipped；Studio 构建及 Ruff 通过；`fe2ea692` 将真实资源启动测试接到官方完整 Core，2 项通过 |
| C01 | 评审人工社区 PR #64 | 已合入 main | 合并 `a79fcfc7`；原提交 `1a719361` 保留，维护补修 `5d0592cb`、`d956a06e` 单独留痕。精确候选的 CI、完整 public-preflight、浏览器与制品安装均通过 |
| C02 | 合并 Actions 更新 #42/#32/#30/#29 | #42 已合，其余进行中 | #42 最新候选 `bc4e962e` 完整门禁通过，合并 `700a7f87`；#29 `c99b8bee` 已含最新 main，待完整门禁；#30/#32 已准备轻量验证候选 |
| C03 | 修复 Actions #40 的冲突与版本硬编码测试 | 已补修并推回 PR | `447e85a8` 保留 main 的条件判断、补齐 Node v7、用步骤顺序替换固定 v4 断言；候选 `8ad10304` 完整预检/CI通过，合并前仍需更新 main |
| C04 | 评审并处理依赖 PR #48/#49/#46/#47/#41 | #48 已补修并推回，其他已评审 | #48 `7f811eb5` 将 E2B 实际锁到2.45.1并同步提示，27项沙箱回归通过，带公共门禁修复后 `2ba70f87` 待CI/公开预检；OTel需联合解算与ADK1/2验证；websockets仍受ADK上界约束 |
| P03 | 修复 checkpoint 的可证实兼容性问题 | 已完成首项 | `869ac46e`：可在无 fcntl 环境导入并创建 Studio；资源锁不可用返回 501，不降级为无锁 I/O；10 项通过 |
| D01 | Codex Runtime / Provider 职责与入口审查 | 已完成首轮 | 已有官方 Provider；直连路径和 Provider 必须共用原生工厂，避免默认 registry 递归；正式 Studio 迁移另列 D04 |
| D02 | 实施首批 Codex Provider 能力迁移 | 已完成首批 | `0c9b2dde`：Provider 原生工厂与输入投影；Runtime 1080→842 行，通用 factory 484→172 行；保留兼容构造入口 |
| D03 | Codex 回归与兼容验证 | 已完成首批 | 70 项通过；真实 Codex App Server + 本地 Responses/MCP stub 1 项通过（两轮工具调用和 native thread 续接） |
| D04 | 正式 Studio Build / Run 进入 Provider | 本地接线已完成 | `61a70be2`：新 Build 冻结 Provider/Bundle，Run 经 PluginHost，禁用拒绝，历史 Build 显式 legacy；完整253项、最后收紧后71项通过。再合最新 main 后33项通过；真实 DSH 注册组合验证进行中，云端迁移/多连接模型/附件清理另行推进 |
| P04 | 接通平台资源的可信准入 | 待实现 | 显式凭证主体验证、目标与 region 校验、资源权限校验、失败关闭。现有连接声明不构成身份凭证 |
| P05 | 知识库插件接入正式 Build / Run | 待 P04、D02 | 冻结绑定、正式 Builder 产物、同一 DSH Core、Worker 调用、租约/连接变更隔离、模型可实际调用 |
| P06 | Studio 平台资源选择与状态 | 待 P04 | 复用现有 React Studio 与 ComponentConfig，连接范围内选项代理、保存/刷新/错误状态；真实浏览器验证 |
| P07 | Memory 与 Skill Center 分批接入 | 待 P05 | Memory 显式/自动语义分别验收；Skill 指令优先、固定版本字节、动态发现与执行审批分别验收 |
| P08 | 云端交付与完整验收 | 待 P05–P07 | 对齐 cloud-plugin-delivery-plan.md、T0–T7/A01–A26；不可变插件恢复与无认证调用先验证，OAuth 按独立合同推进 |
| R01 | 最终独立评审、文档与交付 | 持续 | 最新 SHA、实际测试/跳过原因、远端 PR 状态、剩余条件。完成项要可复现，未做项不得标为打通 |

## 已发现的关键边界

1. 现有 CodexProvider 已经存在。已把原生装配和输入投影收敛到 Provider，Runtime 从 1080 行减至 842 行；新 Studio 本地 Build 已进入 PluginHost。旧 Build 显式兼容、云端格式尚未迁移，不能宣称所有 Codex 运行已经受插件开关控制。
2. Checkpoint 的资源底座适合作为开发起点，但尚未连接完整的正式 Builder/Run；`RESOURCE_AUTHORITY_UNVERIFIED` 当前是必要拒绝边界，不能删掉来伪装接通。
3. 平台 `ListKnowledgeBases` 能提供目录，但不返回认证主体，目录可见也不等于检索有权限。现有 IAM 反查可研究复用于显式签名子账号，旧全局缓存、主账号返回空等行为不能直接作为准入证明。
4. main 已恢复 React Studio 源码，并采用同一官方 DSH Core；不恢复旧的 sandbox relay/client bundle 或额外 iframe Runtime。
5. PR #64 的首项审批投影是已有范围限制。本次补修解决并行中断定向恢复及不同 scope 下相同 call ID 的去重；完整批量审批 UI 不以此项测试通过代替。

## 当前验证记录

- 合并资源回归：`/tmp/ksadk-checkpoint-core-tests.log`，462 passed / 12 skipped。
- 合并 API 回归：`/tmp/ksadk-checkpoint-api-tests.log`，25 passed / 1 skipped。
- Studio 构建：`/tmp/ksadk-checkpoint-studio-build.log`，成功。跳过的实际 Core、CLI、E2B 测试不计入已验收。
- 社区 PR #64：`/tmp/ksadk-pr64-review.md`；新增十项回归在补修前七项失败，补修后十项通过。精确候选 `d956a06e` 完整 public-preflight 通过，已用 merge commit 合入，原作者提交保持 main 祖先关系。
- Codex 首批瘦身：`/tmp/ksadk-codex-provider-thinning-review.md`，实际 App Server 调用使用本地测试服务，不代表真实模型或生产验收。
- 资源真实 Core：`/tmp/ksadk-checkpoint-resource-real-core.log`，DSH `0.1.2-rc.1` + pnpm `10.33.2`，2 passed / 73.87s。实际启动官方 Web/Core、安装四个资源插件、显式迁移 isolated 布局、冻结 Build、启动 Worker、经 MCP 调用所选 KB、关闭并清理 socket/进程；上游 KB 是本地测试服务。此前空白自定义 profile 缺少 Core Web/connection 服务而失败，现改用官方 profile，未新增第二个 Core 或放宽 admission。
- #48 E2B：独立环境按更新后的锁文件安装 E2B 2.45.1；`/tmp/ksadk-pr48-sandbox-tests.log` 27 passed。测试来自内部既有 sandbox suite，复制在候选外并以候选代码运行；验证后端行为和新 SDK 接口，未创建真实远端沙箱。
- 社区公开预检共同修复：首次官方 DSH bootstrap + Provider 初始化实测 7.28 秒，原浏览器 helper 的约5秒窗口提前报失败；`d956a06e` 改为 monotonic 30秒预算并在服务线程退出时立即失败，仍以真实 health HTTP200 为成功。没有禁用 DSH 或跳过浏览器断言。
- 依赖评审：`/tmp/ksadk-community-deps-review-20260908.md`，包含十二组隔离解算和官方兼容性来源；还不代表最终业务回归或远端沙箱验收。

## 更新规则

每完成一项，更新状态、提交和验证；外部条件不足时记录具体缺口，继续独立可做项。远端合并须再次确认 PR head 与通过检查对应，使用普通 merge 保留作者记录。计划和目标同时存在，不能因处理社区 PR 而丢失平台资源或 Codex 瘦身任务。

## Codex 首批实现边界

通用 Runtime registry 与 Codex AgentProvider 都委托 `plugins/providers/codex_native.py`。该工厂负责模型/MCP 覆盖、隔离 CODEX_HOME、原生插件 bootstrap 和绑定 Skill 准备；Provider 使用只注册 native Codex 的 registry，避免回到上层 PluginHost 解析形成递归。

`codex/projection.py` 定义 typed turn projection；`plugins/providers/codex_turn.py` 实现 Skill、canonical history、图像、mention 与附件投影。`codex/runtime.py` 只消费投影，保留 thread/turn 生命周期、stream、审批、取消、bootstrap-before-thread 和事件映射。旧的直接构造方式也委托同一投影实现。

当前迁移保留附件内容寻址留存。每回合 adapter 关闭不等于原生 thread/session 结束，不能在 adapter.close 时删掉后续恢复可能使用的附件。D04 需要把真实的清理所有权交给 session/activation 宿主。

## 新 Studio Build 的本地 Provider 合同

`CodexBuildRecord` 保存 `localExecution=provider` 与 `providerBundle`，其中包含 Provider 引用、注册摘要、内部 Bundle Build ID 和内容摘要。历史记录缺少这些字段时明确读作 legacy。内部 Bundle 指纹绑定原 manifest、模型快照/ID 和原生插件 lock；外层 Build ID 再绑定 Bundle 引用，同输入重复构建复用记录。

正式 `run_build` 和 Scheduler 使用 PluginHost 提供的适配器，继续复用既有 RuntimeExecutor 的事件、完整输入、审批恢复和取消循环。当前 Provider 未注册、禁用、摘要变更或 Bundle 缺失时，新 Build/Run 明确拒绝，不回退到旧直连 Kernel。受信任宿主从原 Codex resolver 传入工作目录、CODEX_HOME、原生插件 bootstrap、模型与沙箱配置，凭证不写入新 Bundle。

Skill 输入最终以 Bundle 冻结字节为准，本地 launch overlay 不得覆盖这些路径。验证覆盖同名内容变化、目录改名、未知 Skill 拒绝，以及原生 plugin lock、模型覆盖、审批和取消。不同连接的多模型暂明确拒绝；同连接多模型和 YAML-only 模型清单继续支持。

提交 `61a70be2` 的完整 Studio/Codex 253 项通过；最后两处收紧后的71项通过；合入 main `700a7f87` 后相关33项通过（含真实 App Server + 本地 Responses stub），日志 `/tmp/ksadk-checkpoint-latest-main-provider-tests.log`。这些测试的 DSH 注册使用 fixture，真实 Core 注册与正式运行的组合验证单独记录，不混同生产、浏览器或云端验收。

## 社区 PR 台账

| PR | 当前状态 | 下一步 |
| --- | --- | --- |
| #64 | 已合并 `a79fcfc7` | 已回合开发分支 |
| #42 | 已合并 `700a7f87` | 已回合开发分支 |
| #29 | 候选 `c99b8bee` 已推回，基线 main700a7f87 | 完整预检和最新 CI 后合并 |
| #30 | 候选 `bce8eb7d`，保留 pnpm 9/10.33.2 | 按顺序更新 main、完整预检、推回及合并 |
| #32 | 候选 `c7674158`，全部11处 checkout v7 | 按顺序更新 main、完整预检、推回及合并 |
| #40 | 候选 `8ad10304` 已推回，原基线完整预检通过 | 更新 main、验证后合并 |
| #48 | 候选 `2ba70f87` 已推回，原基线完整预检通过 | 更新 main、验证后合并；未验证真实远端沙箱 |
| #49/#46/#47 | 已完成约束审阅，分别准备 lock 元数据 | 联合实际版本验证后按顺序处理，保留三项原始贡献 |
| #41 | 已完成约束审阅，准备 lock 元数据 | 验证实际仍解析15.0.1，不宣称16兼容 |
