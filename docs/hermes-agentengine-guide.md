# 从 0 到跑通 Hermes：AgentEngine 爱马仕 Agent 实战手册

适用版本：`ksadk / agentengine CLI 0.5.1`

这篇文档专门写给第一次接触 Hermes 的同学。目标很明确：不用先研究一堆内部实现，只要按步骤执行，就能把一个 Hermes Agent 初始化、部署、连上远端 TUI、打开 UI、接上微信/飞书，并完成远端审批。

如果你只想先记住一句话，可以先记这一句：

> Hermes 和普通代码型 Agent 不一样，`agentengine hermes deploy` 默认直接使用平台预置的 Hermes runtime 镜像，不要求你先本地 `build` / `push`。

---

## 1. 先搞清楚：Hermes + AgentEngine 到底是什么关系

把它理解成下面这套分工就行：

- `Hermes` 负责 Agent 本身的原生能力，例如 dashboard、gateway、原生 TUI、pairing 审批。
- `AgentEngine CLI` 负责把 Hermes 变成一个“可初始化、可部署、可连接、可分享”的标准化云端 Agent。
- 你平时主要用的是 `agentengine` 命令，不需要直接手搓一堆运行时参数。

在 `0.5.1` 里，Hermes 这条链路已经是一等公民，核心命令包括：

```bash
agentengine hermes deploy
agentengine hermes list
agentengine hermes status
agentengine hermes open
agentengine hermes connect
agentengine hermes exec
agentengine hermes pairing
agentengine invoke
```

其中最关键的三个认知是：

1. `agentengine invoke` 在 Hermes 场景下，默认进入的是 **Hermes 原生远程 TUI**。
2. `agentengine hermes open` 默认打开的是 **Hermes 管理 UI**，也就是 `/`。
3. `agentengine hermes open --chat` 打开的则是 **AgentEngine hosted chat UI**，也就是 `/chat`。

---

## 2. 0.5.1 这次你最应该知道的更新

结合当前代码和文档，和 Hermes 最相关的更新主要有这几项：

- 默认 Hermes 共享 runtime 镜像已经更新到 `hub.kce.ksyun.com/agentengine-public/hermes-agent:2026.4.23`
- Hermes 默认模型切到 `glm-5.1`
- 当默认模型是 `glm-5.1` 时，CLI 会自动补齐 `HERMES_CONTEXT_LENGTH=200000`
- 当默认模型是 `glm-5.1` 时，fallback model 默认会补成 `kimi-k2.6`
- `agentengine hermes connect` 现在是托管场景的一等入口，用来在远端容器里完成微信 / 飞书等 IM 连接
- `agentengine hermes exec` 是受限只读运维入口，不是远程 shell
- `agentengine hermes pairing` 可以把 Hermes 原生 pairing 审批能力透传出来

这意味着：现在写 Hermes 使用文档时，主线应该围绕“初始化 -> 部署 -> 状态检查 -> 远端 TUI -> UI -> IM -> 审批”来讲，而不是先讲本地构建镜像。

---

## 3. 一屏看懂：最快跑通路径

如果你想先用最短路径跑通一遍，可以先照下面执行：

```bash
pip install -U ksadk

agentengine init -f hermes my-hermes-demo
cd my-hermes-demo

vim .env
agentengine hermes deploy
agentengine hermes status
agentengine invoke
agentengine hermes open --chat
agentengine hermes connect
```

如果你已经在项目目录里完成过一次部署，后续很多命令都可以不再写 Agent 名称，因为 CLI 会优先读取当前目录下的 `.agentengine.state`。

---

## 4. 第一步：安装 CLI

安装命令很简单：

```bash
pip install -U ksadk
```

安装完成后，下面两个命令入口是等价的：

```bash
agentengine --help
ksadk --help
```

建议你直接统一使用 `agentengine`，因为它更符合对外文档和团队传播习惯。

---

## 5. 第二步：准备配置

Hermes 这条链路至少会涉及两类配置：

- 金山云部署配置
- 模型调用配置

### 5.1 推荐方式：写进项目 `.env`

初始化项目后，最推荐把配置写进项目根目录的 `.env`，因为这样最容易复现，也最适合发给团队成员照着操作。

一个最小可用示例可以写成这样：

