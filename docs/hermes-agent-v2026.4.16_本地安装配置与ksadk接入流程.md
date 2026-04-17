# Hermes Agent v2026.4.16 本地安装、配置与 KsADK 接入流程

本文档沉淀当前这一期 Hermes 接入 AgentEngine / KsADK 的可复用流程。

目标形态：

- `agentengine hermes deploy` 直接部署 Hermes 公共 runtime 镜像
- `agentengine invoke <hermes-agent>` 默认进入 pod 内 Hermes 原生远程 TUI
- `agentengine hermes open <hermes-agent> --chat` 打开统一 hosted chat
- `agentengine hermes exec <hermes-agent> -- <subcommand...>` 透传受限只读运维子命令
- Hermes 管理 WebUI 与 hosted chat 都继续通过同一个 agent 域名访问

## 1. 本期约束

- 本期不把 Hermes 接入 `agentengine-dashboard`
- 本期不让 `agentengine hermes deploy` 走本地 build/push
- 公共 Hermes 镜像由 `ksadk-python` 仓库维护者统一构建并推送到公共仓库
- 业务项目侧默认只消费公共镜像，必要时再通过 `--image` 覆盖

## 2. 本地准备

### 2.1 模型环境变量

Hermes runtime 通过 OpenAI-compatible 环境变量接线：

```bash
export OPENAI_API_KEY=4fd210b0-eee5-4c64-a23c-dc7fb3f86717
export OPENAI_BASE_URL=http://kspmas.ksyun.com/v1
export OPENAI_MODEL_NAME=glm-5.1
```

也可以写进项目 `.env`：

```dotenv
OPENAI_API_KEY=4fd210b0-eee5-4c64-a23c-dc7fb3f86717
OPENAI_BASE_URL=http://kspmas.ksyun.com/v1
OPENAI_MODEL_NAME=glm-5.1
KSYUN_ACCESS_KEY=...
KSYUN_SECRET_KEY=...
KSYUN_ACCOUNT_ID=...
KSYUN_REGION=cn-beijing-6
```

### 2.2 云上部署所需环境

KsADK 侧至少需要：

- `KSYUN_ACCESS_KEY`
- `KSYUN_SECRET_KEY`
- `KSYUN_ACCOUNT_ID`
- `KSYUN_REGION`

如果要部署到预发控制面，还需要对应的预发权限和 kubeconfig。

## 3. 公共 Hermes 镜像发布流程

这部分在 `ksadk-python` 仓库根目录执行，不在业务项目目录执行。

Hermes runtime 资产位于：

- `deploy/hermes/Dockerfile`
- `deploy/hermes/entrypoint.sh`
- `deploy/hermes/runtime/app.py`

当前 Makefile 已提供与 OpenClaw 并列的发布入口：

```bash
cd /Users/xiayu/kingsoft/code/agent-sdk/ksadk-python

make hermes-build
make hermes-push HERMES_TAG=2026.4.16
make hermes-size
```

如需显式切换 Hermes 上游 release ref：

```bash
make hermes-build HERMES_AGENT_REF=v2026.4.16
```

默认发布地址：

```text
hub.kce.ksyun.com/agentengine-public/hermes-agent:2026.4.16
hub-vpc-cn-beijing-6.kce.ksyun.com/agentengine-public/hermes-agent:2026.4.16
```

说明：

- 外网镜像给控制面和通用消费侧使用
- VPC 镜像给集群内拉取使用
- `agentengine hermes deploy` 默认就会使用这个公共镜像

## 4. 一键创建 Hermes 项目

在业务目录执行：

```bash
agentengine init -f hermes demo-hermes
cd demo-hermes
```

生成结果是 container-first 模板，包含：

- `.env`
- `.env.example`
- `agentengine.yaml`
- `Dockerfile`
- `entrypoint.sh`
- `runtime/app.py`
- `README.md`

说明：

- 这个模板会把 Hermes runtime 的参考实现复制到项目里，便于后续定制
- 但默认部署仍然优先走公共 Hermes 镜像，不要求本地先构建镜像

## 5. 运行时 contract

Hermes runtime 对外暴露一个统一端口，聚合四类入口：

- `/`：Hermes dashboard 管理 WebUI
- `/chat`：由平台 router 转到 hosted chat
- `/v1/*`：Hermes OpenAI-compatible API
- `/_ksadk/terminal/ws`：原生远程 TUI、`hermes connect`、受限 `hermes exec` 和 `hermes pairing`

`deploy/hermes/runtime/app.py` 当前额外负责：

- `/health` 同时检查 Hermes API upstream 和 dashboard upstream
- 只允许 `ks-terminal.v1` 子协议建立终端 websocket
- 在服务端二次校验 `exec` / `pairing` 白名单，防止客户端绕过本地校验

## 6. 本地容器运行模型

容器入口是 `deploy/hermes/entrypoint.sh`，启动流程固定为：

1. 写入 `~/.hermes/.env`
2. 写入 `~/.hermes/config.yaml`
3. 启动并监督 `hermes gateway run --replace`
4. 启动 `hermes dashboard --host 127.0.0.1 --port 9119 --no-open`
5. 启动 wrapper ASGI 服务，对外统一暴露一个端口

这样做的好处：

- 不需要等 Hermes 上游提供远程原生 attach 协议
- 平台可以稳定约束 `/v1/*`、WebUI 和终端协议
- 容器健康探针不会只检查 wrapper 自己，而会探测 Hermes API/dashboard upstream
- gateway 退出时优先在容器内自拉起，超过本地重启预算后再让 Kubernetes 重建 Pod
- 默认补齐 `TERM=xterm-256color`，远端 `hermes gateway setup` 尽量保持上下键交互

