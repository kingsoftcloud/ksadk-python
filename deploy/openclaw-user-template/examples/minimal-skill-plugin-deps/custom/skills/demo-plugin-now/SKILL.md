---
name: demo-plugin-now
description: Use the demo_now tool from the bundled demo-now plugin to verify that the custom image, plugin wiring, and APP_MODE env are all working.
---

# Demo Plugin Now

这个 skill 依赖 `demo_now` 工具。

它适合用来验证三件事：

- 自定义 plugin 是否已经被正确加载
- 自定义镜像里注册的 tool 是否可调用
- 部署时透传的 `APP_MODE` 环境变量是否生效

## 推荐步骤

1. 先调用 `demo_now`。
2. 默认传入 `format=YYYY-MM-DD HH:mm:ss`。
3. 如果用户明确要看容器里的原始环境变量，再执行 `bash scripts/show_app_mode.sh`。
4. 汇总返回的 `now`、`appMode`、`nodeVersion` 和 `pluginVersion`。

## 注意

- 这个 skill 本身不提供运行时能力。
- 真正的能力来自 `demo-now` plugin 注册的 `demo_now` 工具。
- 如果 `demo_now` 不存在，优先检查 `plugins.entries.demo-now.enabled` 是否已开启。
