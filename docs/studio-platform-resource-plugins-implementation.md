# Studio 平台资源插件实施与验证记录

状态：开发中，尚未完成资源端到端接入。起点为公开 main 的
`55fe34765501639e97608166c7f45465f530990e`。

## 完整交付范围

知识库、记忆库、Skill 中心通过三个官方 DSH 业务插件和公共资源桥接入。
Studio 提供连接、Agent 绑定、不可变 Build、应用、解绑和真实运行状态。
同一 profile generation 复用一个完整 Core，资源访问经过宿主授权的 Worker。
Codex、LangGraph、DSH 分别接入工具、计划内自动记忆及 Skill 文件和执行能力。
云端复用公共制品、授权和恢复机制，并单独验收。

范围不因阶段性测试通过而缩减。以下阶段均完成后才可关闭项目：

| 阶段 | 必须交付 | 当前状态 |
| --- | --- | --- |
| T0 | 配置、身份、IPC、审批、恢复幂等、兼容矩阵合同及探针 | 进行中 |
| T1 | 显式客户端、broker/Worker、scope v2、撤销、官方公共 Bundle | 进行中 |
| T2 | 知识库选择到真实回答和引用的完整链路 | 未完成 |
| T3 | 专用 MemoryProvider、显式读写、自动生命周期与真实异步状态 | 未完成 |
| T4 | Skill 完整分页、锁定包、消费者文件、加载和执行 | 未完成 |
| T5 | 三引擎真实回合、取消、恢复、能力门禁 | 未完成 |
| T6 | 兼容迁移、云制品与授权接入、云端资源回合 | 未完成 |
| T7 | 生命周期、第三方插件、浏览器、CLI 和交付产物验收 | 未完成 |

## 当前实现

- `ksadk/resource_runtime/contracts.py` 定义 config v1 和可信身份输入。
  拒绝未知字段、重复资源种类、重复 binding ID、不适用策略和不精确的 Skill hash。
  默认策略规范化后计算摘要；记忆分区包含 tenant、资源主体、实例/地域、Agent、
  记忆归属用户，不包含会话或 activation，保证跨会话稳定。
- `SkillServiceClient(allow_env_fallback=False)` 提供新路径的显式构造模式。
  禁止缺省地域、环境地域别名、混合 token/签名认证及半套签名凭证。
  不继承其他账号字段、环境代理、netrc 或下载地址改写；提供 session 关闭入口。
  下层 `AWSV4Auth` 同样接收禁止环境回退标志，避免签名器重新引入其他账号。
  旧调用默认维持环境配置兼容行为。
- 上述合同尚未接入 Studio Build 和 DSH Core 的真实模型调用，不能据此宣称
  完整运行时授权已实现。
- `BoundKnowledgeService` 使用绑定的实例与地域，拒绝模型传入资源/用户字段，
  限制检索数量和正文预算，保留结构化来源及上游 requestId，区分 empty/failed。
  SDK 调用及 last_* 状态复制在同一锁中完成；知识正文、查询和原始异常不进入日志。
- IPC 使用四字节网络序长度前缀和 UTF-8 JSON，最多 1 MiB，拒绝重复字段、
  非有限数字、未知操作、截断帧；deadline 为 Unix 毫秒，传输层仍需实现期限执行。
- `ResourceLeaseRegistry` 管理宿主签发的随机句柄，最长 600 秒；续租旋转句柄，
  不扩大 scope，支持 activation/generation 撤销。一个 activation 不能混用身份或
  Build。它不替代上游授权；执行器的身份解析与续租协调仍待接入。
- `ResourceBroker` 已连接句柄校验与 Worker 端口，拒绝过期、越权、错误参数；
  每 activation 最多八个在途请求，串行派发前重新验权，期限最多 60 秒。
  超时/调用方取消不会提前释放仍在执行的任务名额。撤销后不返回在途检索内容。
  写入仍返回 `RESOURCE_APPROVAL_REQUIRED`，等待完整 Kernel 审批/操作账本接入。
- `ResourceWorkerProcess` 使用独立 Python 子进程、隔离导入和受限环境，初始化
  凭证只通过 stdin 管道传递，普通模型请求不接受连接配置。进程丢失响应时终止，
  不自动重启重放。当前进程支持知识库，记忆和 Skill 操作尚未接入。
- `ResourceSocketServer` 使用私有 Unix socket（目录 0700、socket 0600），复用
  broker；连接数、帧大小、读取和写出期限受限，没有新增 HTTP/MCP/Core 宿主。
  生命周期由资源 supervisor 管理，并随 Studio 的 DSH generation 关闭。
- 新增私有开发 Bundle `dsh-platform-resources`，通过标准 Cordis `provide`
  贡献资源桥。Node client 使用 AsyncLocalStorage 保存调用上下文，通过 Unix
  socket 发起有限操作；支持期限、取消、消息大小、响应关联和有限在途请求。
  尚未发布或预装；MCP 认证投影已接入，native DSH 执行器身份适配仍未完成。
- 新增私有开发 Bundle `dsh-knowledge`，用官方 tools.register 注册检索工具，
  结构化来源和文本视图共用同一结果。模型只能传 query/top_k，执行取消信号
  与宿主上下文信号合并。工具声明遵守所锁定 DSH 版本的 JSON Schema 子集，
  不支持的长度/数量约束由 Python 服务强制执行。
- `ResourceSnapshot` 冻结插件锁摘要、规范化资源配置、连接引用、endpoint、
  tenant/principal 和认证模式；不保存凭证值。Worker 初始化必须与该快照及
  scope 中的快照摘要一致。资源、目标或账号变化会拒绝，声明主体不变时可更换
  密钥值。真实凭证归属依赖宿主的平台验证，不能从声明的 principal 自证授权。
  此快照已用于 Worker 校验，尚待 Studio Build 存储和激活协调器接入。
- `ResourceSupervisor` 统一管理 socket、Worker、句柄和崩溃监视；限制活动 Worker
  数量，拒绝复用已结束的 activation，关闭时撤销并回收所有资源。已接入
  StudioDshCapabilityService 的 generation 刷新、重启和关闭流程；资源激活入口
  校验当前 profile/generation。尚未从 Studio Run/Build 调用该入口，也没有据此
  开放 MCP 资源授权。
- MCP overlay 已支持独立签名的 ks2 令牌，携带资源句柄和冻结运行上下文，
  普通 ks1 工具行为保持兼容。资源插件注册工具归属；ks1 不列出这些资源工具，
  ks1/root 不能直接调用它们。ks2 经现有 tools.execute 进入桥的异步上下文，
  Python broker 再次独立校验句柄。Node scope 撤销与 broker 句柄撤销均已实现，
  Run 生命周期仍需统一协调二者及续租，不能将单层撤销当成全部接入完成。
- `PackageStore` 已验证解压树的内容与文件集合，修改、增删或链接会使缓存失效。
  归档解压在私有暂存目录完成，通过校验后发布；POSIX 进程锁序列化同 key 的
  并发写入。缓存 key 包含 namespace、Skill 身份和 hash，并采用摘要路径。
  提供 `require_hash=True` 模式（要求命名空间与进程锁），尚待 Skill Build/
  执行清单接入。旧缓存目录布局不会被直接复用，首次调用会重新建立校验后的缓存。
  下载和解压均有大小限制；解压拒绝越界路径、链接、重复条目和超量文件。
  实际消费/执行之前的包交付与锁定仍未完成，不宣称消除了同用户进程的所有修改窗口。

Skill 目录分页现已支持 KOP 连续读取，默认最多 20 页；预算耗尽返回 next_page，
重复页、数量变化或短页缺项显式报告 truncated。旧 REST 不假设不存在的分页协议。
公共目录已知总数大于返回数量时同样报告不完整。模型/manifest 消费 active_skills
默认拒绝不完整目录；旧 runtime loader 显式允许部分选择，并在 warnings 中报告。
19 项定向测试覆盖第 101 条、页预算、重复页、跨空间响应、异常 envelope、公共目录
及加载器警告。分页并不提供服务端快照一致性，也不代表 pinned 执行清单已实现。

本地 pinned Skill 执行已有第一条实际消费路径：`runtime/pinned.py` 定义受限归档
清单，宿主校验 ZIP 摘要后复制到私有请求目录；bundled agent 按请求清单安全解压，
不查询远端目录、不加载环境中的本地 Skill、不继承环境中的名称选择。空清单也不
回退到 discovery。清单拒绝非规范归档路径、重复身份/名称和越界选择；消费者重新
校验摘要并拒绝归档符号链接。最多 32 包，每包遵循现有下载/解压限额。
LocalProcess backend 的 pinned 分支只接受 bundled agent，以 Python 隔离导入启动，
宿主环境采用有限系统字段，其余必须显式传入；此处仍是子进程，不是 OS 沙箱。
输出目录由执行器建立，默认每次 pinned 调用使用独立保留的产物目录。
10 项定向测试包含真实子进程执行脚本和读取引用文件、可读取产物、源解压树被修改、
消费者 ZIP 被修改、符号链接、无关环境凭证、非法清单与选择。尚未接通 Studio
Build、资源审批、真实 E2B 验收、依赖环境校验及运行 receipt；不能据此关闭 T4。

E2B backend 现已接入同一 pinned 包输入，先检查已安装 agent 模块的协议版本，再
创建私有交付目录并传送校验后的 ZIP 和版本化请求。执行使用已检查的安装模块，
不调用可能不同版本的镜像脚本副本。旧版本/异常握手拒绝后不上传包、不执行任务；
传输失败回收会话。显式输入文件限定到 `/workspace/inputs`，避免覆盖运行模块和
包清单。请求拒绝未知协议或缺少清单，不能静默降级到 discovery。
当前远程测试是 sandbox 文件/命令传输替身加真实本地消费子进程，不是真实 E2B。
已补 pinned 沙箱产物取回：agent 仅打包执行工作目录内的普通文件，拒绝越界和
符号链接，限制 100 个文件、单文件 20 MiB、总计 100 MiB。宿主经流式二进制读取
取回归档，校验大小、SHA-256、条目数量和解压路径后保存到私有产物目录；完成后
才关闭会话。结构化 output_files/artifacts 替换为宿主路径。任何验证失败均不返回
部分交付文件。协议探针同时检查归档交付能力，旧镜像不静默丢失产物。
传输替身测试在 kill 时删除模拟沙箱目录，验证产物在删除后仍可读取；另覆盖二进制
内容、越界、链接、摘要变化、超限、流关闭和已有目标保护。真实 E2B 仍未验证。
二进制流读取接口按 E2B 官方 Python SDK 文档实现：
https://e2b.dev/docs/sdk-reference/python-sdk/v2.5.0/sandbox_sync
当前环境未安装 E2B SDK，未用已锁定版本的真实 Filesystem/网络调用验证此接口。

资源 Build 制品辅助模块现可在调用方的全新 staging 目录保存绑定快照和完整 pinned
ZIP，并返回用于外层 Build 记录的摘要引用；不创建独立云上传/授权/资源注册库。
包集合必须精确对应 selectedSkills，保存时验证摘要、解压边界和包名，失败清理本次
暂存目录；已有目标不会覆盖。恢复验证外层引用摘要、主体/连接快照、文件集合与
归档内容，使用快照摘要隔离缓存，不需要在线目录或凭证。8 项测试包含删除源缓存
后的离线恢复及真实脚本执行、篡改/多余文件/符号链接、缺包、非法 ZIP 和已有目标。
尚未从 CodexStudioBuilder 调用：当前 `_native_plugin_lock` 仍拒绝 DSH ecosystem，
必须先补精确 DSH 安装锁的构建/激活适配，再接入此制品。该辅助模块不代表 Studio
Build/Run 已支持资源插件，也未包含镜像依赖准入和公共云制品索引。

DSH bridge 新增 `snapshot_for_build` / `verify_build_snapshot`，在 profile 生命周期
读锁内组合 Core 版本、配置摘要、pnpm 锁文件摘要和已安装 node_modules 文件摘要。
后者包含代码字节、执行权限和内部链接目标，不执行插件代码；超限、不可读项、
特殊文件和指向树外的依赖链接拒绝。默认最多 10 万条目/512 MiB，用于有界构建检查。
快照不保存原始配置、凭证或本机安装路径。该摘要只提供本地一致性检查，并非 npm
依赖的离线交付物，也不提供与敌对同用户进程隔离的保证。尚未接入 Codex Build/Run；
既有 source receipt 继续校验源归档，不能代替本次新增的已安装字节检查。
10 项定向测试使用模拟 DSH 命令和真实文件树，覆盖字节/锁/配置/权限变化、树内
链接、越界链接、移动目录后稳定摘要及配额。没有据此宣称真实 DSH Build 已通过。