## 7. 部署到云上

业务项目目录下直接执行：

```bash
agentengine hermes deploy --name demo-hermes
```

如果需要显式指定镜像：

```bash
agentengine hermes deploy \
  --name demo-hermes \
  --image hub.kce.ksyun.com/agentengine-public/hermes-agent:2026.4.16
```

部署命令当前行为：

- 读取当前目录 `.env`
- 自动把 `OPENAI_*` 变量翻译成 Hermes runtime 所需 env vars
- 如果 `OPENAI_BASE_URL` 是 `http://kspmas.ksyun.com/v1`，会自动改写成 `http://kspmas-internal.sdns.ksyun.com/v1`
- 通过 `CreateAgentProduct` 或 `UpdateAgent` 部署 `framework=hermes`
- 保存 `.agentengine.state`

## 8. 生命周期命令

当前 Hermes 已有一等公民 CLI 面：

```bash
agentengine hermes deploy
agentengine hermes list
agentengine hermes status <agent>
agentengine hermes open <agent>
agentengine hermes open <agent> --chat
agentengine hermes connect <agent>
agentengine hermes exec <agent> -- status
agentengine hermes pairing <agent> -- list
agentengine hermes delete <agent> -y
```

补充说明：

- `agentengine invoke <agent>`：默认进入 Hermes 原生远程 TUI
- `agentengine hermes open <agent> --chat`：打开 hosted chat
- `agentengine hermes connect <agent>`：进入远端 gateway setup 向导，完成 Feishu / Weixin 扫码配置
- `agentengine invoke <agent> -m "hello"`：继续走 `/v1/chat/completions`
- `agentengine hermes pairing <agent> -- approve feishu <code>`：透传 pairing 审批子命令
- `agentengine hermes destroy`：保留为 hidden 兼容别名

## 9. `hermes exec` 白名单

`agentengine hermes exec` 不是远程 shell，只允许只读运维子命令。

当前允许：

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

当前明确拒绝：

- `setup`
- `auth`
- `update`
- `install`
- `uninstall`
- `gateway start|stop|restart`
- `cron add|remove`
- `pairing`
- 任意 launcher
- 任意 shell metacharacters
- 任意前导 `-` 选项

## 10. 预发部署与 5 组 E2E

### 10.1 先发布 server

在 `agentengine-server` worktree 中：

```bash
make check-env
make docker-login
make docker-build VERSION=<tag>
make docker-push VERSION=<tag>
make deploy ENV=pre VERSION=<tag> NAMESPACE=agentengine
make status ENV=pre NAMESPACE=agentengine
```

默认依赖：

- `~/.kube/agentengine-pre`
- `deploy/helm/agentengine/values-pre.yaml`
- `hub.kce.ksyun.com` 访问权限

### 10.2 再发布 Hermes runtime 公共镜像

在 `ksadk-python` worktree 中：

```bash
make hermes-push HERMES_TAG=2026.4.16
```

### 10.3 再执行 CLI 侧预发 E2E

```bash
agentengine hermes deploy --name demo-hermes --image hub.kce.ksyun.com/agentengine-public/hermes-agent:2026.4.16
agentengine hermes status demo-hermes
agentengine invoke demo-hermes
agentengine hermes open demo-hermes --chat
agentengine invoke demo-hermes -m "hello"
agentengine hermes exec demo-hermes -- status
agentengine hermes exec demo-hermes -- doctor
agentengine hermes delete demo-hermes -y
```

本期要求固定核对这 5 组：

1. `agentengine invoke <agent>` 进入原生 TUI
2. `agentengine hermes open <agent> --chat` 打开 hosted chat
3. `agentengine invoke <agent> -m "hello"` 单次消息成功
4. `agentengine hermes exec <agent> -- status` 成功
5. `agentengine hermes exec <agent> -- doctor` 成功

## 11. 常见排查点

- `agentengine invoke` 没进原生 TUI
  - 检查 agent detail 的 `framework` 是否为 `hermes`
  - 检查 endpoint 下 `/_ksadk/terminal/ws` 是否可达
  - 检查 websocket 子协议是否为 `ks-terminal.v1`

- WebUI 打不开
  - 检查 `agentengine hermes open <agent>`
  - 检查 runtime `/` 是否成功反代到 Hermes dashboard
  - 检查 `/chat` 是否仍由平台 router 接管

- `hermes exec` 被拒绝
  - 先确认是否命中了本地白名单
  - 再确认是否命中了 runtime 侧二次校验

- 部署后健康检查不通过
  - 看 `/health` 返回的是 API 失败还是 dashboard 失败
  - 进入 pod 检查 `hermes gateway run` / `hermes dashboard` 是否启动成功

## 12. 与后续阶段的边界

本期已经做的：

- Hermes framework allowlist / schema 接入
- 原生远程 TUI
- 受限 `hermes exec`
- Hermes 管理 WebUI + hosted chat 的同域路由约定
- Hermes 专属 deploy / list / status / open / delete CLI
- 公共镜像构建发布入口

本期不做的：

- Hermes 专属 dashboard 管理台改造
- 任意 shell 透传
- `agentengine hermes deploy` 本地 build/push
- 原生 TUI 断线重连与多端广播
- 移除历史本地 Web UI 适配层与依赖
