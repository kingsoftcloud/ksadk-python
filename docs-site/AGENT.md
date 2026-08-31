# AGENT.md — KsADK 文档写作与维护指南

编辑 `docs-site/` 时遵循本文件。公开能力描述必须以当前 KsADK 源码、测试和可复现构建结果为依据。

## 工程约定

- 框架：Fumadocs、Next.js、静态导出。
- 内容目录：`content/docs/**`。
- 中文：`x.mdx`，路由前缀 `/cn/`。
- 英文：`x.en.mdx`，路由前缀 `/en/`。
- 每个公开页面必须中英成对，标题层级、表格、示例、链接和图片结构一致。
- 导航由目录中的 `meta.json` 与 `meta.en.json` 管理。
- 页面 frontmatter 通常只写 `title`，必要时增加 `status`。

## 写作风格

1. 简洁、声明式、一句一义。
2. 概述先讲能力和边界，内部类名、变量名与文件路径放在实现章节。
3. 参数和职责对比优先使用表格。
4. 不把规划中的能力写成已实现能力；区分代码存在、测试通过与公开发布。
5. 不把 KsADK SDK 描述为完整控制平台。注册、网关、远端生命周期和外部服务资源应标明平台边界。
6. `/v1/responses` 保持标准协议语义；KsADK 扩展必须显式标注。

## 架构内容

- 总体架构以 Agent Kernel、Harness、插件化 Provider 和统一事件事实链为主线。
- Host 负责插件图、准入、健康检查和生命周期；Provider 保留框架原生执行语义与私有状态。
- 共用能力通过能力总线注入 Harness，不画成另一条顺序执行链。
- API、Studio 与托管界面是 RuntimeEvent / SessionEvent 的投影消费者。
- SVG 与 PNG 放在 `public/assets/`；英文资产使用 `.en` 后缀。
- SVG 不写分支名、提交号、日期或“基于某主线”等易过期信息。
- 修改图源后同时更新中英文 PNG，并检查文字遮挡、连线穿透和缩放可读性。

## Fumadocs 组件

- 流程使用 Mermaid、SVG 或 `<Steps>`，不使用 ASCII 图。
- 目录结构使用 `<Files>`、`<Folder>` 和 `<File>`。
- 按需使用 `<Callout>`、`<Tabs>`、`<Accordions>`、`<Cards>` 与 `<TypeTable>`。
- 不在页面结尾添加冗余的“下一步”卡片。

## 安全边界

- 不提交密钥、`.env`、私有域名、内部集群路径、私有镜像仓库或客户信息。
- 示例地址使用文档保留地址，凭证只写环境变量或 secret reference。
- 公开文档不复制内部 runbook、预发布证据或运维操作。

## 验证

```bash
make docs-site-build
git diff --check
```

架构图还需执行 SVG 语法校验，并分别检查中文和英文渲染结果。