安装快照检查现已接到 Studio 资源生命周期服务：`prepare_resource_generation`
在冷启动前后验证实际安装快照，并返回可信 generation。相同快照可共享已验证
generation；同 Profile 中仅由 Studio 界面插件提前启动、尚未承载资源 Build 的 Core
会先受控停止，再在两次快照校验之间重启并锁定当前 Build。已有其他资源 Build 或
不兼容 Profile 的 generation 返回 `RESOURCE_PROFILE_IN_USE`，不隐式替换其他运行。
`activate_resources` 必须提供 expected 快照，并在启动 Worker 前重验。
安装变化会撤销对应 generation；Core 重启和关闭清除验证记录，旧授权不可继承。
新增测试覆盖准备流程、实际 Worker/Pipe/socket 生命周期、未准备时零启动、安装变化
时撤销，以及不替换既有 Core。Core/安装验证使用测试替身，Worker 是真实子进程。
Studio Build/Run 路由仍未调用这些入口；expected 必须最终取自保存的 Build，不能
从浏览器自报参数建立授权。此处尚不代表完整 Agent 操作流程接通。

Studio `NativePluginBinding`、`AgentBindings` 和 Codex manifest 现校验官方资源配置，
保留唯一声明在现有 config 内。官方协议 ID 固定为 `kingsoftcloud.dsh-knowledge`、
`kingsoftcloud.dsh-memory`、`kingsoftcloud.dsh-skill-center`，分别映射同名 scoped npm
包 `@kingsoftcloud/dsh-*`。这些 ID 的识别仅用于 schema 校验，不代表安装可信或授权。
错误 ecosystem、kind、schemaVersion、未知字段及多版本重复资源在声明边界拒绝；
其他插件 config 不套用官方业务规则。AgentSpec 的启用记忆 binding 引用必须指向
已启用 memory-instance，并显式选择 `[user]` 分区。未由激活适配器解析的 binding://
在旧 Memory resolver 中明确拒绝，不回退 SQLite。已测试真实 Draft 仓库保存/读取，
以及 Codex manifest 不能绕过资源数量约束。尚未提供选择器、平台连接查询、专用
MemoryProvider 或可运行的 Codex DSH Build；合法声明并不代表资源已可调用。

Memory SDK 的 ListSessions 查询现跨页查找目标会话，默认最多 20 页，每页 20 条，
每次请求保留同一 collection/user。重复页、异常响应、目录变化、缺页、页预算耗尽
有明确错误码。`MemoryExtractionStatus.error_code` 区分查询失败与完整查询未找到；
查询失败仍为 unknown，不表示已提交写入失败。结构化路径直接使用本次调用结果，
不读取共享 last_error；旧 raw 查询返回形状保留，并清除过期 last_* 状态。
9 项本地测试覆盖第 21 条、失败脱敏、缺失与失败区分、不完整目录、并发结果隔离。
仍是 session 粒度状态，尚未解决 operationId/写入批次的权威关联，也不会把 extracted
自动设为 searchable；MemoryProvider 的完整读写、审批和操作账本继续待实现。

新增 `AicpMemoryProvider` 的结构化读取路径，由显式 SDK backend 和可信身份构造，
校验实例/地域及非空签名凭证，以稳定 memory_partition 查询；请求不匹配分区时
零上游调用。复用 SDK strict 解析，未知 schema、缺 ID 和错误 envelope 不冒充空结果。
MemoryRecord 允许 version/confidence/importance 为 None，并新增 unknown 分类，避免
捏造上游未提供的数据。新 Provider 不宣传 CAS、TTL、hard delete 或未知搜索模式；
尚无按类型/时间/metadata/score 过滤的合同，这些策略明确拒绝，调用需显式取消过滤。
返回保留真实 ID、来源 session、score（可为空），仅投影受控 metadata，并执行预算。
读取已可被 MemoryCoordinator 消费；upsert/delete/get/core blocks 明确报不支持，
等待异步回执/审批与具体 API 适配。11 项测试覆盖真实 SDK parser、Coordinator 读取、
跨会话稳定分区、跨用户拒绝、未知字段、策略门禁、异常响应和零预算。
尚未接到 Memory Worker、插件工具、自动 hook 或 Studio Run，不代表 T3 已完成。

## 本地 Skill 产物边界补充

本地 pinned Skill 执行现在复用 artifact_delivery 的有界导出与校验导入，
不再直接信任 workflow_result 中的主机文件路径。拒绝执行目录外文件、路径穿越、
符号链接、超限文件、非字符串路径和重复结果；成功输出复制为独立快照，内部结果中的
output_files/artifacts 同步指向已校验副本。执行前统一工作目录的规范路径，兼容
macOS /var 临时目录别名。该机制不把本地进程升级为安全沙箱，也不是 UI 下载授权。

验证：设置 KSADK_TEST_DSH_NODE_MODULES 后，通过 uv run --no-sync pytest 执行
test_local_skill_artifact_boundary.py、test_pinned_skill_execution.py、
test_skill_artifact_delivery.py、test_bound_skills.py，共 65 项通过；相关 Ruff
和 git diff --check 通过。包含真实本地脚本和官方 Core 集成，云 SDK 传输仍使用替身，
未进行真实平台、模型或云端 E2E。现有附件服务分别有环境凭证回退或小型输入类型限制，
尚不能直接视为受资源 scope 约束的产物发布/读取路径；该宿主集成仍待完成。

## Skill 执行、操作结果与产物读取接入（2026-09-08）

PinnedSkillWorkerBinding 可接收私有执行凭证、冻结连接和宿主产物目录；初始化验证
连接与 Build 一致，凭证仅在 pipe_payload 中展开。Worker 使用冻结的 E2B 工厂，
并在 ready 前检查本地 SDK 与显式连接配置，不创建测试沙箱。缺 SDK 会初始化失败；
这项检查不代表真实模板或上游权限已经验证。

execute_skills 已接入 Worker 和 broker 的宿主审批、稳定事件去重路径。Worker 仅
向 broker 返回固定操作状态与内部产物路径，不转发 stdout/stderr。broker 在宿主
指定目录边界内复用 artifact_delivery 校验文件，再将产物字节、摘要、名称和操作
结果事务性保存在现有 SQLite 操作账本中；不新增 HTTP 上传服务。成功和确认退出
失败可保存终态及部分产物；超时、丢失响应或产物发布失败保留 unknown，不自动重跑。
重复事件返回原产物引用。当前本地账本的保留/清理及多 Pod 云持久化合同仍待接入，
不能将本地恢复等同于云扩容恢复。

新增 read_skill_artifact 固定操作，使用当前租约和操作所有权共同校验，按最多
32768 字节分块返回 base64；原始文件名、大小、摘要供消费者核对。读取复用 broker
并发与 deadline 限制，在返回前重验撤销，不调用 Worker，也不把本机路径返回模型。
官方 Skill Bundle 已注册执行和产物读取工具，并通过真实官方 Core 输出 schema。
Node/broker 的执行等待上限为 900 秒，仍受宿主调用 deadline 与当前租约约束；
长任务续租、断连协调和浏览器下载展示仍未完整接入。Studio Run 的初始化/审批事件
装配也未完成，故尚不能从 Studio 用户流程宣称 Skill 执行已可用。

验证证据：资源运行时加 Studio DSH lifecycle 回归 347 项通过（3 条既有依赖弃用
警告）；其中包含缺 SDK 不报 ready、冻结连接漂移、私有管道凭证、Worker 经沙箱
传输替身执行真实 pinned 脚本、官方 Core 执行/产物工具、跨主体读取拒绝、撤销拒绝、
宿主账本重开后分块恢复字节与不重复执行。真实 E2B、平台资源、模型和云端 E2E
尚未执行；上述结果不替代这些证据。

补充确认非零退出的终态与部分产物保留后，执行/交付、Worker 和 bound Skill
相关 35 项测试再次通过（启用官方 Core fixture）；相关 Ruff 和 diff 检查通过。

## Studio activation 审批归属接入（2026-09-08）

StudioDshCapabilityService.activate_resources 现在接受可信宿主提供的本次运行审批器，
并使用工作区私有 resource-operations 目录中的既有 OperationLedger。资源 supervisor
将审批器与初始化时完整的 scope 集合绑定，按 activation 分派，不从工具参数选择
审批器。未提供审批器的运行拒绝写入；停止、启动失败和 generation 清理均删除对应
归属，异步审批完成后再次确认归属仍有效。旧的显式 supervisor 默认审批器参数保留
兼容，但 Studio 不配置跨运行的默认审批器。

验证使用真实 Worker 进程和本地 HTTP 资源替身，覆盖两个用户分别审批、未配置
运行不能借用别人的审批器、停止时迟到审批不能触发写入、启动失败移除审批归属。
Studio 服务重建测试覆盖已批准写入只提交一次且恢复相同 operationId，未批准
写入两次启动均不发送上游。Core 生命周期在该 Studio 测试中使用既有宿主替身；
实际 Kernel 审批合同仍由独立 Kernel 集成测试覆盖。

此改动补齐 Studio 资源激活服务的装配能力，尚未实现 Codex Build/Run 调用该服务、
工具事件到 Kernel interaction 的正式映射及 Studio 审批 UI，因此不能宣称用户流程
已完成，也未进行真实平台或模型 E2E。

本轮 fresh verification：启用官方 Core 与 E2B 合同检查 fixture 后，resource_runtime
及 Studio DSH lifecycle 合计 354 项通过，3 条既有依赖弃用警告；相关 Ruff 与
git diff --check 通过。

## Studio 资源连接声明入口（2026-09-08）

StudioService 已装配 ResourceConnectionRepository，现有鉴权/CSRF 保护下新增
GET /api/v1/resource-connections 与 PUT /api/v1/resource-connections/{connectionRef}。
连接保存 label、规范化 ConnectionTarget 和与 signed/STS/token 模式严格匹配的
Secret 引用。只接受 env:// 显式引用，不接受凭证值；连接记录不含解析后的 Secret。
expectedRevision 防止覆盖并发修改，记录通过工作区原子写入保存，文件锁协调写入，
使用哈希文件名并重验工作区路径边界。列表与保存响应均标为 unverified；tenant/
principal 是配置声明，不是经过认证的身份，后续 Build/Run 必须通过平台权威验证。

宿主 resolve_credentials 接口比较预期 Build 连接目标，再复用 CredentialResolver
解析指定引用；新增 allow_aliases=False 禁止模型凭证的成对别名回退。原模型客户端
默认行为保持兼容。凭证轮换可读取新值，endpoint/主体等目标漂移要求重新构建；
此方法不授予资源权限，也不通过任何 API 返回凭证值。资源列表、上游权限检查、
前端连接表单以及 Build/Run 的实际消费仍待接入。

本轮验证：资源运行时、Studio 连接 API、插件 API 和 DSH lifecycle 共 369 项通过，
8 条既有依赖警告；最后补充路径边界与输入长度检查后，连接相关测试再次运行。
API 测试使用真实 Studio ASGI 应用，覆盖会话认证、CSRF、保存重开、版本冲突和
引用不匹配；没有访问真实平台、模型或云 API，也未做浏览器 UI 验收。

## 控制面显式连接与 UI 源码核对（2026-09-08）

当前 public checkout 的 Makefile 明确说明 Studio React 源码不随公开导出提供，
ksadk/studio/react-ui 不存在，只有已编译静态文件。相邻 ksadk-web 仓库也不存在；
历史 Git 可找到旧源码，但未确认其与当前静态产物对应。因此未修改压缩 JS，已请求
当前 Studio 源码目录/分支，浏览器 UI 工作仍待补齐；后端工作继续。

AgentEngineClient 新增 allow_env_fallback=False，复用既有签名与请求实现：强制
显式 endpoint/AK/SK/具体地域，不读取环境中的服务地址、签名服务、账号、API
版本、dry-run、TLS 禁用配置或隐式身份缓存/反查；requests 禁用环境代理/netrc
及自动重定向，控制请求遇到重定向明确拒绝，不自动切换内网 endpoint。显式
api_version 参数可固定协议版本。旧调用默认保留原环境兼容行为。

ResourceConnectionRepository.create_control_client 使用该模式和已有凭证引用
解析器，不把浏览器声明的 tenant/principal 作为身份头转发。此工厂只支持已实现的
signed 控制面适配，token/STS 明确拒绝；真实权限验证和 ListKnowledgeBases /
ListMemoryInstances / ListSkillWorkspaces 的服务端合同仍需核对并接入，不能据此
宣称资源选择器已可用。

fresh verification：资源运行时、Studio 连接及 DSH lifecycle 共 373 项通过，13 条
既有依赖警告；相关 Ruff 和 diff 检查通过。新增测试使用本地 HTTP 服务验证实际
签名请求、不读取无关环境账号/代理、拒绝重定向，以及连接工厂不伪造身份头。
没有真实控制面、模型或云端 E2E，也没有 UI 浏览器验收。

