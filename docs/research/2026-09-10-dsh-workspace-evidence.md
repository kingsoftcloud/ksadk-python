# DeepSeek Harness 多工作区与用户配置源码证据

日期：2026-09-10。研究对象：官方仓库 `deepseek-ai/deepseek-harness`，固定提交 `aa8262ec091698bae9a6b04773a6b5b06ad4aef2`。本地工作树只读；本次执行 `git ls-remote origin refs/heads/master`，远端 master 与该提交相同。下列链接均固定到该提交，不依赖以后移动的 master。

## 结论与适用范围

DSH 的多工作区首先是**目录注册、会话归属和导航**，并非每个工作区一套独立应用、账号域或操作系统进程。创建会话时确定目录，此后恢复和 fork 保留目录；打开另一个工作区是在那里选择或创建会话，没有给原会话改 cwd。用户级模型设置和凭据服务被多个会话共享。[工作区定义][workspace-doc]、[创建入口][create]、[客户端导航][navigation]、[默认服务装配][base]

因此，DSH 支持“一个入口管理多个目录、会话固定目录”的产品方案，**不支持用它证明“多工作区必须每目录独立进程”或“工作区天然隔离云账号”**。KsADK 是否阶段性采用工作进程，应依据其进程环境变量、插件和项目依赖的实际耦合决定；这是 KsADK 的工程判断，不是 DSH 事实。

## 用户目录、设置与凭据

| 已核对事实 | 源码证据 |
| --- | --- |
| Harness home 默认 `~/.dsh`，可由显式路径或 `DSH_HOME` 覆盖，所有用户数据共享这一根目录。 | `packages/util/home-paths/src/index.ts:76–90`，[固定源码][home] |
| 默认用户设置存于 `settings.yaml`，按插件 namespace 组织；文件 provider 监听外部修改，写入在跨进程锁中重读并做增量修改。 | `packages/settings/settings-file/src/index.ts:1–6,21–30,50–61`，[固定源码][settings] |
| 设置服务的基础叠加是 schema 默认、装配 base、用户 namespace；这套接口没有以 workspaceId 为维度的项目覆盖。每个 owner 声明设置实时或重启生效。 | `packages/settings/settings/src/index.ts:45–73`，[固定源码][settings-seam] |
| 凭据另存于 `.credentials.yaml`，共享 home，可提供 API key 或授权 record；默认装配明确不把这个受管理存储写回 `process.env`。 | `packages/credentials/credentials-local/src/index.ts:60–92` 与 `packages/bundle/base/cordis.patch.yml:87–98`，[凭据源码][credentials]、[装配][base] |
| 引用凭据优先级为非空继承环境 > 用户受管理凭据 > **启动目录** `.env` > home `.env`；环境覆盖显示只读，避免用户在 UI 保存却不生效。 | `packages/credentials/credentials-local/src/index.ts:557–570,628–638`，[解析与来源][credential-resolution]；行为断言见下节。 |
| 启动环境按来源保留不可变快照；后续 `chdir`、工作区切换、会话恢复不重新装载目标目录 `.env`。 | `packages/util/launch-environment/src/index.ts:31–35,78–102`，[固定源码][launch-env] |
| Session 日志默认统一放在 home 的 `sessions` 下，工作区不是独立日志目录。 | `packages/bundle/base/cordis.patch.yml:110–113`，[默认装配][base] |

**对 KsADK 的启发（设计建议）**：先做一个清楚的用户默认配置入口和来源展示；配置文件数量与用户认知复杂度应分开讨论。DSH 对设置、凭据和状态分别存储，但用户不需要逐个维护文件。不要简单复制 DSH 的优先级：KsADK 的项目部署参数、云连接选择和命令行显式覆盖需要独立定义，云 AK/SK/控制面应作为完整连接处理。DSH 当前这条启动环境路径不能证明“切到 B 就自动读取 B 的 `.env`”。

## 工作区、会话和进程模型

