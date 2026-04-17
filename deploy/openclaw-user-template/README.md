# OpenClaw 用户自定义镜像模板

这是一个“极简直启版”模板，适合直接打包成 zip 发给用户。

这份模板只解决四件事：

- 用户不修改任何构建逻辑，直接 `docker build` 就能得到可运行镜像。
- 用户可以自由替换自己的插件、skills 和默认配置。
- 用户创建实例时额外传入的自定义环境变量，容器里可以直接读取到。
- 兼容我们的运行时约束：只持久化 `/home/node/.openclaw`，默认网关端口 `8080`。

## 运行时架构要求

目标运行时固定按下面这个架构准备：

- Linux x86-64
- `linux/amd64`

如果你在 Apple Silicon 或其他非 x86 机器上构建，也应该按 `linux/amd64` 构建镜像。

它故意不做这些事：

- 不提供独立 bootstrap 脚本
- 不提供 safe-bin
- 不做复杂运行时补丁
- 不做 env -> `openclaw.json` 自动映射
- 不做额外依赖安装逻辑

## 目录结构

```text
openclaw-user-template/
├── Dockerfile
├── Makefile
├── README.md
└── custom/
    ├── config/
    │   └── openclaw.json
    ├── extensions/
    └── skills/
```

## 为什么要有一小段启动初始化

我们的运行时真正会持久化的只有：

```text
/home/node/.openclaw
```

这意味着：

- 如果把默认配置直接 bake 到 `/home/node/.openclaw`，卷挂载后会被遮住
- 所以模板会先把默认内容放进镜像内的模板目录
- 容器启动时只做一次很小的“缺什么补什么”初始化，把默认内容同步到 `/home/node/.openclaw`

这个初始化逻辑只做下面几件事：

- 如果 `/home/node/.openclaw/openclaw.json` 不存在，就写入默认配置
- 如果 `/home/node/.openclaw/extensions/<name>` 不存在，就补默认插件
- 如果 `/home/node/.openclaw/skills/<name>` 不存在，就补默认 skills

也就是说：

- 已存在的持久化内容不会被覆盖
- pod 重启后会继续沿用 `/home/node/.openclaw` 里的用户状态
- 不会因为镜像升级把用户已经改过的 state 强行重置

## 网关鉴权限制

这份模板明确只允许两种模式：

- `trusted-proxy`
- `none`

不能使用：

- `token`

原因是我们的网关接入链路要求用户必须使用 `trusted-proxy` 或者不鉴权；如果切到 `token` 模式，平台网关链路会过不去。

因此 Dockerfile 的启动命令会在启动前显式校验：

- 如果 `OPENCLAW_GATEWAY_AUTH_MODE` 是 `trusted-proxy` 或 `none`，继续启动
- 如果是 `token` 或其他值，直接启动失败并输出错误

## 8080 端口兼容

这份模板默认按 `8080` 启动，并兼容这几个环境变量：

- `OPENCLAW_GATEWAY_PORT`
- `PORT`
- `OPENCLAW_GATEWAY_BIND`
- `OPENCLAW_GATEWAY_AUTH_MODE`

默认行为是：

- 端口：`8080`
- bind：`lan`
- auth：`trusted-proxy`

另外有一个 OpenClaw 原生限制要注意：

- 当 `auth=none` 时，`bind=lan` 会被拒绝启动

所以这份模板会自动做一个兼容收口：

- 如果 `OPENCLAW_GATEWAY_AUTH_MODE=none`
- 并且 `OPENCLAW_GATEWAY_BIND` 还是默认 `lan`
- 启动时会自动改成 `loopback`

所以：

- 本地直接 `docker run` 默认就是 `8080`
- 云端如果注入了 `OPENCLAW_GATEWAY_PORT=8080`，也会直接兼容

## 不修改任何文件，直接构建

在当前目录执行：

```bash
docker build -t openclaw-user-custom .
```

或者直接用模板自带的基础 Makefile：

```bash
make build IMAGE=hub-vpc-cn-beijing-6.kce.ksyun.com/your-namespace/openclaw-user-custom TAG=demo
```

## 不修改任何文件，直接运行

```bash
docker run --rm -it -p 8080:8080 openclaw-user-custom
```

如果你是在本地直接打开，不经过反向代理，建议临时改成不鉴权：

```bash
docker run --rm -it -p 8080:8080 \
  -e OPENCLAW_GATEWAY_AUTH_MODE=none \
  openclaw-user-custom
```

或者：

```bash
make run IMAGE=hub-vpc-cn-beijing-6.kce.ksyun.com/your-namespace/openclaw-user-custom TAG=demo
```

## 基础 Makefile

模板里自带了一个最小 `Makefile`，只保留三个命令：

- `make build`
- `make run`
- `make push`

默认行为：

- 默认平台：`linux/amd64`
- 默认基础镜像：`ghcr.io/openclaw/openclaw:2026.4.15@sha256:0e6bebecf4623216420851f5edd133a748335f45c3508b635f7c5c4bfbc6da7d`
- 默认运行端口：`8080`

例如推送镜像：

```bash
make push \
  IMAGE=hub-vpc-cn-beijing-6.kce.ksyun.com/your-namespace/openclaw-user-custom \
  TAG=demo
```

## custom/ 目录怎么用

### 1. 自定义插件

把你的插件目录放到：