## 冻结记忆召回预算（2026-09-08）

FrozenResourceBinding 增加 memoryRecall，作为 Agent memory.recall 的 Build 投影，
不是另一套草稿配置。条数、预算、分数阈值进入资源快照摘要，离线 Build 恢复保留
这些值；仅记忆实例允许该字段。Worker 使用快照预算，不允许模型传入 maxTokens
等参数覆盖。零预算不请求上游；非零预算使用已有 Provider 的保守估算和截断规则。
未包含该字段的既有开发快照保持原来的默认行为和资源摘要。

目前 AICP Provider 无法保证 minScore 量纲，非零阈值在该快照校验时明确拒绝。
测试覆盖现有 MemoryRecallSpec 字段的投影，但实际 Studio Build materializer 尚未
接入该投影；自动召回启停、写策略、唯一 writer 和三引擎 hook 也尚未完成。

本轮相关模块 36 项通过；随后 resource_runtime、Studio 连接及 DSH lifecycle
共 379 项通过，13 条既有依赖警告，相关 Ruff 与 diff 检查通过。真实 Worker
进程通过本地 HTTP 替身验证条数、零预算和截断；没有真实平台或模型 E2E。

## 资源配置校验接入 Build 与 Agent API（2026-09-08）

新增共享 resource_binding_diagnostics，检查启用的官方资源插件所引用的连接、
精确 Secret 引用和已启用 MemorySpec 的 providerRef/召回预算能力。其他插件与
禁用插件不进入该资源校验；不进行网络请求，也不把 Secret 值写入诊断。

StudioService 将资源连接仓库注入 CodexStudioBuilder，实际 build() 在解析插件和
检查 runtime 前执行上述检查；错误以 RESOURCE_BINDING_VALIDATION_FAILED 返回
逐字段诊断。连接配置通过后仍保留现有 DSH ecosystem 交付门禁，未将预检冒充
完整 Build 成功，也尚未写入最终 Build 的资源投影。

POST /api/v1/agents/{agentId}/resource-bindings/validate 已挂到现有 Studio router，
读取保存的 AgentDraft 并检查 expectedRevision，不接受浏览器伪造的运行身份。
返回静态诊断和 authorizationVerified=false；RESOURCE_AUTHORITY_UNVERIFIED 明确
提示尚未完成平台身份/资源权限验证。该接口支持已有草稿诊断，尚未实现资源选项
列表、未保存表单候选校验或前端展示。

初步验证：实际 Codex Build 入口、真实 Studio ASGI、配置重开等相关 13 项通过；
相关 Ruff 和 diff 检查通过。真实平台与模型 E2E 仍未运行。

随后启用官方 Core 和 E2B 合同 fixture，resource_runtime 及上述 Studio 连接/
绑定校验、DSH lifecycle、插件 API 回归共 390 项通过，13 条既有依赖警告。

## 后续必须固定的合同

1. Build 的连接主体/目标快照与凭证轮换校验，不能把连接名当授权证明。
2. 不同插件锁的 Core 共存策略；同锁共享，不兼容锁不得替换既有运行字节。
3. Kernel 审批到 MCP 等待/恢复，再到 Worker 派发的完整时序。
4. 跨 activation 稳定 operationId 和原子派发账本；未知写结果不可自动重放。
5. pinned Skill 的执行包清单、完整字节恢复、解压文件验证和依赖适配。
6. memory enabled/write/recall 的统一权限真值表和 required 失败规则。

## 验证要求

新增测试使用假凭证、临时目录和 mock transport。运行命令：

```bash
uv run --no-sync pytest -q tests/resource_runtime \
  tests/studio/test_native_plugin_binding.py tests/studio/test_dsh_capability_service.py \
  tests/studio/test_dsh_agent_binding.py tests/plugins/test_dsh_capability_host.py
```

2026-09-07 最近完整插件回归：211 passed，8 条既有弃用/实验功能警告，55.34 秒。
范围为上述资源测试和四组插件/Studio 回归，包含本次 Skill 分页与消费端检查。
本次设置了官方 DSH 测试依赖路径，没有 skipped。定向分页测试另行运行 19 passed，
这些用例已包含在 211 项中，不重复累计。

随后本地 pinned 执行改动运行全部 `tests/resource_runtime`：152 passed，3 条
签名依赖弃用警告，8.79 秒，无 skipped。包含新增的 10 项 pinned 测试；这是资源
模块回归，不替代上述四组 Studio/插件测试或真实平台 E2E。相关 Ruff 和 diff 检查通过。

E2B pinned 传输与协议检查随后运行全部资源测试：164 passed，3 条相同签名依赖
警告，8.98 秒，无 skipped。新增协议与远程传输用例已计入其中；相关 Ruff 和 diff
检查通过。真实 E2B 尚未运行，不能把传输替身结果计入云端或真实资源 E2E。

产物取回完成本地验证后，最新全部资源测试为 180 passed，3 条相同签名依赖警告，
8.83 秒，无 skipped。15 项产物交付定向测试包含在内；修改文件 Ruff 与 diff 检查通过。

资源 Build 制品保存/恢复辅助模块加入后：全部资源测试 188 passed，3 条相同签名
依赖警告，9.24 秒，无 skipped；其中 8 项新测试验证离线包恢复与实际执行。
相关 Ruff、diff 检查通过。Studio Build/Run 接入及真实平台 E2E 仍未完成。

DSH 安装快照加入后运行全部资源测试和 `tests/plugins/test_dsh_source_policy.py`：
207 passed（资源测试 198、来源回归 9），8 条既有警告，10.32 秒，无 skipped。
新增文件及 bridge 的 Ruff、diff 检查通过。

资源激活接入安装检查后：全部资源测试和 Studio DSH 生命周期测试 215 passed，
3 条既有签名依赖警告，10.38 秒，无 skipped。相关 Ruff、diff 检查通过。

资源配置接入 Studio 声明后：全部资源测试、native binding 和 DSH 生命周期测试
233 passed，3 条相同签名依赖警告，10.28 秒，无 skipped。相关 Ruff、diff 检查通过。

Memory 状态分页后运行全部资源测试：221 passed，3 条相同签名依赖警告，9.46 秒，
无 skipped。包含新增 9 项记忆状态测试；相关 Ruff、diff 检查通过。此次数量不包含
上一次命令中的 Studio 测试，不能直接与 233 项比较为增减。

Skill 新增证据包含四个真实 Python 子进程并发写入同一缓存、篡改解压文件后的
拒绝与重建、命名空间分离、路径/链接攻击、下载提前终止并关闭流以及签名 URL 脱敏。
测试包括实际构造签名头、token 客户端不继承签名器凭证，以及缺签名凭证时
在网络调用前拒绝。新增和修改 Python 文件的 Ruff 检查通过，diff 空白检查通过。

新增真实进程/传输集成：Unix socket → broker → 私有管道 → 独立 Worker →
kingsoftcloud SDK → 本地 HTTP 测试服务 → 结构化检索结果。验证显式签名账号、
资源 ID、查询、返回来源、撤销后零新增调用，以及关闭后的进程/socket 回收。
本地 HTTP 服务提供 fixture 数据，这不是真实平台或真实模型 E2E。

资源 MCP 集成已通过：Python 签发 ks2 → 真实 MCP HTTP → 官方 Cordis/Tools →
资源桥 → broker/Worker → SDK/本地测试服务。覆盖 ks1 工具列表过滤、ks1/root
调用拒绝、ks2 成功检索和撤销后 401；上游调用计数确认拒绝路径不访问知识库。

新增 Node 集成：真实 Node 进程共享一个 ResourceBridge，并发调用两个 activation
的独立 Python Worker；按上游捕获的 query → dataset ID 验证调用没有串资源。
Node 协议套件另包含八个子用例，覆盖分片、超限、错误关联、截断、非法 UTF-8、
错误脱敏、非法参数和取消；该套件被上述 pytest 总数中的一个用例调用，不能重复
计入 Python 测试数量。

进一步验证已通过：官方 Cordis 4.0.1 + DSH ToolRuntime 0.1.1-rc.2，加载公共桥
和知识库插件，在单个 Cordis 实例中执行 tools.execute，异步上下文穿过真实工具
管线后仍各自访问正确的 Worker/知识库。无上下文调用返回错误且不访问上游。
这不是完整官方 Core/profile、MCP scope v2 或模型回合的完成证明。
测试依赖及复现步骤在 `tests/fixtures/resource_runtime/dsh-runtime/`；未配置依赖时
该用例明确 skipped，不能用于完成声明。

这只能证明对应合同与客户端隔离行为。完整验收仍包含原方案 A01–A26，
以及评审新增的 A27–A38：不同 Build 并发、崩溃窗口、审批参数冲突、同实例并发、
环境污染、pinned 隔离执行、解压缓存修改、分页状态、策略零值、长租约、
外部 Skill 发现边界、真实入口双用户身份。

真实 E2E 还需模型连接、专用知识库、记忆实例、Skill 空间及沙箱测试配置。

补充：新增宿主侧 `OperationLedger` 持久化去重基础。稳定事件 ID 与已准入的
身份/绑定摘要决定操作身份，activation 不参与键；参数摘要变化拒绝复用。
发送前事务占用，重启后 unknown 不自动重发；支持 pending 与终态的单向转换，
不存储请求正文。6 项测试覆盖恢复、并发宿主争用、参数冲突、身份隔离、
状态恢复与私有目录要求，全部通过。账本保留策略和 checkpoint 事件来源仍需集成。

后续已接入 broker 写分发：必须同时配置宿主 ResourceWriteAuthorizer 与账本，
默认仍拒绝写入；宿主查询真实审批并返回稳定事件 ID，IPC 不接受自报 approved。
Worker 调用前提交去重记录，等待审批/磁盘后重验租约；重复请求返回操作状态，
不重发。超时返回 RESOURCE_WRITE_UNKNOWN，Worker 返回值不能直接推进账本终态。
请求通过 JSON 深复制，避免审批等待时调用者修改原参数。6 项写分发测试通过，
覆盖不同 activation/generation 恢复、并发去重、参数拒绝、超时、撤销与参数修改。
资源运行时全量 244 项通过，3 个已知第三方弃用警告。真实宿主审批查询适配、
记忆 Worker 写操作和上游状态核查仍未接通，本轮没有真实平台 E2E 证据。
模型 API key 不能替代平台资源凭证。云端需要配套 Server、镜像、制品和授权能力。

记忆读取已接入真实 ResourceWorkerProcess：MemoryWorkerBinding 使用显式签名凭证、
冻结实例/地域/endpoint 与宿主身份创建专用 Provider；初始化只准入 load_memory，
禁止将知识库操作或尚未接通的写操作投影到该服务。broker 在进入 Worker 前校验
query-only 参数，用户分区不从工具参数取得。当前工具读取固定最多 8 条、1600
估算 token，无 minScore/分类过滤；MemorySpec 的可配置预算及能力门禁尚需接入，
不能把这个默认工具读取路径视为自动记忆完整实现。
新增真实子进程到 SDK 到本地 HTTP fixture 的双用户测试，验证真实 ID、未知版本、
稳定分区隔离及环境凭证不混用；另验证跨服务操作在初始化拒绝。两项均通过。
这不是实际平台或真实模型 E2E，实际 Studio 运行入口仍需装配。

新增私有开发 Bundle `@kingsoftcloud/dsh-memory`，当前注册 load_memory，复用同一
platformResources 与 tools 服务。输出保留真实 memoryId、正文和 nullable score/version，
不向模型投影内部租户与分区字段。官方锁定 Cordis/DSH ToolRuntime 实测发现不支持
JSON Schema type 数组，已改用 oneOf。真实 Core 工具执行到 UDS、Worker、SDK 与本地
HTTP fixture 通过，覆盖正常记录、空结果、畸形上游结果及缺少调用上下文拒绝。
修改的通用 Core 探针同时回归原知识库链路，相关 16 项测试通过；补充空/失败场景后
记忆模块 3 项重跑通过。插件未发布或预安装；save_memory、自动 hook、管理 UI 和
实际 Studio Build/Run 装配仍未完成，不能据此宣称完整记忆插件可用。
凭证值不进入本文、源码、测试 fixture 或交付产物；真实调用结果单独脱敏记录。