```dotenv
# ======================
# 金山云部署配置
# ======================
KSYUN_ACCESS_KEY=your-ak
KSYUN_SECRET_KEY=your-sk
KSYUN_ACCOUNT_ID=your-account-id
KSYUN_REGION=cn-beijing-6

# ======================
# 模型配置
# ======================
OPENAI_API_KEY=your-model-key
OPENAI_BASE_URL=http://kspmas.ksyun.com/v1
OPENAI_MODEL_NAME=glm-5.1

# ======================
# Hermes runtime
# ======================
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
HERMES_DASHBOARD_HOST=127.0.0.1
HERMES_DASHBOARD_PORT=9119
PORT=8080

# 可选覆盖项
# HERMES_CONTEXT_LENGTH=200000
# HERMES_FALLBACK_MODEL=kimi-k2.6
# HERMES_UI_LOCALE=en
# HERMES_IMAGE=hub.kce.ksyun.com/agentengine-public/hermes-agent:2026.4.23
```

### 5.2 第一次使用也可以先走全局配置

如果你更习惯先把全局配置准备好，再初始化项目，也可以用：

```bash
agentengine config
```

或者直接非交互设置：

```bash
agentengine config set region=cn-beijing-6 OPENAI_MODEL_NAME=glm-5.1
agentengine config show
```

### 5.3 关于 `OPENAI_BASE_URL` 的一个贴心默认

如果你把模型地址写成：

```bash
http://kspmas.ksyun.com/v1
```

那么在云端部署时，CLI 会自动把它改写成运行时可访问的内部地址：

```bash
http://kspmas-internal.sdns.ksyun.com/v1
```

这一步不需要你手动处理，目的是避免云端 Pod 去访问公网模型网关时出现超时或不稳定。

---

## 6. 第三步：初始化一个 Hermes 项目

执行：

```bash
agentengine init -f hermes my-hermes-demo
cd my-hermes-demo
```

初始化完成后，你通常会看到这些核心文件：

- `.env`
- `.env.example`
- `agentengine.yaml`
- `Dockerfile`
- `entrypoint.sh`
- `runtime/app.py`
- `README.md`

可以这样理解这些文件的作用：

- `.env`：你最常改的文件，主要放 AK/SK、模型地址、模型名
- `agentengine.yaml`：项目元信息和部署配置
- `Dockerfile` / `entrypoint.sh` / `runtime/app.py`：Hermes runtime 的参考实现，方便后续做定制镜像
- `.agentengine.state`：部署完成后 CLI 会自动写入，后续命令可据此自动识别当前 Agent

这里有一个非常重要的点：

> `agentengine init -f hermes` 会把 Hermes runtime 参考资产复制到项目里，但默认部署仍然优先走平台共享镜像，不要求你本地先构建镜像。

---

## 7. 第四步：部署 Hermes Agent

在项目目录里直接执行：

```bash
agentengine hermes deploy
```

如果你想显式指定名称，也可以写成：

```bash
agentengine hermes deploy --name my-hermes-demo
```

如果你想覆盖默认镜像，可以这样：

```bash
agentengine hermes deploy --image hub.kce.ksyun.com/agentengine-public/hermes-agent:2026.4.23
```

### 7.1 这条命令背后做了什么

当前 CLI 实现里，`agentengine hermes deploy` 会做这些事：

1. 读取当前目录 `.env`
2. 读取 `OPENAI_*` 配置并翻译成 Hermes runtime 所需环境变量
3. 在未显式指定镜像时，优先使用服务端默认镜像，否则回退到 CLI 默认镜像
4. 以 `framework=hermes`、`artifact_type=Container` 的形式调用控制面创建或更新 Agent
5. 默认写入 `ui_config.profile=hermes`，并把 UI 根路径设为 `/`
6. 将部署结果保存到 `.agentengine.state`

### 7.2 当前默认值

结合 `cmd_hermes.py`，当前常见默认值如下：

- 默认镜像：`hub.kce.ksyun.com/agentengine-public/hermes-agent:2026.4.23`
- 默认模型：`glm-5.1`
- 默认 CPU：`2`
- 默认内存：`4Gi`
- 默认最小副本数：`1`
- 默认最大副本数：`1`
- 默认并发：`1000`

### 7.3 `glm-5.1` 的自动增强

如果你的默认模型是 `glm-5.1`，CLI 会自动帮你补齐这些运行时行为：

- `HERMES_CONTEXT_LENGTH=200000`
- `HERMES_FALLBACK_MODEL=kimi-k2.6`
- `HERMES_FALLBACK_PROVIDER=custom`

这也是为什么你在 0.5.1 的 Hermes 文档里，应该明确告诉用户“默认模型已经切到 `glm-5.1`”。

---

## 8. 第五步：检查部署状态

部署之后，先看状态：

```bash
agentengine hermes status
```

