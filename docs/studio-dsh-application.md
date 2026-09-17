# Studio 作为 DSH Core 的应用界面

## 目的与归属

Studio 保留 Agent、会话、构建、部署、资源与观测等产品功能。DSH 社区插件使用完整的官方 Core 服务与客户端上下文；插件界面直接出现在 Studio 中。用户不再进入另一套带导航、会话列表和模型设置的 DSH 页面。

- 每个 Studio 工作区使用一个受管理的 Core Profile / 进程 / HTTP 服务，能力调用与界面共用该实例。
- `@kingsoftcloud/dsh-app-studio` 是应用层组合插件，注册官方 `root` slot，通过官方 `renderSlot` 挂载社区插件贡献。
- 官方 Core 负责 Cordis 注入、模块加载、React 上下文、插件 RPC、配置及资源生命周期。应用插件不实现这些服务的替代品。
- Studio 的 React 应用使用自己的 React root。DSH 插件界面保留官方 renderer 的 React root，通过 portal 挂到 Studio 容器，避免混用两套 React 的 hook dispatcher。
- Python 同源转发只处理传输与 Studio 本地会话校验，不创建第二套插件运行时。

## 界面入口与多个插件

`/studio-core/` 加载官方 Core 启动文档，但根界面为 Studio。DSH 自带的聊天主页、侧栏及通用设置不重复显示。

应用补丁停用原有 `ui-settings-general` 壳层，由 Studio 根贡献声明 `settings.section`，避免同一 slot 的双重归属。官方 settings 服务、settingsScope、RPC 和各社区设置页仍使用原实现。

“插件设置”先列出当前已激活插件提供的 `settings.section`。用户明确选择后才挂载该设置页，即使只有一个插件也不默认进入。多个贡献按官方 slot 标识区分；切换卸载上一页的显示树，但不会停用它的后台服务。回到设置列表卸载当前显示树。贡献移除时不自动改选另一个插件。

具体插件详情中的“打开插件设置”已经表达了选择，因此直接进入该插件的唯一设置页；若该插件有多个设置页，只展示其页面列表。导航将官方 slot registrant 与 Core 启动模块的客户端导出 name 精确匹配，取得模块包 ID；不假设注册名等于 npm 包名，不硬编码 IM。重名或未能归属的页面仍可从全局页面列表打开，不会被错误归入某个插件。该元数据只用于显示导航，不用于授权。插件没有活动设置页时显示空状态，绝不替换为其他插件。首次从普通 Studio 文档进入 Core 文档时通过 URL 保留此选择。

安装、启用、停用与卸载仍走既有 Profile 管理 API。涉及 DSH 的成功变更使 Core generation 失效，处于 Core 文档中的 Studio 随后重新加载，获取新插件注册表和浏览器会话。无需为每个插件创建 Core 或单独配置模型。

DSH 安装可输入裸 npm 包名（如 `@xmanrui/dsh-im`）或精确版本。裸包名先通过 npm 查询 `latest` 的实际版本，再将精确坐标交给 DSH 安装并写入 Profile/锁文件；查询失败不修改 Profile，不把可变标签交给安装命令。显式 Git/URL、版本范围及其他标签仍不受支持。详情简介与长描述相同时只显示一份。

当前接入的是官方设置页面扩展点。只提供工具的插件不应出现空白设置页。侧栏、编辑器或特定 DSH 壳 DOM 等其他扩展点，不因为已经安装就自动等价为一个设置页；支持新扩展点应通过官方 slot 合约明确接入，不能靠隐藏原壳或伪造 service 实现。

## 模型与凭证

快速创建从已配置模型中优先绑定 `deepseek-v4.1-flash`（默认）与 `glm-5.3-flash`，不会生成未配置的模型引用。默认执行限制为 100 步、600 秒；编辑已有 Agent 时保留其明确保存的限制。

`dsh_models.py` 从 Studio 的 CLI 默认模型及模型资源目录生成官方 `llm-pi-ai` 路由。默认模型通过 `agent-default-model` 指向 Studio 路由。