记忆写入进展：Worker 现在可显式准入 save_memory，broker 验证 content-only 参数，
宿主审批查询返回稳定事件 ID 后才登记账本并发往 Worker。Worker 私有请求 ID
替换为持久操作 ID，派生独立抽取 session，避免同一会话多次提交共用状态。
Provider 复用 SDK save_memory(strict=True, flush=True)，确认响应需包含 request ID
且无明确错误；空响应、畸形响应、错误码和 Success=false 都不能报告 accepted。
确认受理只返回 accepted_pending/searchable=false，broker 将固定提交回执写入 pending，
不推进 searchable。真实子进程到本地 HTTP fixture 验证重复调用只有一次上游提交。
资源回归 257 项通过；随后补充 Success=false 拒绝用例，提交模块 10 项重跑通过。
实际 Studio 审批查询实现、supervisor 的写授权装配、save_memory 插件工具以及
抽取/索引核查仍待完成。真实平台确认响应格式也仍需联调，不以本地 fixture 替代。

后续完成 supervisor 的写授权依赖注入：宿主须同时提供审批查询与持久账本，
未配置时保持拒绝。记忆 Bundle 已注册 save_memory，复用资源桥并将受理结果显示为
pending，unknown 明确禁止自动重发。官方 Cordis/DSH ToolRuntime → UDS → supervisor
管理的真实 Worker → SDK → 本地 HTTP fixture 的工具测试通过：获批及重复调用只产生
一次提交，返回同一操作 ID/pending，未获批调用不触达上游，缺上下文调用拒绝。
记忆 Worker/工具相关 5 项通过。实际 Studio 审批查询实现与抽取/索引核查仍未完成，
本地工具测试没有替代真实平台、模型和 UI 验收；插件仍为私有开发包，未发布。

本地审批适配：SQLiteAgentKernelStore 新增完整 tenant/agent/session/run 范围的
get_terminal_receipt，只返回已提交终态，保留 approved/rejected 差异。新增
KernelResourceWriteAuthorizer 读取现有 InteractionRecord 和终态回执，校验相同 revision、
批准 outcome，以及身份/Build/绑定/操作/参数摘要；稳定事件 ID 放宿主内部 metadata，
不存请求正文。真实 LocalSessionService 与 Kernel 共库 request/resolve 后关闭重开，
验证批准与拒绝、跨作用域查询、参数变化和绑定快照变化，两项参数化测试通过。
该适配尚需 Studio 发起 interaction 并维护可信工具事件映射；远端/PostgreSQL
审批读取路径尚未接入，不能将本地 SQLite 适配视为完整云授权或 UI 流程完成。

补齐本地审批创建：KernelResourceWriteAuthorizer.request_memory_approval 使用现有
Kernel activation fencing 写入 InteractionRecord，不另建审批库，也不能自行批准。
审批展示包含待保存正文供用户核对，内部 continuation metadata 只记录摘要/稳定事件。
重复创建同一请求复用记录；批准、拒绝和请求恢复继续沿 Kernel 原合同。
记忆提交测试已替换模拟审批回调，使用真实 LocalSessionService、SQLite Kernel 的
request/resolve、资源 broker、持久操作账本、真实 Worker 与 SDK 到本地 HTTP fixture。
批准前零上游写入，批准后一次提交，重试返回同一 pending；相关 7 项测试通过。
Studio UI 与可信工具事件到 interaction 的映射尚未装配，这些测试不代表真实平台 E2E。

新增 memory_status：操作账本使用独立归属索引关联主体/绑定摘要与操作类型，
broker 必须核对回执归属后才查询 Worker。旧账本无归属行默认不可查；只有相同
已审批事件重新 claim、校验原参数摘要后才能补归属，且不重发写入。
Worker 使用持久操作 ID 派生抽取 session，Provider 查询 SDK ListSessions；结果将
账本 status 与 extractionStatus 分开，extracted 仍返回 searchable=false，不假装索引
已经就绪。插件注册同 Core memory_status 工具，模型仅提供经过归属检查的 operationId。
资源全量 262 项通过；补充真实 Core 提交后调用状态工具的测试后，记忆模块 5 项重跑通过。
索引可见性确认、抽取失败后的账本协调与 UI 自动轮询仍未完成；上游为本地 HTTP fixture。

抽取失败协调已补齐：固定 Worker 回执确认本操作 extractionStatus=failed 且无查询错误时，
broker 将 unknown/pending 推进持久 failed；重试只返回该终态，不重复提交。状态查询
拒绝 ResponseMetadata.Error、布尔 Code，以及返回条目中不匹配的用户/记忆实例。
相关记忆 Worker、真实 Kernel 审批及 SDK 状态测试 19 项通过，代码检查通过。
本轮核对发现 ListMemories 客户端只支持用户/Query/分页，没有本次抽取 session 过滤；
旧正文匹配无法证明记录来自本次提交，因此不复用它宣称 searchable。索引确认仍需
真实上游返回记录关联或明确服务保证，当前返回抽取完成但 searchable=false。

Skill 消费进展：新增 PinnedSkillService，复用 ResourceBuildReference/restore_resource_build，
每次访问验证冻结 manifest、完整 ZIP 和提取缓存。提供 list_skills、load_skill、
read_skill_resource，只接受已锁定 Skill ID 与包内规范相对路径，文本以字符 offset
有界分页；返回包身份/摘要/相对路径，不泄露宿主绝对路径，不静默下载最新版本。
PinnedSkillWorkerBinding 已接入现有 Worker 初始化与分发，要求 snapshot/主体匹配，
路径来自宿主私有初始化；无需传平台凭证，线上资源准入仍由宿主负责。
11 项测试通过，包括删除原下载目录后的离线读取、真实 broker/Worker、正文连续读取、
路径穿越、未绑定 Skill、篡改 Build ZIP 与提取缓存。Skill DSH 业务 Bundle、discovery、
执行适配及实际 Studio Build/Run 装配仍未完成，不能把只读 pinned 路径视为整个 T4 完成。

新增私有开发 Bundle @kingsoftcloud/dsh-skill-center，注册 list_skills、load_skill、
read_skill_resource，复用同 Core tools/platformResources，不新增宿主。真实官方
Cordis/DSH ToolRuntime 到 UDS、broker、Worker 和冻结 Build 包的离线读取通过。
输出保留精确版本/hash，分段读取明确 nextOffset；没有把宿主路径当作文件交付。
实际注册暴露 DSH 输出 schema 不支持长度关键字，已调整输出，输入校验保留。
Worker 将受限 Skill 文件访问的预期错误转成脱敏业务失败，不因越界路径退出整个
进程；拒绝后合法读取仍成功。Skill、知识库及记忆 Worker/Core 相关回归 31 项通过。
三个业务 Bundle 现都有源码与部分工具，但均不是完整发布：Skill discovery/search/execute、
自动能力、配置 UI、真实安装交付及 Studio/云入口仍需继续完成。

Pinned 搜索已接入 search_skills：复用现有 match_skill_refs，只读取冻结包的名称与
SKILL.md description，返回精确 skillId/versionId/hash 与匹配原因、启发式整数分值。
不从环境变量扩大空间，也不把模型提供的 spaceId 当作可选范围；空查询和未知字段拒绝。
真实 Core → 资源桥 → Worker 的搜索调用、说明匹配、无命中及参数边界通过；
Skill 读取/搜索与 Build 制品相关 21 项测试通过。discovery 的在线授权目录仍未接入，
此实现没有把 pinned 搜索冒充动态发现或脚本执行。

新增宿主侧 pinned 执行适配：执行请求必须显式选择已锁定 Skill ID、配置为 isolated，
重复/未绑定 ID、空间覆盖字段和错误预算在调用 Runtime 前拒绝。现有 Runtime 接收
精确 pinned_packages 与稳定操作 ID，运行时不从可变目录重新选择包。真实本地子进程
删除原下载目录后执行包脚本，读取 reference 并生成预期产物；这不是 E2B 隔离证明。
Kernel 审批创建扩展到 execute_skills，展示选定 ID 与任务，沿用同一摘要和批准回执
验证；批准、拒绝、参数/绑定变化与数据库重开覆盖记忆和 Skill 两种操作。
执行适配返回内部 SkillRuntimeResult，宿主仍需注册产物，不能直接将本机文件路径
发送给远程模型。Worker 的执行后端装配、execute_skills 工具、超长执行/取消及真实
E2B 环境仍未完成；未把本地测试后端声明成生产隔离后端。

显式 E2B 连接：在临时目录安装 e2b==2.15.3 检查真实 SDK 配置实现，未修改项目依赖
或版本。该版本 debug=False/sandbox_url=None 仍会读取环境 fallback；新增
ExplicitE2BConnection 要求明确 HTTPS API origin、domain 与 SecretStr API key，并在
E2B_DEBUG/SANDBOX_URL/ACCESS_TOKEN、代理或证书环境覆盖存在时拒绝新资源路径。
旧未传 connection 的兼容路径保留；资源 Worker 使用已有最小环境。Sandbox 与 Skill
Runtime 可显式传递连接，凭证不放沙箱 env、repr 或默认模型序列化中。已创建沙箱
若 readiness/输入上传失败则尝试 kill，清理失败日志不包含凭证。
7 项连接测试通过，包含真实 e2b 2.15.3 ConnectionConfig 参数验证与环境污染；这仅
验证客户端配置，尚未调用真实 E2B 云 API。检查安装位于临时目录，后续可通过
KSADK_TEST_E2B_SITE_PACKAGES 指定重现；未配置时对应实 SDK 测试明确 skip。

沙箱目标冻结：FrozenResourceBinding 可携带 skillExecution，记录 E2B 连接主体/地址、
domain、templateId、timeout 和网络策略，限定同租户 isolated Skill，纳入资源快照摘要。
未配置目标的旧只读快照保持原摘要算法；运行时工厂要求明确目标，拒绝连接主体或
地址变化，只在相同连接下重新解析 API key。SDK 连接不包含在 Build 中。
执行适配将请求超时缩小到冻结目标上限；资源制品离线恢复保留完整执行目标。
资源回归 303 项通过；随后新增预算测试，目标合同模块 11 项重跑通过。
Studio 目标选择/连接解析、Worker 实际执行装配和云端准入尚未接通，不把工厂构造
成功当作真实沙箱或完整执行路径已通过。

### Discovery Skill 读取路径（2026-09-08）

新增 DiscoverySkillService 与显式签名 Worker binding。动态目录仅来自绑定空间，
允许消费客户端标明不完整的目录并继续返回 truncated。首次加载校验包摘要并固定
当前 activation 的版本，后续读取复验缓存；远端升级不会替换已选包，缓存篡改
不会触发静默重新下载。模型不能覆盖空间，未列出的 Skill 不下载。

本次新增 9 项服务层测试，覆盖 150 条目录中的后续条目、版本保持、空间/参数/
摘要/路径拒绝和缓存篡改。使用项目 uv 环境，resource_runtime 全集及 Studio
resource_binding_validation、resource_connections、dsh_capability_service、
dsh_plugin_api 回归共 399 passed，13 warnings；相关 Python Ruff 检查通过。
测试使用本地 fixture 和现有 DSH/E2B 合同测试依赖，不代表真实平台或云端 E2E。

仍未完成：discovery 选择记录的持久化与 checkpoint 恢复、discovery 执行、公共
空间策略、原生 Skill 热发现及实际 Studio Run 接入。Worker 当前仅允许 discovery
目录与文本读取操作，不允许 execute_skills；不得据此宣称 T4 完成。

### Discovery Worker HTTP 故障与进程验证（2026-09-08）

新增真实 Python Worker 子进程与本地 HTTP Skill 服务 fixture 联调。先复现目录
503 导致整个 Worker 退出的问题，再将 HTTPX/requests 上游读取异常转换为固定
SKILL_RESOURCE_UNAVAILABLE 业务结果，避免泄漏上游文本并允许后续读取。目录
401/403 继续关闭 Worker，不把失效授权视为可重试读取；当前撤销粒度是整个
activation，尚非单 binding 的 optional 降级。

新增四项测试：503 后恢复目录和下载完整 ZIP/读取 SKILL.md、拒绝 discovery
执行 scope、401 和 403 停止 Worker。请求空间始终核对为冻结绑定，输出不含
消费者本机路径。此次相同 resource_runtime 与四组 Studio 回归共 403 passed、
13 warnings（30.37 秒），相关 Ruff 和 git diff --check 通过。此证据验证真实
进程/HTTP/包消费，服务为本地 fixture；没有使用真实平台凭证或执行模型 E2E。

### Studio 运行绑定观测接口（2026-09-08）

GET /api/v1/agents/{agentId}/resource-bindings/status?activationId=... 接入现有
Studio 鉴权边界。必须明确 activation；先确认 Agent 属于当前工作区，再按 supervisor
中的 Agent 身份匹配具体运行实例。返回冻结 buildDigest、bindingSnapshotDigest、
generationId、observedAt 和各 binding 的 runtimeState/allowedOperations；不回传
句柄、socket、PID、凭证或用户身份，不从当前 Draft 生成运行状态。

