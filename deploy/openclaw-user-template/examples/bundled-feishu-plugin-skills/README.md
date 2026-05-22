# 内置飞书 plugin + skills 极简示例

这个目录是给用户打包参考用的“最简单例子”。

它适合这种场景：

- 用户不想自己写 plugin / skill 代码
- 只是想基于我们已经打好的 OpenClaw 公共镜像，再封一层自己的分发镜像
- 目标能力就是直接复用基础镜像内置的飞书 plugin 和相关 skills

## 这份示例到底做了什么

它实际上只做了两件事：

- `Dockerfile` 直接 `FROM ${OPENCLAW_BASE_IMAGE}`，默认基础镜像是 `ghcr.io/openclaw/openclaw:2026.5.20-slim@sha256:db199be23add581ef18ca8c8a866af84db13586d5bfcd566c8ac73d8d106eebb`
- 不覆写 `ENTRYPOINT` / `CMD`，继续使用官方 `bootstrap.sh` 启动链路

也就是说：

- 运行行为和官方基础镜像一致
- 用户只是多得到一个“可以继续分发、继续二次封装”的自定义镜像入口
- 不需要额外编写任何 JS / TS plugin 代码
- 不需要额外编写任何自定义 skill

## 基础镜像里已经有什么

基础镜像里已经自带下面这些内容：

- plugin: `openclaw-lark`
- channel: `feishu`
- skills 目录：`/opt/openclaw/default-extensions/openclaw-lark/skills/`

当前镜像里能直接看到这些飞书相关 skills：

- `feishu-bitable`
- `feishu-calendar`
- `feishu-channel-rules`
- `feishu-create-doc`
- `feishu-fetch-doc`
- `feishu-im-read`
- `feishu-task`
- `feishu-troubleshoot`
- `feishu-update-doc`

所以这份示例的重点不是“教你怎么写飞书 plugin”，而是明确告诉用户：

- 如果你只是要内置飞书能力，基础镜像本身就已经带好了
- 你只需要基于它继续打镜像，不需要重复造一份飞书 plugin / skills

不要把这个示例的默认基础镜像随意改成上游 `ghcr.io/openclaw/openclaw:*`，除非你已经确认该镜像也内置了 `openclaw-lark` 和相关 skills；否则飞书 channel 配置会写进去，但运行时没有对应 plugin 可用。

## 直接构建

```bash
make build \
  IMAGE=hub-vpc-cn-beijing-6.kce.ksyun.com/your-namespace/openclaw-feishu-bundled \
  TAG=demo
```

或者：

```bash
docker build -t openclaw-feishu-bundled .
```

## 直接运行

如果只是本地起一个页面验证镜像能启动，建议临时改成不鉴权：

```bash
docker run --rm -it -p 8080:8080 \
  -e OPENCLAW_GATEWAY_AUTH_MODE=none \
  openclaw-feishu-bundled
```

平台接入时仍然建议使用：

- `trusted-proxy`
- 或显式 `token`

如果使用 token 模式，需要同时传：

- `OPENCLAW_GATEWAY_AUTH_MODE=token`
- `OPENCLAW_GATEWAY_TOKEN=<shared-secret>`

平台短链/Dashboard 场景下，浏览器不直接持有 token；Router 会在服务端向 upstream runtime 注入 `Authorization: Bearer <OPENCLAW_GATEWAY_TOKEN>`。

## 如何通过 env 预配置飞书 channel

官方 `bootstrap.sh` 已经支持通过：

```text
OPENCLAW_CHANNEL_BOOTSTRAP_JSON
```

来做 channel 预配置。

如果你在运行时传：

```bash
docker run --rm -it -p 8080:8080 \
  -e OPENCLAW_GATEWAY_AUTH_MODE=none \
  -e OPENCLAW_CHANNEL_BOOTSTRAP_JSON='{"feishu":{"appId":"cli-app-id","appSecret":"cli-app-secret","domain":"lark"}}' \
  openclaw-feishu-bundled
```

那么官方 bootstrap 会自动做这些事：

- 自动启用 `openclaw-lark`
- 自动生成 `channels.feishu`
- 自动补默认值：`enabled=true`
- 自动补默认值：`connectionMode=websocket`
- 自动补默认值：`requireMention=true`
- 自动补默认值：`dmPolicy=pairing`
- 自动补默认值：`groupPolicy=open`

这也是这份示例推荐的方式，因为它不需要你自己在 Dockerfile 里再硬编码飞书配置文件。

## 平台部署时怎么传

如果你走 `agentengine openclaw deploy`，可以直接这样传：

```bash
agentengine openclaw deploy \
  --image hub-vpc-cn-beijing-6.kce.ksyun.com/your-namespace/openclaw-feishu-bundled:demo \
  --env OPENCLAW_CHANNEL_BOOTSTRAP_JSON='{"feishu":{"appId":"cli-app-id","appSecret":"cli-app-secret","domain":"lark"}}'
```

token 模式可以追加：

```bash
agentengine openclaw deploy \
  --image hub-vpc-cn-beijing-6.kce.ksyun.com/your-namespace/openclaw-feishu-bundled:demo \
  --env OPENCLAW_GATEWAY_AUTH_MODE=token \
  --env OPENCLAW_GATEWAY_TOKEN=gateway-token-demo \
  --env OPENCLAW_CHANNEL_BOOTSTRAP_JSON='{"feishu":{"appId":"cli-app-id","appSecret":"cli-app-secret","domain":"lark"}}'
```

这里要特别注意：

- 这是“运行时注入到容器里的环境变量”
- 不是只在宿主机里 `export OPENCLAW_CHANNEL_BOOTSTRAP_JSON=...` 就算完成

也就是说：

- 本地跑 Docker，要用 `docker run -e`
- 平台部署，要用 `agentengine openclaw deploy --env`

## 什么时候该看进阶示例

如果你后面不满足于“只复用基础镜像里已内置的飞书能力”，而是还要：

- 自己写一个 plugin
- 自己带一个 skill
- 再给这个 plugin 安装自定义 npm 依赖

那就看这个目录：

```text
../minimal-skill-plugin-deps/
```
