# Web UI 仓库

KsADK Web UI 源码属于独立仓库 `kingsoftcloud/ksadk-web`。`ksadk-python`
只消费构建后的静态产物，用于本地 `agentengine web`。

## 边界

| 仓库 | 职责 |
| --- | --- |
| `ksadk-web` | 可编辑 React/Vite 源码、测试、双构建目标、Pages demo |
| `ksadk-python` | Python SDK、CLI、runner、本地 server、嵌入式 `ksadk/server/static` |
| hosted UI | 生产部署壳、网关、镜像和环境注入 |

`ksadk-python` 的公开 clean export 不包含 `ksadk/server/web-ui` 源码。

## 构建规则

发布时固定 `ksadk-web` tag 或 commit，然后构建：

```bash
npm ci
npm run build:ksadk
```

产物同步到 `ksadk-python/ksadk/server/static` 后再构建 wheel。不要在 wheel 构建时
实时拉取 GitHub latest release；latest 不可复现，也会引入网络和供应链风险。

## 发布记录

每次 `ksadk-python` release note 应记录：

- `ksadk-python` 版本。
- `ksadk-web` tag 或 commit。
- 静态产物构建命令。
- wheel/sdist 审计结果。