观测会检查 Worker 存活和当前 lease 是否有效。不存在、已撤销或属于其他 Agent
的 activation 一律 404；无 supervisor 时不会启动 Core。runtimeState=available
只表示资源 Worker/lease 当前可用，upstreamVerification 固定为 not-observed，
不宣称资源权限已重验。此接口目前没有历史状态存储、云实例聚合或完整的
configured/ready/degraded 状态机；实际 Build/Run 自动接入仍待完成。

新增 ASGI 与真实 Worker 测试，覆盖会话鉴权、跨 Agent 拒绝、冻结快照字段、
敏感字段不泄漏、lease 过期、撤销后 404 和只读观测不启动 Core。

验证：resource_runtime 全集和 Studio resource_binding_status、resource_binding_validation、
resource_connections、dsh_capability_service、dsh_plugin_api 共 405 passed、13 warnings
（31.12 秒）；本次变更 Python 文件 Ruff 检查和 git diff --check 通过。

### Studio 资源构建 materializer（2026-09-08）

新增 studio/resource_build_materializer.py，供可信 Build adapter 在完成身份、权限、
插件 lock 与执行器准入后调用。输入既有 NativePluginBinding/MemorySpec、已准入的
ResourceSnapshot 和现有连接仓储；严格核对声明集合及记忆召回策略与冻结快照一致，
不会将连接声明视为权威授权。isolated 必须有执行目标，公共空间和 STS Skill 下载
仍明确拒绝。

Pinned Skill 使用显式 SkillServiceClient 查询绑定空间、以目录确认成员身份，再按
声明的精确 versionId/contentHash 下载旧版本或当前版本，复用 PackageStore 校验和
write_resource_build 保存完整 ZIP。下载前后复核连接目标，临时解压目录始终回收；
失败不生成目标资源产物。没有新增上传、注册或独立保存服务。

5 项新增测试使用真实 SDK client 与 HTTPX MockTransport，验证目录升级后仍下载
v1、删除源和连接后离线恢复引用文件、错误摘要/缺失 Skill/连接主体变化/声明漂移
拒绝且不发布。新增测试与资源制品、Studio 绑定验证回归共 17 passed、5 warnings
（2.92 秒）；相关 Ruff 和 git diff --check 通过。

尚未连接实际 CodexStudioBuilder 成功路径：权威准入解析、DSH 插件字节交付、
构建记录与 Run activation 装配仍需继续实现，现有 DSH ecosystem 门禁保留。
此 materializer 不证明依赖镜像已交付、自动记忆已接入或端到端已通过。

### 记忆结构化更新与软删除（2026-09-08）

AicpMemoryProvider 新增 post-approval mutate 路径，支持 update_memory/delete_memory。
仅接受当前 activation 在本用户分区成功检索到的真实 ID（有界保留 256 个）；未知
ID 不发上游请求，每次 SDK 请求仍包含冻结 collection 和稳定 AgentUserId。更新
后只跟踪服务端明确返回的新 ID，旧 ID 移除；不确定结果也移除旧观察记录，后续
需要重新检索。重启后不继承观察记录，不提供 CAS 或硬删除。

SdkLTMBackend 的 strict mutation 模式要求有效成功 envelope 和 RequestId；缺失、
错误或畸形响应不再推断成功。strict 更新不会按相同正文猜测新 ID，服务未返回时
保留空 newMemoryId。未启用 strict 的旧高代码行为保留并有兼容测试。

Worker 操作白名单、broker 参数校验与结果去重、Kernel 更新/软删除审批展示已
接入。写入需宿主稳定事件和真实审批；操作状态进入既有持久化账本。重复事件
返回既有状态，不重新提交；账本当前不保存更新后的 ID，恢复消费方需要重新
检索，不能沿用旧 ID。普通模型插件仍未自动新增这些管理工具，管理 UI 待接入。

验证：真实 Worker + 本地 HTTP fixture 完成检索、审批门禁、更新、软删除、重复
请求不重放、分区字段和账本重开检查。Provider/SDK 覆盖真实新 ID、歧义响应、
用户隔离、CAS/硬删除参数拒绝和旧 SDK 行为。Kernel SQLite 审批覆盖更新/删除
批准及拒绝、重开后的 receipt 与参数/身份漂移拒绝。广泛回归执行 425 passed、
13 warnings（31.85 秒）；之后补充旧 SDK 兼容和审批用例，相关最终小范围回归
24 passed（1.18 秒）。相关 Ruff 与 git diff --check 通过。上游仍为 fixture，
不代表真实平台跨用户权限、管理 UI 或完整端到端通过。

### 记忆变更结果的持久化恢复（2026-09-08）

既有 OperationLedger 增加 memory_mutation_results 表，在同一 SQLite 事务中提交
已确认的更新/删除结果和终态。白名单 receipt 仅保存 operationId、真实旧/新 ID、
固定状态和错误码，不保存记忆正文。结果与 authority/operation 关联查询，终态
结果不得被替换，未知写入仍不自动重放。重复事件经过审批和当前 scope 校验后
返回原结果；新 activation/Worker 可恢复原 ID 元数据，不再仅返回状态。

Broker 读取持久化记忆或 Skill 结果后会再次检查租约、activation 和 deadline，
阻止等待磁盘期间撤销后仍披露结果。历史 receipt 证明原操作结果，不证明记录
当前仍存在；新 mutation 仍需当前 Worker 的分区检索观察与新审批。

新增 7 项 receipt 测试覆盖结果重开、跨 authority/操作不可读写、事务故障全回滚、
终态不可替换、非法 ID/错误/正文拒绝。真实 Worker 测试扩展到新 activation 恢复
更新和删除结果、无新增上游调用，以及结果读取期间撤销不泄漏。该局部验证
8 passed（1.10 秒），相关 Ruff 与 git diff --check 通过。完整 Studio Run 恢复和
真实平台 E2E 仍待完成。

本轮最终广泛回归：resource_runtime 与 Studio resource_build_materializer、
resource_binding_status、resource_binding_validation、resource_connections、
dsh_capability_service、dsh_plugin_api 共 437 passed、13 warnings（32.19 秒）。

### DSH 记忆变更工具投影（2026-09-08）

私有开发 dsh-memory Bundle 新增 update_memory/delete_memory 工具，沿现有
platformResources -> broker -> Worker 路径执行，仍由调用 scope/宿主审批决定
能否使用。新工具不支持 userId、CAS 或硬删除参数；展示真实新 ID、未确认状态，
软删除不宣称物理擦除。未知操作重复读取时统一错误码投影，避免首次 unknown
和账本恢复的同一 unknown 显示不同结果。不新增平台业务协议或 Core。

真实官方 Cordis/DSH ToolRuntime、插件、UDS broker、Python Worker 和本地 HTTP
fixture 联调覆盖检索后更新/删除、重复请求仅一次上游写入、只读 scope 拒绝、
未审批拒绝、缺少调用上下文拒绝、未确认响应保持 unknown。测试 fixture 增加
显式顺序调用选项以验证前后依赖，原并发探针行为保留。这里的批准器是测试
替身，真实 Kernel 审批另有测试；没有真实模型或平台服务回合。

最终回归：resource_runtime 与六组相关 Studio 测试共 439 passed、13 warnings
（34.20 秒）。变更 JS 的 node --check、Python Ruff 和 git diff --check 通过。
实际 Studio Build/Run、管理界面与完整端到端验收仍未完成；资源业务 failed
在 MCP overlay 的 isError 投影还需与结构化结果共同核对。

### MCP 资源业务失败投影（2026-09-08）

先由真实 MCP HTTP 调用复现知识库业务失败仍 isError=false，再修正同 Core
MCP overlay：仅对已认证 resource scope 的 failed/unauthorized 结果设置
isError=true，保留 structuredContent 和受限错误码。空结果、accepted_pending、
unknown 不改为成功写入或可检索；普通第三方工具的 status 字段不套用资源语义。
更新 capability host 的 Bundle integrity，未改版本或发布。

HTTP MCP -> 官方 Cordis/ToolRuntime -> 资源插件 -> broker -> 真实 Python Worker
-> 本地 HTTP fixture 验证：KB 503 与空结果不同；记忆检索畸形响应为业务错误；
pending/unknown 的首次和重复查询一致且不重写；普通非资源 fixture 工具返回
status=failed 时保留原投影。这里仍未包含真实模型/平台资源调用，不是完整 E2E。

resource_runtime 与六组相关 Studio 回归 440 passed、13 warnings（35.05 秒）。
相关 node --check、Ruff、git diff --check 通过。
宿主追加回归 tests/plugins/test_dsh_capability_host.py：16 passed（38.47 秒）。

### 宿主资源租约续期入口（2026-09-08）

ResourceLeaseRegistry.renew_many 原子换发同 activation 的多个句柄，先验证全部
旧句柄和期限，再替换；任一失效不部分签发。ResourceSupervisor.renew 要求可信
宿主传入当前 ActiveResources 和异步授权重验函数，15 秒验证预算，只有字面
True 才可续期。验证期间不持有生命周期锁，停止可立即撤销；拒绝、异常或取消
会撤销原 activation。并发续期用当前资源对象校验旧请求，不允许旧结果替换或
撤销另一续期已换发的句柄。新句柄 scope 完全沿用旧 scope，不重启 Worker。

StudioDshCapabilityService.renew_resources 检查已准备 Build 和当前安装快照，
文件检查和上游授权检查均不持有 Studio 生命周期锁。失败仅清理对应的旧
supervisor，不干扰后来启动的 generation。状态查询读取换发后的 leases。

新增测试覆盖同 Worker 换发、原权限保持、失败/异常/非布尔真值拒绝、验证期间
停止、并发续期、批量部分过期、验证期间过期和取消。Studio 服务测试确认续期
重新检查快照。尚未实现 Codex/LangGraph/DSH 执行器自动调度重验、连接安全切换、
平台 grant 重验实现或凭证轮换后的 client 重建；这些由真实 Run 接入继续完成。

最终验证：resource_runtime 与六组相关 Studio 测试 449 passed、13 warnings
（37.03 秒）；相关 Ruff 与 git diff --check 通过。授权重验使用测试替身，
未据此宣称真实平台授权续期或完整端到端完成。

### Pinned Skill 消费者工作目录准备（2026-09-08）

新增 prepare_pinned_skill_workspace 上下文管理器，从冻结 Resource Build 恢复
已校验包，在新建且私有的消费者工作目录内准备 .agents/skills/<name>。路径
相对于消费者 cwd，保留脚本、引用和二进制文件；不把 Worker 缓存绝对路径当作
交付结果。调用方必须先完成 activation/binding 准入，并拥有工作目录生命周期。

只接受 outer-agent pinned 配置，不对 discovery/isolated 冒充原生文件交付。
已有目录/链接拒绝合并或覆盖；名称必须能形成唯一安全目录。包装 ZIP 可移除
纯目录前缀，但 Skill 根目录外有文件时拒绝，避免静默丢失资源。结束和异常时
只清理本次创建的根目录；根目录被替换后拒绝清理，保留用户的新目录。

8 项测试覆盖独立 Python 消费进程在 cwd 中真实读取引用文件、脚本保留、原始
源删除后离线恢复、二进制包装包、外部文件拒绝、已有目录/链接、异常清理、
根目录替换和 isolated 拒绝。结合资源 Build 与 Studio materializer 回归共
21 passed（0.66 秒）；相关 Ruff 与 git diff --check 通过。

尚未接入实际 Codex Run 工作目录组合，也未验证具体 Codex 版本的原生发现、
Skill 热加载、全局目录隔离或脚本依赖。此准备器提供消费者真实可读文件，
不宣称原生 Skill 已生效、具备 OS 隔离或完整 E2E 已通过。

### LangGraph 显式 MCP 工具接入（2026-09-08）

新增 `ksadk.resource_runtime.langgraph.create_bound_resource_tools` 异步上下文管理器。
可信执行器在 admission 后提供现有 Core connector、activation leases 和明确 aliases，
在上下文内将返回工具交给 graph factory、ToolNode 或 model.bind_tools。会话和凭证
不进入 graph state；退出上下文后，先前保存的工具返回 `RESOURCE_SESSION_CLOSED`。
续租后需在安全执行边界使用新 leases 重新打开上下文，不自动重放调用。

实现复用已安装的 MCP SDK Streamable HTTP transport 和 LangChain StructuredTool，
未新增 `langchain-mcp-adapters` 依赖。这是对设计 9.2 节适配库选型的实现调整，
连接仍指向同一 Core，没有新增 Python 资源传输协议或模型执行循环。
工具目录必须与授权 aliases 完全匹配；成功响应保留 MCP payload 为 ToolMessage
artifact，业务失败保留结构化 JSON 诊断并投影为 ToolMessage error。写操作仍受
broker 宿主审批及稳定事件校验约束，helper 不签发审批、不提供默认写入许可。