1. **注册的是目录记录。** 存储 schema 包含 `path/title/sessionIds/createdAt/updatedAt`；registry 维护展示顺序、归档集合和恢复标记。路径做 realpath 规范化，符号链接到同目录不能注册为不同身份。这里没有凭据、工作进程 ID 或独立运行时配置字段。`packages/workspace/workspace/src/spec.ts:17–75`，[schema][workspace-schema]；`paths.ts` 的规范化契约见[工作区文档][workspace-doc]。
2. **成员关系有双重约束。** Session ID 必须出现在工作区记录中，同时 SessionHeader.cwd 的规范路径必须等于工作区路径。`attachSession` 读取真实 header 并拒绝缺失 cwd、非目录及不匹配路径；不能只改列表把 A 会话搬到 B。`packages/workspace/workspace/src/entity.ts:109–148`，[固定源码][membership]。
3. **选择工作区作用于创建会话。** API 拒绝同时传 `workspaceId` 和 `cwd`；解析出 `workspace.path ?? request.cwd ?? defaultCwd`，创建或恢复 Agent 后再 attach。已有 ID 对不同 cwd 的采用请求报冲突。`packages/api/session-controller/src/commands.ts:87–125`，[创建][create]；`agent.ts:450–486`，[恢复与冲突][adopt]。
4. **Session cwd 是不可变创建元数据。** Session.header 是 detached/deep-frozen 创建元数据，存储恢复把原 header 传回；不是可追加的 cwd-change 事件。`packages/core/session/src/index.ts:456–464,530–542,585`，[固定源码][session-header]。直接修改 DB/日志不属于受支持操作，而且工作区成员投影会滤除 cwd 不一致项，不能用来实现产品迁移。
5. **fork 也留在原目录。** API fork 创建新的 ID、复制已选事件前缀，并把 `source.header.cwd` 写入子会话 metadata；没有目标工作区参数。`packages/api/session-controller/src/commands.ts:247–275`，[固定源码][fork]。因此“带摘要去另一工作区继续”需要新产品流程，不能把现有 fork 当现成跨目录搬家。
6. **客户端打开工作区不会改现有会话 cwd。** `connectWorkspace` 复用该目录内已归属的空白会话，否则调用 `sessions.create({ workspaceId })`；`openWorkspace` 随后打开该会话，并用 navigation abort 标记阻止晚完成的旧请求抢回页面。`packages/client/ui-workspace/src/client/navigation.ts:110–170`，[固定源码][navigation]。此处取消的是过期导航，没有取消此前运行任务的调用；这不等于已经实测后台任务不断流。
7. **默认 Web 路径是同一个 Host 下的多个 Agent。** Web bundle 装配一个 workspace 服务与 session controller；controller 在 `ctx.agents` 中 create/resume；Agent service 委托当前 factory，默认 AgentLoop 准备 Session 并 setup/publish 独立的 Agent scope。该调用链没有按工作区 fork/spawn。`packages/bundle/web-app/cordis.patch.yml:75–76,105–120`，[Web 装配][web-bundle]；`packages/core/agent/src/index.ts:388–412`，[工厂委托][agent-factory]；`packages/core/agent-loop/src/index.ts:764–800`，[默认工厂][agent-loop]。工具子进程、workflow worker 或其他 profile 可有进程边界，不应混同为“每工作区一个进程”。
8. **工作区注册不是沙箱。** 独立 sandbox policy 在每个操作边界从 `session.header.cwd` 解析 workspace root，供文件、shell 等执行侧消费；部署/会话模式可以是 read-only、workspace-write 或 danger-full-access。`packages/sandbox/sandbox-policy/src/index.ts:1–18,157–168`，[固定源码][sandbox]。工作区列表本身既不提供租户认证，也不保证全部读取仅限该目录；安全效果取决于具体 capability 与沙箱模式。
9. **删除工作区仅移除注册。** registry 删除记录和顺序，不删除目录、用户文件、会话或持久日志；历史在产品中成为 Ungrouped。重新注册同目录是新身份，不自动接回之前的 sessionIds。`packages/workspace/workspace/src/index.ts:190–199,357–386`，[固定源码][delete]；下节有对应测试断言。

## 已阅读的测试断言与验证边界

本次完成源码、调用链和测试断言检查，**未运行 DSH 测试、真实模型或浏览器**。本地不存在 `node_modules/.bin/vitest`；为只读研究没有安装依赖。测试列出的行为是项目已有断言，不代表本次运行通过。

| 测试文件与位置 | 测试明确验证的行为 |
| --- | --- |
| `packages/workspace/workspace/tests/workspace.spec.ts:510–530`，[测试源码][delete-test] | 删除仅影响注册；目录仍存在；新注册 ID 不同且会话列表为空。 |
| `packages/workspace/workspace/tests/workspace.spec.ts:761–810`，[测试源码][membership-test] | 成员需要 ID 和 canonical cwd 同时匹配；仅 cwd 相同不自动归属；重复所有权、重复路径、顺序损坏报错。 |
| `packages/api/session-controller/tests/agent.host.spec.ts:416–443`，[测试源码][adopt-test] | 持久会话 cwd 冲突被拒绝；恢复前检查归属竞争。 |
| `packages/credentials/credentials-local/tests/local.spec.ts:123–169,211–221`，[测试源码][credential-test] | UI 受管理存储覆盖旧 `.env`；启动项目 `.env` 只在未存凭据时兜底；继承环境最高且不可写。 |
| `packages/client/ui-workspace/tests/workspaces-service.client.spec.ts:257–275`，[测试源码][navigation-test] | A/B 创建乱序完成时只打开最新选择 B，旧请求不能抢回导航。 |