如果你不在项目目录里，或者要查别的实例，可以显式写 Agent 名称或 ID：

```bash
agentengine hermes status my-hermes-demo
agentengine hermes status ar-xxxx
```

要列出当前区域下全部 Hermes 实例：

```bash
agentengine hermes list
```

一个很实用的判断方式是：

- 想看“当前这个项目对应的 Hermes 实例”时，用 `status`
- 想找“我这个区域里到底有哪些 Hermes”时，用 `list`

---

## 9. 第六步：进入 Hermes 原生远程 TUI

这是 Hermes 最有辨识度的一步。

执行：

```bash
agentengine invoke
```

如果你在项目目录外，也可以显式指定：

```bash
agentengine invoke my-hermes-demo
```

### 9.1 这里为什么不用 `agentengine hermes invoke`

因为当前 CLI 设计里，Hermes 的交互式终端复用了统一入口 `agentengine invoke`。  
只要 CLI 判断目标是 Hermes，它就会默认切换到 **Hermes 原生远程 TUI**，而不是 ksadk 的通用 chat TUI。

### 9.2 什么时候不用 TUI，而用单次消息调用

如果你只是想打一条消息试试通不通，可以用：

```bash
agentengine invoke -m "你好，先介绍一下你自己"
```

这时它会走单次调用，而不是进入交互终端。

### 9.3 一个重要提醒

Hermes 不再支持 ksadk 通用 chat TUI 这条路径。  
如果你想要浏览器里的聊天体验，请直接用：

```bash
agentengine hermes open --chat
```

---

## 10. 第七步：打开管理 UI 和聊天 UI

Hermes 这条线里，经常有人搞混 `/` 和 `/chat`。你可以这样记：

- `/` 是 **Hermes 管理 UI**
- `/chat` 是 **AgentEngine hosted chat UI**

### 10.1 打开 Hermes 管理 UI

在项目目录里直接执行：

```bash
agentengine hermes open
```

或者显式写成：

```bash
agentengine hermes open --manage
```

### 10.2 打开聊天 UI

```bash
agentengine hermes open --chat
```

### 10.3 生成可分享链接

如果你希望把链接分享给别人，而不是自动打开浏览器，可以用你在群里通知里那组命令：

对话 UI：

```bash
agentengine dashboard open --path /chat --share --expires-seconds 0 --no-open
```

管理 UI：

```bash
agentengine dashboard open --path / --share --expires-seconds 0 --no-open
```

如果你更希望统一从 Hermes 命令组出发，也可以写：

```bash
agentengine hermes open --chat --share --expires-seconds 0 --no-open
agentengine hermes open --share --expires-seconds 0 --no-open
```

### 10.4 什么时候用哪一个

可以按这个口诀判断：

- 想聊天、演示、给业务同学体验：用 `/chat`
- 想看 Hermes 自身管理能力：用 `/`
- 想发链接给别人：加 `--share --expires-seconds 0 --no-open`

---

## 11. 第八步：连接微信、飞书等 IM

如果你要把 Hermes 接到 IM 渠道，执行：

```bash
agentengine hermes connect
```

如果不在项目目录里：

```bash
agentengine hermes connect my-hermes-demo
```

### 11.1 这条命令本质上做什么

它会进入远端容器内的 `hermes gateway setup` 流程，让你在托管实例内部完成扫码连接和配置，而不是要求你在本地机器上安装一个常驻 daemon。

这也是当前托管 Hermes 的一个核心设计点：

- gateway 由容器自动托管
- `agentengine hermes connect` 负责远端配置
- 不依赖 `systemd`
- 不依赖 `launchd`
- 不要求你去手动保活 gateway

---

## 12. 第九步：做远端审批 Pairing

如果接入了飞书、微信等 IM，经常会遇到审批或授权配对，这时用：

```bash
agentengine hermes pairing -- list
```

例如审批一条飞书 pairing 请求：

```bash
agentengine hermes pairing -- approve feishu RHL5XXXX
```

如果你在项目目录外，也可以写成：

```bash
agentengine hermes pairing my-hermes-demo -- list
agentengine hermes pairing my-hermes-demo -- approve feishu RHL5XXXX
```

这里的本质是：

- `pairing` 不是 AgentEngine 自己发明的新协议
- 它是把 Hermes 原生 pairing 子命令安全地透传了出来

---

## 13. 第十步：远端只读运维命令

如果你想做一些只读检查，而不是进入完整 TUI，可以用：

```bash
agentengine hermes exec -- status
agentengine hermes exec -- doctor
agentengine hermes exec -- gateway status
agentengine hermes exec -- sessions list
agentengine hermes exec -- config show
```