验证：`tests/resource_runtime/test_langgraph_resources.py` 使用实际编译的 StateGraph、
ToolNode、官方 Cordis/DSH 工具 Core、HTTP MCP 与独立 Python Worker，验证 KB 命中、
空结果、上游失败、撤销后不发送上游请求、关闭后工具拒绝调用以及消息中不含 token。
这不是模型/平台资源 E2E；Studio Framework Build admission、graph factory 配置接入、
自动记忆节点、checkpoint 事件与审批关联以及宿主自动续租仍待接通。

### Studio 生成的 LangGraph factory 接收资源工具（2026-09-08）

`runtime_source.py` 生成的 `ksadk_graph_factory` 新增可选 `resource_tools=()` 参数。
每次构图冻结基础工具与资源工具的集合，模型 bind_tools 和 ToolNode 共同消费该集合；
不修改模块 `_tools`。重名工具在构图时返回 `RESOURCE_TOOL_NAME_CONFLICT`。
原有仅传 checkpointer 的调用保持有效；资源 MCP session 必须覆盖整个 graph 调用。

集成测试现在实际执行生成源码，编译带资源工具和不带资源工具的两个 graph，
以受控模型响应驱动“模型 → MCP 工具 → 模型”流程。工具侧仍是真实官方 Core、
HTTP MCP、Worker 和测试上游。验证两个 graph 不互相覆盖工具、checkpointer 保持
调用者传入实例、重复工具拒绝、checkpoint 不包含 MCP token 或模型凭证。
模型响应为测试替身，不属于真实模型回合或平台 E2E。

首次执行生成源码发现本地 uv 环境没有安装项目已有的 LangGraph extra。
已使用 `uv sync --locked --extra langgraph --inexact` 安装 lock 声明依赖（包括
langchain-openai 1.4.0）；没有改变 pyproject、uv.lock 或版本号。
仍需将 Studio 的 admission/Build 快照与运行时会话所有者接到这个 factory；
当前 Framework 编译器的原生插件门禁保留，不能据此宣称 Studio 配置已经可运行。

### 知识库 HTTP 撤权停止绑定调用（2026-09-08）

真实 SDK 的 `_check_status` 会把 HTTP 非 200 统一转换成不含状态码的
`ServerNetworkError`。此前 BoundKnowledgeService 将其全部投影为普通检索失败，
HTTP 401/403 后仍能继续发请求。KnowledgeBaseClient 现在在委托 SDK 状态检查前
记录本次 HTTP 状态，每次 search 重置；不根据错误文本或业务 JSON 中的数字猜测权限。
BoundKnowledgeService 将确认的 HTTP 401/403 投影为固定 RESOURCE_FORBIDDEN。
Broker 消费该结果后撤销该 activation/binding 的所有句柄，保留无关绑定。

`test_knowledge_revocation.py` 使用真实签名 SDK、独立 Python Worker 和 HTTP 测试
上游验证 401、403 各自停止后续 KB 请求/拒绝旧句柄续租，同时同 Worker 内记忆读取
仍成功；503 后仍可恢复 KB 读取；原始上游错误正文不进入工具结果。

此变化尚未实现控制面撤销通知、业务错误码授权分类、required/optional 的运行终止
策略或 MCP inventory 的动态移除。Memory 和 discovery Skill 各自的鉴权失败处理
仍需统一；不能将本项描述为全部资源的授权生命周期已经完成。

资源选项查询仍缺少当前控制面源码/完整响应合同；本 checkout 的 AgentEngineClient
没有三类 List* 封装，本次没有依据接口名猜测字段或上线未验证的列表代理。

### 记忆资源 HTTP 撤权与写入记录（2026-09-08）

SdkLTMBackend 捕获 SDK 丢弃前的 HTTP 状态。AicpMemoryProvider 在独占后端调用
范围内重置/读取该状态；确认 401/403 后在本 activation 内保持不可用并清除观察到的
记忆 ID。该路径覆盖 load/save/update/delete/status；不会根据错误文本或业务 JSON
推测撤权。搜索返回结构化 unauthorized，其他调用通过固定异常跨到 Worker 的
固定授权失败响应。Worker 本身继续承载未被撤权的其他资源。

Broker 在解析写入结果前识别授权失败并撤销该 binding 的句柄。已 claim 的写入保持
unknown 并返回 operationReceipt 供可信宿主核查，不把授权失败当成业务写入成功；
状态查询撤权也不把原 accepted_pending 操作改成失败或重新提交。续租/新的 activation
仍需重新走宿主权限验证，不能凭上游后来恢复 200 复活旧句柄。

新增 10 项独立进程集成场景（5 个操作 × HTTP 401/403），验证不再发出第二次上游
请求、敏感错误正文不外泄、持久操作状态保持，以及同 Worker 中的 KB 仍可检索。
相关 Provider/变更语义测试与这些场景共 36 项通过。此为测试上游验证，尚不覆盖
平台授权服务通知、业务错误码、required 绑定对整个 Run 的终止策略和真实平台 E2E。

### Skill Service 撤权与下载失败分层（2026-09-08）

Worker 不再因 discovery Skill Service 的 HTTP 401/403 退出整个 activation。
目录查询或 GetSkillDownloadUrl 鉴权失败进入统一 RESOURCE_FORBIDDEN 路径；Worker
维护该 activation 的已撤销 binding 集合，Broker 同时撤销句柄。之后即使上游恢复，
旧绑定仍不再发请求。同样记录 KB/Memory 已观察到的撤权，作为 Worker 内的防护。
协议错误和进程崩溃仍按整个 Worker 故障处理。

区分 Skill Service 授权失败与对象存储临时下载 URL 失败：下载 URL 返回 401/403
仍是脱敏的 SKILL_RESOURCE_UNAVAILABLE，不能单凭该响应宣称 Skill 空间 grant 被撤销。
后续显式只读重试可以重新向 Skill Service 获取 URL；不会把 URL/错误正文返回模型。

更新独立 Worker 测试覆盖 ListSkillsBySpaceId/GetSkillDownloadUrl × 401/403，验证
不重复访问被撤权的服务、同 Worker 中 KB 继续可读；新增对象下载 401/403 后重新
获取 URL 并加载成功的测试。原“401/403 必须杀 Worker”测试随正确故障范围更新。
此变化不等于控制面主动撤权、工具目录即时刷新或 required 资源的 Run 终止策略完成。

### MCP 工具目录反映当前句柄（2026-09-08）

资源 IPC v1 新增可选严格布尔 `checkOnly`（默认 false）。Broker 完成句柄、操作、
activation 和 deadline 校验后，检查请求参数为空并直接返回本地 available；不进入
Worker、审批或操作 ledger，不访问上游，不续租。Worker 管道拒绝 checkOnly 请求，
避免错误接线把目录检查执行为业务请求。旧调用省略此字段保持原行为。

同 Core 的 MCP tools/list 对 scope v2 资源执行上述只读检查，隐藏已撤销或无法确认
句柄的工具；普通工具/root inventory 语义保留。每轮检查最多沿现有 scope 的 32 个
别名并发执行，单次检查预算 3 秒。返回前再次检查 MCP token 撤销/到期。
该结果只反映本地能力租约，不是平台授权重验；真正 tools/call 仍独立校验句柄。
不会因 tools/list 可见而允许未经宿主审批的写操作，也不改变 Core 全局工具注册。

真实官方 Core/HTTP MCP/独立 Worker 测试增加：目录不触发上游检索；KB 上游 403 后，
仍在有效期内的 MCP token 再次列目录时不再显示该工具。合同测试覆盖 probe 不写入、
不授权、无效类型/参数/操作/期限拒绝，以及 Worker 已停止但 monitor 尚未清理的窗口。
已刷新私有 capability-host bundle 内容摘要，未变更版本号。

### Discovery Skill 的本地持久运行选择（2026-09-08）

新增 DiscoverySelectionReceipts，复用 OperationLedger 的同一 SQLite 数据库，不新建
授权库或资源上传服务。记录仅含 Skill ID、version ID、content hash、包名和显示版本，
不保存下载 URL、凭证或正文。每个授权运行范围最多 32 个选择，事务保证同一 Skill
的首个版本不可被并发请求改写；相同选择可重复确认。

DiscoverySkillWorkerBinding 需要宿主提供稳定 `selectionRunRef`。记录/缓存范围摘要
包含该 run 引用、完整会话身份、Build、绑定快照和 binding ID，排除易变的 generation
与 activation。Supervisor 要求持久账本，在启动 Worker 前从账本恢复选择；覆盖而非
信任输入中附带的 restoredSelections。不同 run/主体/Build 不共用选择。

Worker 首次 load/read 返回内部选择元数据；Broker 校验其与请求和结果对应，先提交
账本再返回内容，并剥除内部字段。提交失败时不给消费者正文；返回前仍重验租约。
恢复后使用原来的精确包缓存，缺失/损坏时拒绝读取，不去远端静默选择新版本。
服务直接单独实例化仍可用于 activation 内发现，但正式 Supervisor discovery 激活
缺少账本会返回 RESOURCE_DISCOVERY_RECEIPTS_REQUIRED。

测试包含：真实 HTTP Skill Service 测试上游和独立 Worker 跨 generation 重启后保持
旧版本、新 run 使用升级版本、缓存损坏不替换、提交失败不返回正文、伪造预载不覆盖
账本；SQLite 并发首写、主体/run 隔离、32 项限额和真实事务失败回滚。
这仅证明本机持久卷与账本上的恢复；尚未证明跨 Pod 缓存交付、云端共享账本、原生
Skill 热发现或 discovery 隔离执行。Discovery execute 仍明确拒绝，不能将本项视为
完整 Skill 执行能力完成。宿主还需要把真实 Run ID 与引擎 checkpoint 生命周期接线。

### Discovery 选择接入隔离 Skill Runtime（2026-09-08）

Discovery Worker 支持绑定 Build 中的显式 E2B 目标、进程内 Secret 和产物目录；三者
必须完整且与冻结 snapshot 匹配，只有这种绑定才允许 execute_skills/read_skill_artifact。
复用 pinned 的 Runtime 工厂、执行路径、取消预算和产物持久化，没有创建第二个执行器。

Broker 在执行前从本 run 的 DiscoverySelectionReceipts 读取请求中全部 Skill 的记录。
没有先 load/read 并提交记录则拒绝；仍经过宿主审批。选择记录只在验证模型参数之后
加入私有 Worker 参数，模型自行提供该字段会被 schema 拒绝。Worker 再次核对记录
与已选包的版本/摘要，匹配后才把 verified packages 交给 Runtime，不发起发现或下载。
幂等参数摘要包含宿主选择记录，避免同一事件换包字节后仍被当成相同执行；产物沿用
既有 ledger 和 artifact receipts。普通 pinned 执行保留原调用合同。

测试用私有 Worker 帧与 E2B transport double 驱动现有 Runtime，实际运行包内脚本，
验证引用文件生成的产物为 version-one。缺少或改变选择记录时，沙箱创建计数为零。
另验证目标漂移、凭证只进入私有初始化、未审批/伪造记录拒绝、重复执行复用产物、
同一稳定事件改变选定版本返回冲突。工具描述更新为同时适用 pinned/discovery。

这些是 Runtime/Worker 合同和实际脚本测试，沙箱连接仍为测试替身；尚未完成真实
E2B/平台 Skill 空间联调，也未接通 Studio Build/Run 对这组参数的完整交付。
此前“discovery execute 无运行时适配”的记录由本段取代，不代表云端或产品 E2E 已完成。

### Wheel 与干净安装验证（2026-09-08）

新增 opt-in `tests/packaging/test_resource_distribution.py`，从隔离源码副本构建 wheel，
逐字节比较当前 resource_runtime 模块、四个资源 Bundle、capability-host 与选定交付
辅助模块，并确认 Studio/server 静态入口存在、不包含 node_modules 或测试目录。
复用已有 wheel 构建辅助函数，新增 include_static 参数；既有调用默认行为保持不变。

测试根据当前 uv.lock 导出 langgraph extra 约束，在全新虚拟环境安装构建出的 wheel。
使用 Python -I、独立工作目录及不含凭证的最小环境启动已安装模块，确认导入路径位于
新虚拟环境。实际启动独立 ResourceWorker，经私有管道调用 KB SDK，向本地 HTTP
测试服务发送检索，核对返回正文、文档来源与仅一次上游请求，随后关闭 Worker。
同时验证安装环境中 `python -I -m ksadk plugin --help` 正常退出。

验证命令：`KSADK_TEST_RESOURCE_WHEEL=1 uv run --no-sync pytest -q
tests/packaging/test_resource_distribution.py`，结果 2 passed（18.24 秒）。两个测试文件
Ruff 与 git diff --check 均通过。测试临时目录保存 wheel 摘要、实际导入路径和启动/
HTTP/CLI help 结果，不含真实凭证。本轮没有修改依赖、版本号或发布产物。

