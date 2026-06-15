# 发布流程

公开发布分为维护者审核、GitHub 导入、Pages、release 和 PyPI/TestPyPI。

## 发布前

```bash
make open-source-review
make open-source-review-bundle
make public-publish-check PUBLIC_PUBLISH_PHASE=pre-publish V=0.6.5
```

## GitHub

维护者 review 通过后，从已审核的 GitHub `main` commit 创建 tag 和 release。
不要从未同步的候选分支直接创建公开 release。

## PyPI

PyPI metadata 指向：

- Repository: `https://github.com/kingsoftcloud/ksadk-python`
- Documentation: `https://kingsoftcloud.github.io/ksadk-python/`

上传凭证不进入仓库。推荐使用 PyPI Trusted Publishing / GitHub OIDC；
如需临时 token，也只能放在发布系统 secrets 或维护者本地环境中。

真正执行 `make publish` 或 `make publish-test` 前，必须填好
`docs/maintainer-approval-record.md`。该记录需要包含已审核 commit SHA、
发布策略和维护者签署。
