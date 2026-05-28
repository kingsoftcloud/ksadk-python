# 贡献指南

贡献应面向公开仓库、公开文档和公开 CI。不要在 pull request 中包含内部凭证、
私有 endpoint、客户数据或公司内部部署 runbook。

## 开发环境

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

本地构建文档：

```bash
make public-docs-build
make public-docs-serve
```

运行开源检查：

```bash
make open-source-audit
make public-docs-audit
```

如果变更影响 packaging、公开文档、release metadata 或仓库布局，请在请求 release
approval 前运行更完整的审核目标：

```bash
make open-source-review
```

## 公开 CI 期望

公开 CI 不能依赖内部 kubeconfig、内部 registry、内部对象存储或本地 `.zread/` 状态。

提交公开 PR 前：

- 运行变更区域的 focused tests。
- 修改 `public-docs/` 或 `mkdocs.yml` 时运行文档构建。
- 命令行为变化时更新 CLI 文档。
- packaging 或公开 API 变化时更新 release notes。
- 示例应保持本地优先，除非某个 hosted feature 已明确批准公开。

## 测试策略

| 层级 | 目的 | 常见命令 |
| --- | --- | --- |
| unit 和 component tests | 验证 helpers、配置解析、runners、sessions 和 packaging 规则 | `pytest tests/ -q` |
| CLI snapshot tests | 保护用户可见 command help、resource output 和 error hints | `tests/` 下 focused pytest 文件 |
| ASGI service tests | 不启动真实网络 server 验证 FastAPI routes 和 session events | service/session pytest 文件 |
| HTTP protocol E2E | 验证 `/v1/responses`、`/v1/chat/completions`、upload 和本地 Web UI action payload | OpenAI protocol E2E tests |
| browser E2E | Chromium 可用时验证构建后的 UI 行为 | browser-tagged E2E tests |
| open-source audits | 验证公开 tree、docs、Pages artifact、sdist、wheel 和 clean export 边界 | `make open-source-review` |

当变更影响协议形态、附件处理、session event 或本地 Web UI payload 时，优先写一个
跨越真实客户端边界的测试。

## 文档贡献

公开文档应写给外部开发者。优先使用：

- 能在干净虚拟环境中运行的命令。
- 占位凭证和 provider URL。
- 明确的本地 fallback。
- 本地 SDK 行为与 hosted AgentEngine 行为之间的清晰区别。

避免：

- 私有 URL。
- kubeconfig 路径。
- 内部 registry 名称。
- 真实 token 或客户数据。
- 把生成的 `.zread/` 输出当作发布源引用。

本地 zread wiki 输出可以作为工程笔记来源，但公开文档应是 `public-docs/` 下经过整理的
Markdown。不要发布生成的 wiki 目录，也不要让公开 CI 依赖它。

## 开源审核边界

公开仓库应包含外部开发者需要的 SDK、公开示例、公开文档、CI 和 release metadata。
它不应包含：

- 内部部署自动化。
- 私有 registry 或对象存储位置。
- 内部 incident notes 或 operator playbooks。
- `.pypirc`、PyPI/TestPyPI token、GitHub token、kubeconfig 或本地云凭证。
- 本地 session 状态、上传文件、抽取后的附件内容或生成的 build output。

如果某个文件内部有用但不适合公开，请把它排除在 clean export 之外，并在人工整理文档中
总结相关公开行为。

## 安全

公开仓库创建后，漏洞报告通过 `SECURITY.md` 中记录的安全流程处理。