- 模型协议、地址、模型名与 credential reference 使用 Studio 解析器的结果。
- 同一地址、协议及 credential reference 的模型共享一个 provider；不同凭证不会被混用。
- 配置补丁只包含 provider 元数据和环境变量引用。
- 解析后的密钥仅传入受管理的 Core 进程环境，不写入 DSH 持久配置，不返回浏览器，也不进入 dataclass repr。
- 无法解析凭证的资源被略过，不阻止其他已配置模型或插件启动。
- 目前在 Core 启动时生成模型投影；运行中修改模型配置需要重新启动 Core 才生效。

这解决插件重复填写模型 API key 的问题。DSH 插件使用 Core 原生会话服务；本次未将其历史记录迁入 Studio 既有 Agent 会话库，不能据此宣称两者会话已合并。真实 IM 收发还需要用户连接渠道账号，并单独验证。

## 同源传输与权限

Agent Teams 默认在 Studio 启动后异步启用，`KSADK_STUDIO_TEAMS_DEFAULT=0` 可关闭这次自动激活。后台激活与页面重试共用生命周期锁。页面持续读取实际状态，区分准备中、可用及激活失败；启用不等于创建团队或开始执行任务。

同一工作区的团队数据库只能由一个 Studio 宿主持有。另一个实例占用时保留其数据与独占锁，返回 `authority_in_use`；页面提示关闭该实例后重试，不自动删除锁文件或重建数据库。

Studio 退出时，HTTP 长连接最多等待 10 秒，然后进入运行时清理；已打开的 SSE 观察连接不应无限阻止团队锁释放。会话等待用户填写表单时仍保持原响应流，收到实际执行终态后才关闭。`CancelRun` 同时接受客户端 Invocation ID 和 `GetSession.ActiveInvocationId` 返回的运行 ID，并用实际运行 ID 检查团队占用权限。

`dsh_application.py` 把官方资源、HTTP RPC/事件流与 WebSocket 转发到同一个 Core。Studio 已注册 API 和静态路由优先，未知 Studio API 不落入 Core。

所有转发要求有效 Studio cookie；写请求还要求当前页面的精确 Origin，其他本机端口不视为同源。Core 自己的 cookie 和协议鉴权继续有效。启动握手中的进程 token 不进入访问日志，浏览器只接收 HttpOnly cookie。网络请求不使用系统代理，也不接受调用方指定的 upstream 地址。

社区插件在当前用户权限下运行，原生界面与进程共用宿主信任边界；这不是第三方代码沙箱。官方 Codex 市场插件按用户要求点击即可安装；其他 Codex 来源与 DSH 包安装保留来源确认。后端安装风险确认字段保留，官方插件的显式点击由 UI 传递为确认，不自动安装插件。

## 插件市场展示

Codex App Server 提供的展示元数据投影到 Studio：名称、简介、详细介绍、使用示例、开发者、类别、功能和公开链接。详情按需加载实际能力，安装状态仍以宿主返回结果为准。

远程图片使用宿主提供的 HTTPS 地址；本地市场图片只读取宿主声明的插件根目录内有大小限制的图像文件，处理真实路径与符号链接边界，不向浏览器公开绝对路径。缺失或损坏图片显示名称缩写，不编造官方 logo。

## 验收要求

1. 浏览器只有一套 Studio 导航，无嵌套 DSH iframe 或聊天主页。
2. 真实外部 `dsh-im` 的设置页在 Studio 内渲染；此证据不等于全部社区插件或真实 IM 收发已验证。
3. 插件设置初始不选中任何插件，切换和移除贡献都释放旧显示树。
4. 官方 Codex 插件直接安装，第三方来源确认仍有效，详情使用真实元数据。
5. Core 模型目录显示 Studio provider 与默认模型；配置补丁不含明文凭证。
6. 未认证访问、跨来源写操作、未知 Studio API 均不能穿透转发边界。
7. Profile 变更后重新建立 Core 浏览器会话；SDK 原有插件 CLI 和能力生命周期测试继续通过。

以上属于本地集成验收。发布仍需完整候选检查、平台兼容测试与公开发布流程。
