# 安全边界

公开仓库、包、文档和 GitHub Pages 都必须使用最小公开面。任何 token、私有 registry、
kubeconfig、客户数据和内部部署细节都不能进入 GitHub。

## 禁止进入公开仓库

- `.pypirc`、PyPI/TestPyPI token。
- 私有 registry 凭证和 kubeconfig。
- 真实 API Key、长期 token、数据库 DSN。
- 内部 Helm values、部署脚本和生产路由。
- `.zread/`、内部分析快照和客户资料。

## 包边界

`ksadk-python` wheel 保留运行所需 Python 代码和 `ksadk/server/static` 静态 UI
产物，不包含 `ksadk/server/web-ui` 可编辑源码或 hosted bundle。

Web UI 源码属于独立 `kingsoftcloud/ksadk-web` 仓库。构建静态产物时应固定
`ksadk-web` tag 或 commit。

## 文档边界

公开文档使用 GitHub Pages URL 和占位配置。不要写入：

- 内部文档地址。
- 真实服务 endpoint。
- 客户截图、日志和数据集 id。
- 内部排障流程或值班信息。

## 发布边界

公开 GitHub source import、Pages、release、PyPI/TestPyPI 上传必须在维护者 review
和维护者审批之后执行。审批前公开仓只保留 placeholder。