本项证明 Python 发行物内容和干净安装后的 Worker/SDK 调用；HTTP 上游仍为 fixture。
尚未证明 npm 独立安装、CLI 完整插件生命周期、浏览器 UI、真实模型/平台或云端 E2E，
不能以这两项通过替代方案 A01–A26 和三引擎完整验收。

### 将资源 Build 与 DSH 安装快照绑定（2026-09-08）

核对正式 Build/Run 接线时发现：已有 Studio 激活分别验证 Worker 的资源快照和调用方
提供的 DSH 快照，但没有验证两者属于同一 Build。现给 ResourceSnapshot 增加
`dshProfile`，保存既有 DshProfileBuildSnapshot（配置投影、依赖锁和安装内容摘要），
其摘要纳入资源 snapshot digest，进而进入资源制品摘要与 Worker scope fencing。
字段省略时保持旧独立资源快照的摘要算法；正式 Studio 路径不接受省略。

Studio resource Build materializer 在下载前要求该快照。保存的 resource-build.json
包含完整宿主快照，离线恢复若被替换则摘要校验失败。Studio activate_resources 要求
资源内的宿主快照与 prepared generation 精确一致，随后仍验证真实安装内容。缺失、
错配的输入返回 RESOURCE_BUILD_PROFILE_MISMATCH，不启动新 Worker，也不处置其他
已通过验证的运行；真实安装漂移仍按既有规则撤销 generation。

回归新增缺失/配置/依赖锁/安装摘要错配，以及下载前拒绝未关联快照、持久制品中的
宿主摘要被替换后拒绝恢复。既有真实 Worker 生命周期、续租和服务重启后的写入防重
用例更新为带宿主快照的正式激活输入。此项不授予平台资源权限，也没有移除 Codex/
Framework 的原生插件构建门禁；正式三引擎 composition、权限 admission 与 Run 交付
仍需接通，不能将新增快照字段视为完整 Build/Run 已完成。

验证：启用既有官方 Core 与 E2B SDK 合同测试依赖路径后，resource_runtime 全目录及
Studio materializer/status/validation/connections/DSH service/API 回归共 494 passed、
16 warnings（58.98 秒）；涉及文件 Ruff 与 git diff --check 通过。该回归没有真实平台
资源、模型或云沙箱调用，不能作为最终产品端到端证据。

### 停止运行时回收审批等待任务（2026-09-08）

修复实际生命周期问题：Supervisor 关闭 Worker 后，Broker.retire_activation 原来只
等待在途任务自然结束；若任务等待宿主审批、审批 UI 没有回应，停止操作会持续持有
supervisor 锁。修改现有真实 Worker 测试，使审批一直不返回，旧实现确定超时失败。

现在 lifecycle owner 在撤销权限并关闭 Worker 后，取消并等待该 activation 的审批/
排队/响应任务，再移除 Broker 状态。dispatch 用 asyncio.wait 区分子任务的关闭取消
与调用方自身的取消：调用方断线/超时继续保留底层任务与容量，不隐式取消或重发写入；
宿主关闭导致的取消返回固定资源不可用错误，写入保守返回 RESOURCE_WRITE_UNKNOWN。
已经持久化的 claim 不回滚，重启后仍不能重放结果未知的操作。

新增回归覆盖：停止不等待审批 UI、在途及排队检索被回收且不返回正文、调用方取消后
已发送写入继续保留、宿主终止后恢复账本不重复写入。本项没有增加模型审批工具、
自动重试或第二套会话所有者，也不意味着原生引擎的所有取消 hook 已完成接线。

验证：resource_runtime 全目录、Studio DSH service 与 binding status 回归共 471 passed、
16 warnings（52.88 秒），启用既有官方 Core/E2B SDK 合同测试依赖路径，无 skip。
修改的四个 Python 文件 Ruff 及 git diff --check 通过；真实平台/云端 E2E 尚未执行。

### 无有效租约时回收 activation（2026-09-08）

修复 Worker 只监听进程退出、租约过期后无人调用就永久占用容量的问题。通过真实
独立 Worker、1 秒测试租约复现旧实现 3 秒内不能回收的失败。Registry 新增只读
remaining_lifetime，使用与签发相同的单调时钟，返回当前句柄中最长有效剩余时间，
不签发、不续租，也不能代替操作授权。

Supervisor monitor 同时等待 Worker 退出、续租通知和有效期；每次唤醒都在同一
生命周期锁内读取当前 leases。全部租约过期或撤销时执行既有 deactivate 清理，
释放 Worker、审批归属与 Broker 状态。正常续租更新 resources 并通知 monitor，
旧计时器不会撤销新句柄；缩短有效期也会立即重新安排等待。完全撤销且无续租通知
的情况最多 30 秒重查一次。仍有其他有效绑定时保留 Worker，各调用独立校验权限。

这不是自动续租或上游权限重验，也不恢复旧 activation ID；新的运行必须重新准入。
租约失效期间的在途写入仍按既有 unknown/持久防重处理，不宣称终止进程等于上游
事务回滚。共享 generation 的 socket 可以继续服务其他 activation。

新增测试覆盖到期无人调用时实际 Worker 退出与容量释放、续租跨过旧期限仍可用、
新期限到期后回收、缩短租期唤醒，以及多个句柄/轮换/撤销的剩余时间观察。初次实现
的多事件等待遗漏 FIRST_COMPLETED，导致关闭等待超时；已主动中断该次测试并修正，
不把中断的结果计为通过。后续小范围回归 28 passed、2 skipped（未启用可选 Core
依赖的该次运行）；最终证据以启用依赖后的完整回归为准。

最终回归：启用既有官方 Core/E2B SDK 合同测试依赖路径，resource_runtime 与 Studio
resource materializer/status/validation/connections/DSH service/API 共 501 passed、
16 warnings（67.58 秒），无 skip。三个修改文件 Ruff 和 git diff --check 通过。
这些结果仍不包含真实模型、平台资源或云沙箱端到端调用。

### 官方 Bundle 的真实 CLI 打包与生命周期（2026-09-08）

新增 opt-in `tests/packaging/test_resource_bundle_cli.py`。准备临时目录下的 pnpm 11.7.0
与 DSH 0.1.1-rc.2，通过项目真实 `plugin toolchain install/status` 确认版本和可用性。
未更改用户默认工具链/profile，也没有 npm 发布或版本号变更。

测试使用临时 HOME、DSH_HOME/profile、源码副本与只含工具链引用的环境，实际执行
`python -I -m ksadk plugin ... --output json`。四个资源 Bundle 和 capability-host 均
通过 `pack` 生成 tgz，核对归档文件白名单、代码/patch 字节及包名/版本。随后使用
tarball 依次 install、enable、list/profile、disable、uninstall，验证安装默认停用、
启用集合精确匹配、全部卸载后只剩原生 dsh-base 投影。

第一轮配置断言遗漏 DSH 自带 dsh-base 而失败，按实际原生组合修正测试；之后完整
私有 Bundle 生命周期 1 passed（62.38 秒）。另扩展同一用例，从 npm 安装未经修改的
`@deepseek-ai/dsh-web-app@0.1.1-rc.2`，验证共存与资源插件卸载后保留上游插件，再
清理上游插件。每条 CLI 命令退出码和五个 tarball 摘要保存在临时 cli-evidence.json。

该用例证明真实 CLI/包管理和 DSH 配置投影，没有浏览器渲染、资源调用或模型回合；
因此不能替代外部插件 UI、单 Core 资源业务、三引擎或云端最终 E2E。

外部 Bundle 初次安装因 pnpm 11 的原生脚本策略拒绝 koffi@3.2.1，CLI 正常回滚。
核对已安装 pnpm 的 allowBuilds 解析与 DSH profile 初始化代码后，在 CLI 已初始化的
临时 profile 中仅批准该精确依赖版本的构建，再执行外部包安装。没有开启全量脚本
许可或修改外部源码。该前置条件记录到验收脚本和 evidence；不能将未批准依赖时的
安装失败解释成资源 Bundle 业务回归通过。

扩展后的共存用例当前未通过：五个私有 Bundle 与上游 Web App 均安装/启用，list 和
profile 集合验证成功，但卸载 capability-host 时原生 pnpm remove 超过 120 秒，CLI
返回 dsh_plugin_host_unavailable；用例失败（173.53 秒）。确认原子进程已经结束后，
在该临时诊断 profile 使用 pnpm remove 的 `--config.offline=true` 成功（无下载），
说明已安装字节足以完成卸载，但正常联网路径超时的原因仍需定位。没有据此修改
生产卸载行为、强制离线或将扩展用例计为通过。当前新增 opt-in 用例保留此失败。

本轮另确认一项正式启动接线缺口：平台桥 apply 要求宿主提供 socketPath，而当前
Studio/Core 启动路径未发现该注入；CLI dump-config 成功不证明资源 Core 能启动。
后续需要在启动时交付临时 IPC 地址，并保持地址不进入不可变 Build。

### Studio 完整 Core 启动与资源 IPC 交付（2026-09-08）

已补齐上一节的启动依赖：ResourceSupervisor 可以先启动空 broker，不创建 Worker、
不签发资源 lease。Studio 在资源 profile 的 Core 启动前准备 broker，将其私有 socket
通过 capability host 的临时 overlay 交付给 platform-resources。Core 就绪及冻结
profile 校验完成后，资源激活复用该 supervisor；socket 不进入 Build/profile 快照。
Host 校验 socket 为绝对路径、非链接、真实 socket，且目录及 socket 无组/其他用户权限。

Core 启动或 MCP 初始化失败时关闭空 broker；同一冻结 profile 的失败启动可以保留
Host 的 circuit breaker 后重试，但必须创建新 socket。已经就绪而未获对应资源快照
证明的 Core 仍不能通过该重试规则接管。显式 DSH executable 的版本探针改在 executable
所在目录执行，避免全新 HOME 下默认 managed toolchain 目录不存在导致启动前误失败。

新增 `tests/resource_runtime/test_studio_resource_core_startup.py`，使用临时 HOME/profile
和真实锁定 DSH 0.1.1-rc.2 CLI 安装 platform-resources/knowledge，调用实际 Studio
prepare/activate 路径。测试核对 Core 启动时没有 Worker/lease、运行 overlay 含 socket
而 Build 快照不含，随后通过真实 MCP/LangChain tool 调用实际 Worker，向本地 HTTP
知识库 fixture 发出一次绑定查询。关闭后核对 Core PID、overlay、socket 和 Worker 清理。

首次完整 Core 启动失败，发现临时工具链中 dsh-agent/dsh-tools 等包缺失 lib/index.js。
直接读取 npm 官方同版本 tarball 确认两个包均包含该入口；没有证据支持上游发布缺包。
对该临时工具链执行锁定版本的 pnpm install --frozen-lockfile --ignore-scripts --force
后恢复入口，未升级版本、修改上游源码或触碰用户默认安装。缺失文件的最初成因尚未确定。

本轮 fresh verification：

- Studio service、capability host、resource renewal 与启动测试文件：55 passed、1 skipped，
  50.10 秒；该次没有配置 CLI 根目录，可选真实启动用例被跳过。
- 指定 KSADK_TEST_RESOURCE_CLI_ROOT 后单独执行真实启动测试文件：2 passed，12.03 秒，
  包含完整 Core/Worker/MCP 调用以及全新 managed 目录缺失的显式 executable 回归。
- 六个相关 Python 文件 Ruff 通过。

这是实际完整 Core 启动和本地数据面 fixture 的集成证据，不是真实模型/平台资源/云沙箱
端到端证据。Studio 编辑器、正式 Builder/Run 接线、三引擎生命周期及云交付仍未完成；
上一节外部 Web App 共存卸载的联网超时尚未通过复测，不能据此宣布其恢复。

### 三类 Bundle 同 Core 与外部插件卸载复测（2026-09-08）

扩展真实 Studio Core 启动用例，同时安装 knowledge、memory、skill-center 与公共桥。
仅激活知识库绑定时，实际 MCP inventory 经 helper 精确匹配校验，只向当前运行暴露
知识库别名；知识库调用与关闭清理继续通过。该测试文件 2 passed（13.53 秒），
不将未绑定记忆/Skill 的隐藏验证表述为其业务端到端通过。

工具链入口恢复后重新执行外部 Web App CLI 共存用例，仍在 capability-host 卸载处
失败：1 failed（174.84 秒），原生命令达到 120 秒超时。安装、启用、list/profile
集合及停用步骤完成。失败后，在这一临时 profile 直接调用 pnpm remove，设置诊断用
fetch-timeout=10000、fetch-retries=0，689 毫秒完成、无下载。首次诊断因 /var 与
/private/var 路径别名产生 store location 不一致；将临时路径 resolve 后才执行成功。
没有修改生产网络/卸载策略，也不复用直接诊断修改后的 profile 作为原子 CLI 验收证据。
结果将后续定位指向 DSH 原生 remove 调用与直接 pnpm 调用之间的差异，根因尚未确定。
两个测试文件 Ruff 和 git diff --check 通过。

