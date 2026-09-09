# 官方 DSH 工具链测试依赖

仅用于测试，不是另一个产品运行时或可发布插件。锁文件固定 Cordis、DSH Tools
及传递依赖；不包含第三方源码。建议在临时目录安装，避免污染产品工作区。

从仓库根目录执行：

```bash
runtime_dir=$(mktemp -d /tmp/ksadk-resource-core.XXXXXX)
cp tests/fixtures/resource_runtime/dsh-runtime/package*.json "$runtime_dir/"
npm ci --prefix "$runtime_dir" --ignore-scripts --no-audit --no-fund
KSADK_TEST_DSH_NODE_MODULES="$runtime_dir/node_modules" uv run --no-sync pytest -q tests/resource_runtime
```

没有设置该环境变量时，官方 Cordis 集成用例明确标记 skipped，不能视为通过。
其余 Node 协议、真实 Worker 和本地 HTTP 数据面集成不依赖这套 npm 安装。
