# 测试策略

公开贡献需要尽量使用可本地复现的测试。

## 常用命令

```bash
uv run --extra dev pytest
uv run --extra dev python -m mkdocs build --strict
make open-source-review
```

## 覆盖重点

- CLI 参数和错误提示。
- runner 加载和协议 payload。
- OpenAI 兼容 API。
- 文档链接和双语页面。
- sdist/wheel 文件边界。

不要依赖私有集群、真实客户数据或不可公开的服务凭证。