```text
custom/extensions/<plugin-id>/
```

### 2. 自定义 skills

把你的 skill 目录放到：

```text
custom/skills/<skill-name>/
```

### 3. 自定义默认配置

编辑：

```text
custom/config/openclaw.json
```

默认内容现在是：

```json
{
  "gateway": {
    "auth": {
      "mode": "trusted-proxy",
      "trustedProxy": {
        "userHeader": "x-forwarded-user"
      }
    },
    "trustedProxies": [
      "127.0.0.1",
      "::1",
      "10.0.0.0/8",
      "172.16.0.0/12",
      "192.168.0.0/16",
      "35.0.0.0/8"
    ]
  }
}
```

这份默认配置不是随便填的，它的作用是：

- 兼容当前 OpenClaw 新版本在 `trusted-proxy` 模式下对 `gateway.auth.trustedProxy` 的要求
- 兼容我们的运行时默认通过可信代理接入

你可以把希望预置的 `openclaw.json` 直接写进去，但如果你的运行时仍然走 `trusted-proxy`，建议保留这段最小配置，或者至少保留等价字段。

## 自定义环境变量怎么理解

这份模板支持“读取用户自定义环境变量”，但这里的支持是下面这个意思：

- 用户在创建实例时传入的自定义环境变量，会原样出现在容器环境里
- 你的插件、skills 或业务代码，可以直接读取这些环境变量

例如在 Node.js 插件里：

```js
const mode = process.env.APP_MODE;
const apiBase = process.env.API_BASE;
```

但要特别注意：

- 自定义环境变量不会自动写进 openclaw.json
- 这些自定义环境变量不会自动写进 `openclaw.json`
- 也不会自动改写 channel 配置、模型配置或其他 OpenClaw 配置项

如果你希望 `APP_MODE=prod` 自动变成某段配置文件内容，那需要你自己的代码去读取 env 后处理。

## 如何通过 CLI 传自定义环境变量

如果你用 `agentengine openclaw deploy` 创建实例，可以这样传：

```bash
agentengine openclaw deploy \
  --image hub.kce.ksyun.com/your-namespace/openclaw-user-custom:demo \
  --env APP_MODE=prod \
  --env API_BASE=https://api.example.com
```

这两个变量会直接进入容器环境。

另外要注意：

- `--env OPENCLAW_GATEWAY_AUTH_MODE=trusted-proxy` 可以
- `--env OPENCLAW_GATEWAY_AUTH_MODE=none` 可以
- `--env OPENCLAW_GATEWAY_AUTH_MODE=token` 不允许

## 中国大陆镜像源

为了方便用户后续在容器里自行安装依赖，Dockerfile 默认预置了这些源：

- ClawHub site: `https://cn.clawhub-mirror.com`
- ClawHub registry: `https://cn.clawhub-mirror.com`
- npm: `https://registry.npmmirror.com`
- pip: `https://mirrors.aliyun.com/pypi/simple`
- uv: `https://mirrors.aliyun.com/pypi/simple`
- Playwright: `https://npmmirror.com/mirrors/playwright`
- Puppeteer: `https://npmmirror.com/mirrors/chromium-browser-snapshots`

对应环境变量是：

- `CLAWHUB_SITE`
- `CLAWHUB_REGISTRY`

这两个变量的作用是：

- 如果用户后续在容器里安装了 `clawhub` CLI，默认会优先走中国镜像源
- 如果用户的插件、skill 或构建步骤里会调用 ClawHub，也可以直接复用这两个默认值

这份极简模板本身不额外预装 `clawhub` CLI，但会把默认源先准备好。

如果用户自己覆盖这些环境变量，以用户传入值为准。

## 打包给用户

建议直接 zip 整个目录：

```bash
zip -r openclaw-user-template.zip openclaw-user-template
```

用户拿到后只需要：

1. 解压
2. 按需修改 `custom/`
3. 执行 `docker build`
4. 执行 `docker run` 或 `agentengine openclaw deploy --image ...`

## 示例目录

### 1. 最简单示例：直接复用内置飞书 plugin + skills

如果你只是要把我们已经打好的公共镜像再封一层给用户，不想自己写任何 plugin / skill 代码，直接看：

```text
examples/bundled-feishu-plugin-skills/
```

这份示例的特点是：

- 基础镜像已经内置 `openclaw-lark`
- 飞书相关 skills 已经跟着这个 plugin 一起打进镜像
- Dockerfile 不覆写官方 `ENTRYPOINT` / `CMD`
- 启动时继续走官方 `bootstrap.sh`
- 如果需要在运行时预配置飞书 channel，直接传 `OPENCLAW_CHANNEL_BOOTSTRAP_JSON`

### 2. 进阶示例：skill + plugin + 自定义依赖

如果你要给用户一个“带最小 plugin、最小 skill、以及自定义 npm 依赖”的参考版本，可以直接看：

```text
examples/minimal-skill-plugin-deps/
```

这份示例额外演示了：

- 如何在 `custom/extensions/<plugin-id>/` 下放一个最小 OpenClaw plugin
- 如何在构建镜像时自动安装这个 plugin 自己的 npm 依赖
- 如何通过 `custom/config/openclaw.json` 显式启用用户自定义 plugin
- 如何用独立 `custom/skills/<skill-name>/SKILL.md` 去调用该 plugin 暴露的 tool
