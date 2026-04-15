# skill + plugin + 自定义依赖 极简示例

这个目录是给用户打包参考用的“进阶最小例子”。

它演示三件事：

- 如何内置一个最小 OpenClaw plugin
- 如何让这个 plugin 带一个自定义 npm 依赖
- 如何再配一个独立 skill 去调用这个 plugin 提供的 tool

这份示例刻意只用一个 npm 依赖 `dayjs`，不再额外引入 Python 包、编译链或独立 bootstrap。

## 目录结构

```text
minimal-skill-plugin-deps/
├── Dockerfile
├── Makefile
├── README.md
└── custom/
    ├── config/
    │   └── openclaw.json
    ├── extensions/
    │   └── demo-now-plugin/
    │       ├── index.ts
    │       ├── openclaw.plugin.json
    │       └── package.json
    └── skills/
        └── demo-plugin-now/
            ├── SKILL.md
            └── scripts/
                └── show_app_mode.sh
```

## 这个示例具体做了什么

### 1. plugin

`custom/extensions/demo-now-plugin/` 里放了一个最小 tool plugin。

它会注册一个工具：

- `demo_now`

这个工具会返回：

- 当前时间
- `APP_MODE` 环境变量
- Node 版本
- 插件自身版本

其中“当前时间格式化”用到了 `dayjs`，这就是示例里的自定义依赖。

### 2. skill

`custom/skills/demo-plugin-now/` 是一个独立 skill。

它不自己实现运行时能力，而是直接指导模型去调用上面的 `demo_now` 工具，并且在需要时执行本地脚本：

- `scripts/show_app_mode.sh`

这样用户可以一眼看懂：

- plugin 负责注册能力
- skill 负责把能力包装成更好用的提示/工作流

### 3. 默认配置

`custom/config/openclaw.json` 里做了两件事：

- 保留 `trusted-proxy` 所需的最小 `gateway.auth.trustedProxy` 配置
- 显式启用 `demo-now` plugin

这里很关键，因为 OpenClaw 对非 bundled 的用户自定义 plugin 默认不是“发现即启用”。
如果你不在配置里写：

```json
{
  "plugins": {
    "entries": {
      "demo-now": {
        "enabled": true
      }
    }
  }
}
```

那 plugin 很可能被发现了，但不会真正启用。

## Dockerfile 比主模板多了什么

只多了一段：

```dockerfile
find /opt/openclaw-template/extensions -mindepth 1 -maxdepth 1 -type d | while read -r dir; do
  if [ -f "$dir/package.json" ]; then
    cd "$dir"
    npm install --omit=dev --no-audit --no-fund --registry="${OPENCLAW_NPM_REGISTRY}"
  fi
done
```

它的意思是：

- 只要你的自定义 plugin 目录里有 `package.json`
- 构建镜像时就自动帮你装这个 plugin 自己的 npm 依赖

这适合绝大多数“只多几个 npm 包”的轻量 plugin。

## 直接构建

```bash
make build IMAGE=hub-vpc-cn-beijing-6.kce.ksyun.com/your-namespace/openclaw-user-example TAG=demo
```

或者：

```bash
docker build -t openclaw-user-example .
```

## 直接运行

```bash
docker run --rm -it -p 8080:8080 openclaw-user-example
```

如果你本地只是临时验证页面是否起来，也可以改成：

```bash
docker run --rm -it -p 8080:8080 \
  -e OPENCLAW_GATEWAY_AUTH_MODE=none \
  openclaw-user-example
```

## 部署时透传自定义环境变量

例如：

```bash
agentengine openclaw deploy \
  --image hub-vpc-cn-beijing-6.kce.ksyun.com/your-namespace/openclaw-user-example:demo \
  --env APP_MODE=prod
```

部署完成后：

- `demo_now` 工具会读到 `APP_MODE=prod`
- `demo-plugin-now` skill 里的脚本也会读到 `APP_MODE=prod`

## 如果你要换成自己的 plugin

直接改这几个文件就够了：

- `custom/extensions/demo-now-plugin/package.json`
- `custom/extensions/demo-now-plugin/openclaw.plugin.json`
- `custom/extensions/demo-now-plugin/index.ts`
- `custom/config/openclaw.json`

最容易漏的是最后一个：

- 如果你改了 plugin id，记得同步改 `plugins.entries.<id>.enabled`

## 如果你要再加 Python 依赖

这个示例没有默认帮你装 Python 包，因为基础镜像自带 `python3`，但不保证带 `pip`。

如果你的 skill 或 plugin 真要依赖 Python 包，建议在这份 Dockerfile 的基础上，再显式补一段你自己的 Python 安装逻辑，而不是把它混进主模板里。