尚未验证：多个真实运行同时读写不同目录、崩溃后后台任务行为、内存与启动成本、真实云账号切换、第三方插件私自读环境的行为。不能从上述源码研究给这些路径出具 E2E 通过结论。

## 对 KsADK 需求取舍的建议

以下是源码研究导出的工程建议，后续整体复核及用户确定的“一期同时交付全局配置、工作区切换、目录关联”已合入 [主方案](2026-09-10-studio-user-config-and-workspaces-plan.md)，产品范围以主方案为准。会话保持所属工作区，不妨碍同一任务按授权读取或修改其他目录。

- **配置复用是一期基础模块**：优先收敛解析、作用范围、来源展示、完整连接选择与迁移；工程上不依赖多工作区管理器，但一期最终须与切换、目录关联一同验收。
- **多工作区入口纳入一期**：提供最近目录、打开项目、按项目列会话和最小后台状态入口。完整任务中心及容量治理留到后续阶段；工作区名称不应暗示多租户或操作系统隔离。
- **不迁移原会话归属**：保留会话默认目录与身份；更换所属工作区采用新会话＋明确选择的摘要。当前任务跨目录读写或单条命令指定其他 cwd 不需要迁移会话。完整 transcript 可能带旧路径、资源身份与工具状态，不能默认原样继承。
- **不把每工作区进程定为产品定义**：先抽出 workspace/session identity 与配置上下文，再按 KsADK 现有全局环境、插件可重入性和项目依赖情况决定执行边界。若用 worker，是兼容当前实现的工程策略；需要测量启动时延、内存、连接重复与空闲回收成本。
- **迁移的验收不是下拉菜单能切换**：应覆盖错目录请求拒绝、会话恢复归属、异步导航竞争、后台运行文件落点、完整云连接不混用、移除注册不删历史、缺失目录及符号链接、全局修改对已运行任务的生效时点。

[home]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/util/home-paths/src/index.ts#L76-L90
[settings]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/settings/settings-file/src/index.ts#L1-L61
[settings-seam]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/settings/settings/src/index.ts#L45-L73
[credentials]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/credentials/credentials-local/src/index.ts#L60-L92
[credential-resolution]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/credentials/credentials-local/src/index.ts#L557-L638
[launch-env]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/util/launch-environment/src/index.ts#L31-L102
[base]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/bundle/base/cordis.patch.yml#L87-L113
[workspace-doc]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/docs/subsystems/workspace.md
[workspace-schema]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/workspace/workspace/src/spec.ts#L17-L75
[membership]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/workspace/workspace/src/entity.ts#L109-L148
[create]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/api/session-controller/src/commands.ts#L87-L125
[adopt]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/api/session-controller/src/agent.ts#L450-L486
[session-header]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/core/session/src/index.ts#L456-L585
[fork]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/api/session-controller/src/commands.ts#L247-L275
[navigation]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/client/ui-workspace/src/client/navigation.ts#L110-L170
[web-bundle]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/bundle/web-app/cordis.patch.yml#L75-L120
[agent-factory]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/core/agent/src/index.ts#L388-L412
[agent-loop]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/core/agent-loop/src/index.ts#L764-L800
[sandbox]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/sandbox/sandbox-policy/src/index.ts#L157-L168
[delete]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/workspace/workspace/src/index.ts#L190-L199
[delete-test]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/workspace/workspace/tests/workspace.spec.ts#L510-L530
[membership-test]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/workspace/workspace/tests/workspace.spec.ts#L761-L810
[adopt-test]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/api/session-controller/tests/agent.host.spec.ts#L416-L443
[credential-test]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/credentials/credentials-local/tests/local.spec.ts#L123-L221
[navigation-test]: https://github.com/deepseek-ai/deepseek-harness/blob/aa8262ec091698bae9a6b04773a6b5b06ad4aef2/packages/client/ui-workspace/tests/workspaces-service.client.spec.ts#L257-L275