如果在项目目录外，可以把 Agent 名称放前面：

```bash
agentengine hermes exec my-hermes-demo -- status
```

### 13.1 这不是远程 shell

这一点一定要在文档里写清楚：

> `agentengine hermes exec` 只允许受限的只读子命令，不是一个能随便执行任意命令的远程 shell。

当前常见允许项包括：

- `status`
- `doctor`
- `version`
- `insights`
- `sessions list|show|export`
- `config show|check|path|env-path`
- `skills list|audit|check`
- `tools list`
- `cron list|status`
- `gateway status`

像下面这些思路，就不属于它的目标范围：

- 想远程执行任意 bash
- 想做安装、更新、重启
- 想直接跑带副作用的 gateway / pairing / launcher 操作

---

## 14. 第十一步：删除实例

如果这个 Hermes Agent 不再需要了，可以删除：

```bash
agentengine hermes delete my-hermes-demo -y
```

如果你就在项目目录里，也可以写：

```bash
agentengine hermes delete ar-xxxx -y
```

删除成功后，CLI 也会顺带清理当前目录里对应的状态记录。

---

## 15. 常见问题

### 15.1 为什么 Hermes 不要求先本地 `build` / `push`？

因为当前 Hermes 走的是共享 runtime 镜像模式，`agentengine hermes deploy` 直接部署平台预置镜像或你显式指定的自定义镜像，不走普通代码型 Agent 那套本地构建链路。

### 15.2 为什么我执行 `agentengine invoke` 进入的是远端 TUI？

因为 CLI 会根据目标 Agent 的 `framework=hermes` 自动切换到 Hermes 原生远程终端模式。这是 Hermes 的默认体验，不是异常。

### 15.3 为什么聊天页不是 `/`，而是 `/chat`？

因为 `/` 是 Hermes 自己的 dashboard 管理 UI；`/chat` 才是 AgentEngine hosted chat。两者是同一个 Agent 域名下的两个入口。

### 15.4 为什么我不写 Agent 名称也能执行 `status` / `open` / `connect`？

因为部署后 CLI 会把结果保存到 `.agentengine.state`，后续很多命令都能从当前目录自动解析目标 Agent。

### 15.5 如何切英文界面？

在 `.env` 或环境变量里设置：

```bash
HERMES_UI_LOCALE=en
```

默认情况下，托管 Hermes 首次打开会优先显示中文。

### 15.6 什么时候需要自定义镜像？

当你只是想快速托管一个 Hermes Agent 时，不需要。  
当你需要预装额外依赖、预置插件、技能或定制 runtime 行为时，再考虑：

```bash
agentengine hermes deploy --image <your-image>
```

---

## 16. 最后给团队同学的一张命令速查表

```bash
# 安装
pip install -U ksadk

# 初始化项目
agentengine init -f hermes my-hermes-demo
cd my-hermes-demo

# 配置
vim .env
agentengine config show

# 部署
agentengine hermes deploy

# 状态
agentengine hermes status
agentengine hermes list

# 原生远程 TUI
agentengine invoke

# 单次消息调用
agentengine invoke -m "你好"

# 打开 UI
agentengine hermes open
agentengine hermes open --chat

# 生成分享链接
agentengine dashboard open --path / --share --expires-seconds 0 --no-open
agentengine dashboard open --path /chat --share --expires-seconds 0 --no-open

# 连接 IM
agentengine hermes connect

# 审批 pairing
agentengine hermes pairing -- list
agentengine hermes pairing -- approve feishu RHL5XXXX

# 只读运维
agentengine hermes exec -- status

# 删除
agentengine hermes delete my-hermes-demo -y
```

---

## 17. 本文依据

这篇文档不是凭印象写的，关键命令和默认值来自下面这些一手材料：

- [`ksadk-python/ksadk/cli/cmd_hermes.py`](../ksadk/cli/cmd_hermes.py)
- [`ksadk-python/ksadk/cli/cmd_invoke.py`](../ksadk/cli/cmd_invoke.py)
- [`ksadk-python/docs/ksadk使用文档.md`](./ksadk使用文档.md)
- [`ksadk-python/deploy/hermes/README.md`](../deploy/hermes/README.md)
- [`ksadk-python/deploy/hermes/README.md.template`](../deploy/hermes/README.md.template)

如果你要把这篇文档发到群里或沉淀到项目仓库，建议保留“速查表”和“常见问题”两节，因为这是最容易帮新人少踩坑的部分。
