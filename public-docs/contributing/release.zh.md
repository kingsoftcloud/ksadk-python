# 发布流程

公开发布分为内部审核、GitHub 导入、Pages、release 和 PyPI/TestPyPI。

## 发布前

```bash
make open-source-review
make open-source-review-bundle
python3 scripts/check_publication_state.py --phase placeholder
```

## GitHub

审批通过后，使用审核过的 clean export 替换 placeholder。不要把完整历史直接公开，
除非历史改写和 secret scan 已单独批准。

## PyPI

PyPI metadata 指向：

- Repository: `https://github.com/kingsoftcloud/ksadk-python`
- Documentation: `https://kingsoftcloud.github.io/ksadk-python/`

上传凭证只放本地或发布系统 secrets，不进入 GitHub。
