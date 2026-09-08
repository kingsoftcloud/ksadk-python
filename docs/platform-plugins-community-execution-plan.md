# 平台插件、Codex Provider 与社区贡献研发执行计划

更新时间：2026-09-08。本文是本次工作的持续任务清单，任务完成必须补充提交与实际验证，不能只更新为“完成”。

## 目标与工作边界

在保留社区贡献的基础上，接续平台资源插件实现，并把 Codex 的产品能力收敛到 Agent Provider 插件。Runtime 只保留必要的生命周期、协议与框架适配；身份、权限、密钥、审批和审计继续由可信宿主管理。插件化不能产生第二套会话、事件流或审批执行链。

- 工作树：`codex/platform-resource-plugins-checkpoint`；不切换或覆盖其他会话的 checkout。
- 起点：贡献者 fork 的 `89a59c48`；合入 GitHub main `ff505c58` 后提交 `15609732`。
- 保留社区原作者的提交及 Author 字段，维护者补修单独提交。优先 merge commit，避免重建或 squash 掉原贡献。
- 不改公开版本号，不触发正式发布，不绕过 main 的保护检查；更新 main 后再回合到本分支。
- 测试、真实本地工具链、浏览器、真实平台访问分别记录；mock 通过不能替代平台验收。

## 执行顺序与验收

| 编号 | 工作 | 状态 | 验收与依赖 |
| --- | --- | --- | --- |
| P01 | 定位 checkpoint、双轴评审、合入最新 main | 已完成首轮 | `15609732`，六处冲突保留双方有效改动；后续 main 更新仍需回合 |
| P02 | 完成合并后针对性回归 | 已完成首轮 | 资源相关 462 passed / 12 skipped；API 补测 25 passed / 1 skipped；Studio 构建及修改文件 Ruff 通过 |
| C01 | 评审人工社区 PR #64 | 已完成评审及本地补修 | 保留原提交 `1a719361`；合并基线 `58c5fbe0`；补修 `5d0592cb`；78 passed / 1 xpassed；待公开门禁和远端合并 |
| C02 | 合并 Actions 更新 #42/#32/#30/#29 | 进行中 | 更新 base、审阅新 diff、public-preflight 与最新 CI；#42 已更新 base。新增 workflow 遗漏更新由维护者补齐 |
| C03 | 修复 Actions #40 的冲突与版本硬编码测试 | 待执行 | 只修改 action 引用和保留原测试目的的断言，完整工作流检查 |
| C04 | 评审并处理依赖 PR #48/#49/#46/#47/#41 | 已评审，待维护 | OTel 联合解算与 ADK1/2 验证、E2B lock/接口测试、websockets 的实际版本限制写清；不能声称上界扩大即已验证新版本 |
| P03 | 修复 checkpoint 的可证实兼容性问题 | 进行中 | 核对资源模块导入是否影响 Windows Studio/CLI；补最小回归，不用静默无锁降级 |
| D01 | Codex Runtime / Provider 职责与入口审查 | 进行中 | 列出实际调用者、内置 manifest、加载路径、可迁移能力与兼容入口；避免仅搬文件 |
| D02 | 实施首批 Codex Provider 能力迁移 | 待 D01 | Provider 承担明确职责，Runtime 通过既有注册与契约调用；插件禁用/缺失有明确错误；不引入循环依赖 |
| D03 | Codex 回归与兼容验证 | 待 D02 | 会话恢复、stream、取消、审批、工具、模型/MCP 配置、既有 CLI/API；真实 app-server stub 或可用工具链验证 |
| P04 | 接通平台资源的可信准入 | 待实现 | 显式凭证主体验证、目标与 region 校验、资源权限校验、失败关闭。现有连接声明不构成身份凭证 |
| P05 | 知识库插件接入正式 Build / Run | 待 P04、D02 | 冻结绑定、正式 Builder 产物、同一 DSH Core、Worker 调用、租约/连接变更隔离、模型可实际调用 |
| P06 | Studio 平台资源选择与状态 | 待 P04 | 复用现有 React Studio 与 ComponentConfig，连接范围内选项代理、保存/刷新/错误状态；真实浏览器验证 |
| P07 | Memory 与 Skill Center 分批接入 | 待 P05 | Memory 显式/自动语义分别验收；Skill 指令优先、固定版本字节、动态发现与执行审批分别验收 |
| P08 | 云端交付与完整验收 | 待 P05–P07 | 对齐 cloud-plugin-delivery-plan.md、T0–T7/A01–A26；不可变插件恢复与无认证调用先验证，OAuth 按独立合同推进 |
| R01 | 最终独立评审、文档与交付 | 持续 | 最新 SHA、实际测试/跳过原因、远端 PR 状态、剩余条件。完成项要可复现，未做项不得标为打通 |

## 已发现的关键边界

1. 现有 CodexProvider 已经存在，当前 `codex/runtime.py` 仍有约 1080 行，`plugins/providers/codex.py` 约 868 行。不能假设“新增 Provider 文件”就完成瘦身；必须让正式执行入口使用它，同时保留兼容入口。
2. Checkpoint 的资源底座适合作为开发起点，但尚未连接完整的正式 Builder/Run；`RESOURCE_AUTHORITY_UNVERIFIED` 当前是必要拒绝边界，不能删掉来伪装接通。
3. 平台 `ListKnowledgeBases` 能提供目录，但不返回认证主体，目录可见也不等于检索有权限。现有 IAM 反查可研究复用于显式签名子账号，旧全局缓存、主账号返回空等行为不能直接作为准入证明。
4. main 已恢复 React Studio 源码，并采用同一官方 DSH Core；不恢复旧的 sandbox relay/client bundle 或额外 iframe Runtime。
5. PR #64 的首项审批投影是已有范围限制。本次补修解决并行中断定向恢复及不同 scope 下相同 call ID 的去重；完整批量审批 UI 不以此项测试通过代替。

## 当前验证记录

- 合并资源回归：`/tmp/ksadk-checkpoint-core-tests.log`，462 passed / 12 skipped。
- 合并 API 回归：`/tmp/ksadk-checkpoint-api-tests.log`，25 passed / 1 skipped。
- Studio 构建：`/tmp/ksadk-checkpoint-studio-build.log`，成功。跳过的实际 Core、CLI、E2B 测试不计入已验收。
- 社区 PR #64：独立工作树 `community-pr64-20260908/review-pr64.md`；新增十项回归在补修前七项失败，补修后十项通过。
- 依赖评审：`/tmp/ksadk-community-deps-review-20260908.md`，包含十二组隔离解算和官方兼容性来源；还不代表最终业务回归或远端沙箱验收。

## 更新规则

每完成一项，更新状态、提交和验证；外部条件不足时记录具体缺口，继续独立可做项。远端合并须再次确认 PR head 与通过检查对应，使用普通 merge 保留作者记录。计划和目标同时存在，不能因处理社区 PR 而丢失平台资源或 Codex 瘦身任务。
