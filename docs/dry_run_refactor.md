# Dry Run 重构说明（2026-03-05）

## 目标
- 统一 dry-run 行为，避免“有的命令支持、有的命令不支持”。
- 减少各命令里重复的 `try/except DryRunExit` 模板代码。
- 支持根命令级全局 dry-run：`agentengine --dry-run <subcommand> ...`。

## 核心改动

### 1. 新增公共模块
- 新文件：`ksadk/cli/dry_run.py`
- 提供能力：
  - `dry_run_option()`：复用型 Click 选项装饰器。
  - `run_async_with_dry_run()`：统一 `asyncio.run` + `DryRunExit` 收敛。
  - `effective_dry_run()`：合并“命令参数 dry-run + 全局 dry-run 环境标记”。

### 2. 全局 dry-run 开关
- 文件：`ksadk/cli/__init__.py`
- 新增根命令选项：`--dry-run`
- 行为：设置进程内环境变量 `AGENTENGINE_GLOBAL_DRY_RUN=1`，子命令可统一感知。

### 3. AgentEngineClient 统一感知全局 dry-run
- 文件：`ksadk/api/client.py`
- `AgentEngineClient.__init__` 中 `self.dry_run` 支持全局环境变量兜底：
  - 本地参数 `dry_run=True` 或
  - `AGENTENGINE_GLOBAL_DRY_RUN` 为真值（`1/true/yes/on`）。

### 4. CLI 命令接入与行为统一
- 已接入公共 dry-run 处理：
  - `cmd_status.py`
  - `cmd_destroy.py`
  - `cmd_deploy.py`
  - `cmd_launch.py`
  - `cmd_mcp.py`
  - `cmd_openclaw.py`
  - `cmd_version.py`
- `mcp` / `openclaw` 的 `list/status/delete` 新增 `--dry-run`。
- `mcp delete` / `openclaw delete` 增加 `-y/--yes`，并在 dry-run 模式下跳过确认。
- 统一修复：避免 `except Exception` 吞掉 `DryRunExit`（改为先 `except DryRunExit: raise`）。

## 使用方式

### 方式 A：子命令局部 dry-run
```bash
agentengine mcp status mcp-demo --dry-run
agentengine openclaw list --dry-run
agentengine version list --agent demo-agent --dry-run
```

### 方式 B：根命令全局 dry-run
```bash
agentengine --dry-run mcp status mcp-demo
agentengine --dry-run deploy --target serverless
agentengine --dry-run launch .
```

## 回归验证

### 自动化测试
- 新增：`tests/test_cli_dry_run.py`
- 结果：
  - `pytest -q tests/test_cli_dry_run.py` => `5 passed`
  - `pytest -q tests/test_deploy_integration.py` => `6 passed`

### 命令级验证（真实 CLI）
- `python -m ksadk.cli mcp status mcp-demo --dry-run`
- `python -m ksadk.cli openclaw list --dry-run`
- `python -m ksadk.cli version list --agent demo-agent --dry-run`
- `python -m ksadk.cli --dry-run mcp status mcp-demo`
- 验证点：
  - 请求被打印（含 headers/body/curl）。
  - 命令退出码为 0。
  - 输出统一包含 `Dry Run Completed`。

## 兼容性说明
- 现有 `--dry-run` 命令参数保持可用。
- 新增了根命令全局开关，不破坏原命令格式。
- 仅改 dry-run 流程，不改 API 返回字段结构。