### 包管理超时后的进程组清理（2026-09-08）

核对锁定 DSH 的原生 plugin 命令：它使用 spawnSync 启动 pnpm，继承标准流。
在之前已经直接诊断修改的临时 profile，再直接执行 DSH add/remove 均成功；
因此尚不能将真实 CLI 验收中的超时归因于 DSH 转发本身或上游网络，原始失败仍保留。

发现并修复独立的失败路径风险：bridge 原先使用 subprocess.run 超时处理，只终止
直接 DSH 进程，不能保证 pnpm 子进程先于 profile 回滚退出。现在 Unix 命令使用独立
session/process group，communicate 超时或中断后终止整组并回收父进程，再传播异常。
正常输出、退出码和原有错误脱敏保持；未更改 120 秒命令预算或 pnpm 网络策略。

新增真实子进程回归，覆盖父进程等待与父进程先退出而子进程仍持有管道两种情况。
子进程原计划在超时后写入模拟 profile；验证超时返回后它不能再写入，避免回滚竞争。
另外验证成功输出和失败输出的 token 脱敏。清理测试、source policy、Studio plugin API
共 19 passed（7.57 秒、5 warnings）；真实锁定 CLI/Core 启动测试 2 passed（13.43 秒）。
相关 Ruff 与 git diff --check 通过。该修复不代表外部 Web App 完整卸载验收已经通过。

### 卸载完成日志与退出阶段诊断（2026-09-08）

在两个全新临时 HOME/profile 重放真实 CLI 用例，仅在诊断进程 PATH 中包装 pnpm，
将原生 stdout/stderr 记录到临时文件。第一轮确认 remove 打印 Done（732 毫秒），
但实际 pnpm、包装 shell、DSH 都持续存活，最终超时并执行 frozen-lockfile 回滚。
输出重定向没有修复失败，不能将 Done 当成命令成功退出。

第二轮仅在诊断 wrapper 中用 Node preload 定时记录活动句柄类型和请求类型，
不修改上游源码。remove 完成后持续观察到 MessagePort，活动请求列表为空；第一轮
对应存活 pnpm 的 lsof 未发现活动网络连接。锁定 pnpm 源码存在 WorkerPool 与
finishWorkers 路径。证据将下一步定位收敛到 Worker/MessagePort 退出生命周期，
尚未证明是哪一个具体缺陷；不能继续把该失败简单归因于联网下载或 Python 等待 EOF。
两次诊断均真实失败、退出码 1，未修改生产 pnpm 参数、版本或卸载成功标准。
诊断脚本和日志仅在临时目录，未使用真实模型或平台凭证。

### 完成后重新创建 Worker 的诊断（2026-09-08）

进一步在临时 Node preload 中仅记录 Worker 创建栈、任务类型、结果状态、STOP 和 exit，
使用新的临时 HOME 重放失败。remove 的 Worker 1–9 均收到 STOP 并以 0 退出，打印
Done（740 毫秒）后又创建 Worker 10–13，其调用栈进入 addFilesFromTarball/fetch。
这些解包任务返回 success，但没有相应 STOP/exit，原生命令最终超时。该证据确认
后台下载解包在首次 Worker 清理后重新创建了线程，并非根据 MessagePort 名称猜测。
没有在生产中注入 preload、修改第三方源码或强制成功退出。

另外在全新临时 profile 验证 remove --lockfile-only 后 install --frozen-lockfile 的
两步候选流程。第二步打印 Done 后同样不退出，最终测试失败、退出码 1；这条候选
没有进入 bridge 生产代码。过程中的完成日志不足以证明成功，必须检查实际退出码。
已阅读官方 pnpm 版本记录及 11.22.0 归档内 changelog，尚无足够依据将其中修复与
本次退出路径直接对应，因此没有盲目调整锁定版本。下一步需验证候选工具链的完整
共存生命周期；当前外部插件卸载 gate 仍未通过。

### 工具链版本与依赖布局候选验证（2026-09-08）

在独立临时目录安装官方 pnpm 11.22.0，仅通过临时 PATH wrapper 用于 DSH 转发的
包管理命令；SDK toolchain status/pack 的锁定路径仍为 11.7.0。全新临时 HOME 下
11.22.0 同样在 capability-host remove 打印完成后不退出，最终用例失败、退出码 1。
因此没有调整正式 PNPM_VERSION，也没有将混合版本诊断当作工具链升级验收通过。

随后保留 pnpm 11.7.0，在另一个全新临时 profile 的 DSH 包管理命令中显式使用
node-linker=isolated 与 confirmModulesPurge=false。真实资源 CLI 用例完整返回 PASS：
五个 Bundle 打包、安装、启用、与未经修改的 dsh-web-app 共存、逐个停用/卸载，
保留上游包，再卸载上游包并核对只剩 base 投影全部通过。原先失败的 remove 正常
退出，没有通过强杀、离线模式或更改成功判断来绕过 gate。

同一临时布局策略用于完整 Studio Core 启动用例，2 passed（13.73 秒）：三类业务
Bundle 可装配，只有知识库绑定时只暴露对应工具，真实 MCP/Worker 调用本地 HTTP
fixture 成功，关闭清理通过。诊断启动器初次用函数替换 Popen 导致 MCP 导入类型
标注失败，改为 Popen 子类后才得到该通过结果；没有修改生产 Popen 接口。

上述通过属于显式临时 isolated 布局的候选证据，正式 bridge 尚未持久化选择该布局。
下一步须设计并验证新 profile 配置与已有 hoisted profile 迁移/回滚，不能将临时
wrapper 留作产品依赖，或宣称未改变的默认 CLI 卸载路径已经修复。未修改上游源码、
正式工具链版本或用户现有 profile；真实平台/模型/浏览器验收仍未完成。

### 新建 profile 正式采用 isolated 布局（2026-09-08）

bridge 现在仅在首次 add 且 profile manifest 尚不存在时向原生 DSH/pnpm 传递
node-linker=isolated；原生初始化完成后，将 nodeLinker 持久写入新生成的
pnpm-workspace.yaml，保留 packages、autoInstallPeers 等原生字段并设为私有文件权限。
后续命令直接读取持久配置，不再依赖临时 wrapper、网络参数或命令行布局覆盖。
初始化及后续 Bundle 校验仍在既有事务内；失败时按原有新 profile 回滚清理。

已有 profile 的设置不被普通 add/remove 静默迁移。因此本次修复覆盖新建 managed
profile；已有 hoisted profile 的显式迁移、运行影响与回滚路径仍需后续实现及验证。
正式 pnpm 保持 11.7.0，未修改 DSH 源码、版本号或用户现有 profile。

新增 layout 回归验证新配置保留原生字段、首次安装校验失败会清理初始化产物，以及
已有 profile 的字段和注释保持。layout、进程清理、source policy、Build snapshot、
Studio plugin API 共 31 passed（7.86 秒、5 warnings）。移除全部诊断 wrapper，
以正式 CLI/bridge 执行完整资源 Bundle 加外部 Web App 的打包/安装/启停/卸载，
再执行完整 Core/MCP/Worker 启动测试：3 passed（95.95 秒）。这次通过来自实际
生产代码的新建 profile 路径，已不只是布局候选实验。相关 Ruff 与 diff check 通过。
真实模型、平台资源、浏览器 UI 和云部署的最终验收仍未完成。

### 已有 hoisted profile 的显式迁移（2026-09-08）

新增 bridge 的 migrate_to_isolated_layout 与 Studio 的
POST /api/v1/plugin-ecosystems/dsh/profile:migrate-layout，使用既有鉴权/CSRF 与
acceptHostPermissions 确认字段。入口经 Studio reconfigure_dsh_profile 暂停准入、
撤销旧 Core 状态后运行；未增加可绕过该保护的迁移 CLI 或自动迁移已有 profile。
底层 bridge 方法要求调用者拥有停止/准入控制，不用于接管外部自行启动的 Core。

迁移仅接受 hoisted -> isolated，已经 isolated 时只做预检。迁移前验证来源 receipt
与安装树边界，在私有临时目录复制独立字节备份并核对摘要，然后按原锁文件重装。
依赖锁必须字节一致；恢复原 manifest/启停状态，防止 DSH reconcile 自动启用已停用包。
安装、锁文件变化或预检失败时，直接恢复目录和配置，不重跑有缺陷的 hoisted 安装。
如果恢复自身失败，保留安装字节与配置备份，返回 DSH_PROFILE_RECOVERY_REQUIRED，
Studio 保持准入暂停。布局变化会使原安装摘要失效，需要重新构建/验证对应运行。

HTTP 请求取消时，迁移线程仍受重配置保护直到完成；重复取消不能提前释放保护，
恢复失败的错误优先于取消传播，防止误恢复运行准入。尚无前端迁移按钮/浏览器验收。

实际使用锁定官方 DSH/pnpm 在临时目录创建旧 hoisted profile，安装已有资源 Bundle
tarball，停用插件，执行迁移，再卸载：通过。核对 manifest、lock 字节保持、停用状态
保持、isolated 布局生成、卸载后 inventory 为空。初次诊断使用原生目录 link，因安装
树外链接被拒绝；改用真实 tarball 后通过，未放宽边界校验。没有迁移用户现有 profile。

最终回归：Studio API（含请求取消与恢复失败保持暂停）、layout（成功、安装失败、
锁变化、预检失败、恢复失败保留备份）、进程清理与 Build snapshot 共 30 passed，
5 warnings（8.83 秒）。五个相关 Python 文件 Ruff 与 diff check 通过。该证据不等于
真实平台资源、三引擎回合或云端最终验收完成。

### 正式 Build/Run 与源码依赖重新核对（2026-09-08）

重新检查当前 Codex Builder、资源静态校验、Memory resolver、Studio PCM helper 与
调用点。Codex Builder 仍只接受 Codex 原生插件快照；资源静态校验明确报告
RESOURCE_AUTHORITY_UNVERIFIED，现有连接声明不能作为平台主体/权限证明。
materialize_resource_build、prepare_resource_generation、activate_resources 与
create_bound_resource_tools 的独立测试不等于正式 Builder/Run 已调用这些入口。
recall_platform_memory 目前也未发现生产调用点，不能把这个旧 helper 当作已经接通的
自动召回生命周期。下一步必须在实际装配入口接入权威授权与生命周期，而非继续增加
无人调用的辅助函数或仅删除编译门禁。

通过可用项目列表及 game03、leo-repo 源码路径重新查找，仍未找到设计引用的
agentengine-server 资源/Agent action、component_config 或 Studio AgentEditor 源码。
需要用户提供控制面/前端 checkout 位置（或对应可访问仓库），以及真实平台测试资源
配置或已有 Secret 引用；模型 API Key 不能替代这些资源权限。没有从旧内部提交恢复
源码、伪造资源授权或把本地 fixture E2E 改称完整平台端到端。完整目标保持未完成。


### 阶段性分支提交验证（2026-09-08）

本次保存 SDK/runtime 阶段性实现，正式 Builder/Run、Studio 资源选择 UI、权威授权、
自动记忆、三引擎真实回合及云端交付仍未完成，不作为发布或完整 E2E 通过声明。
用户已提供真实资源测试配置，此前缺配置的记录已不再适用；服务端与前端源码仍待补齐。
只读实测：模型调用 HTTP 200；Skill 请求连接失败；知识库 HTTP 403；记忆 HTTP 502；
沙箱仅确认 TCP 连通。未写入测试记忆，也未执行真实沙箱任务。配置值、资源 ID 和凭证
均不纳入本记录。

提交前重新执行：

```bash
uv run --no-sync pytest -q tests/resource_runtime tests/studio/test_resource_binding_status.py tests/studio/test_resource_binding_validation.py tests/studio/test_resource_build_materializer.py tests/studio/test_resource_connections.py tests/studio/test_dsh_plugin_api.py tests/plugins/test_dsh_profile_layout.py tests/plugins/test_dsh_command_cleanup.py
```

结果：481 passed、12 skipped、16 warnings，61.83 秒。跳过项需要额外指定的官方
DSH/Core/CLI 或 E2B SDK 检查环境；本轮没有把它们计为通过。
所有变更 Python 文件执行 Ruff，发现 SQLite store 的 I001、F401、E501 共三项；
使用 HEAD 原文件复核，三项均在相同行号已存在，本次无新增 Ruff 问题。
147 个变更/新增文件的仓库敏感信息规则扫描及本次配置定向扫描均无命中；
git diff --check 通过。未执行发布 preflight、发布、版本号修改或云部署。
