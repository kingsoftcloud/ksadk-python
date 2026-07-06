# KsADK P0-P1 Coding-Agent Tooling 与运行时治理

## 目标

P0 目标是把现有内置工具补齐到可组合 coding agent 的最低可用面，同时保持托管默认安全。P1 在此基础上补齐 Claude Code-like 的发现、批量编辑和 deferred tools 动态注入体验。

本阶段只增强现有工具面和必要运行时治理：

- 工具能力覆盖 `read/edit/search/list/run/web`。
- 工具入口覆盖 canonical `tool_dispatcher`、兼容 alias `agentengine_tool_dispatcher` 和轻量 `tool_search`。
- 运行时治理覆盖输出预算、sandbox 生命周期、命令策略、熔断与观测。
- 不引入 V2 工具命名。
- 不引入独立 Git tools，Git 读操作通过受控 `run_command` 覆盖。
- 托管环境不会在 E2B 不可用时自动 fallback 到 pod/local process。

## 内置工具面

### Workspace

保持原工具名：

- `list_workspace_files(path=".", glob=None, recursive=False, include_dirs=True, max_results=500, sort_by="name")`
- `read_workspace_file(path, start_line=None, end_line=None, max_chars=None, include_line_numbers=True)`
- `edit_workspace_file(path, old_text, new_text, expected_replacements=1, replace_all=False)`
- `multi_edit_workspace_file(path, edits, replace_all=False)`
- `search_workspace_files(query, path=".", is_regex=False, glob=None, case_sensitive=False, context_lines=0, max_results=100)`

关键约束：

- 所有路径被限制在 workspace root 内。
- `read_workspace_file` 返回 `mtime_ns`、`size_bytes`、`total_lines`、`line_count`、`read_range`、`partial`、`truncated` 和 `suggested_action`，并按 tool execution session 记录 read state。
- `edit_workspace_file` 强制 read-before-edit，并校验 `mtime_ns` 与 `size_bytes`，避免覆盖用户或外部进程刚写入的内容。
- read state 通过 `ToolExecutionContext.session_id` 做会话隔离；本地 direct tool call 没有上下文时落到 `default` session，仍支持 `read -> edit` 直调用链路。
- `edit_workspace_file` 默认要求唯一命中；多处替换必须显式 `replace_all=True`。
- `edit_workspace_file` 失败时返回可重试诊断：`snippet_not_found` 包含 nearby candidates，`ambiguous_edit` 包含命中行号和预览。
- `multi_edit_workspace_file` 先校验全部 edits，再一次写入；任一 edit 失败则不落盘。edits 按顺序应用，后一个 edit 在前一个 edit 的结果上匹配。
- 精确匹配失败后会做 quote normalization，用于处理智能引号和普通引号差异；更宽松的 fuzzy match 保持显式 opt-in。
- `search_workspace_files` 优先使用 `rg`，不可用时 fallback Python 搜索，并统一返回 `searched_path`、`match_count`、`context_lines`、`search_backend`。
- 大结果和 diff 通过 `ToolResultBudget` 返回 preview 与落盘引用。

### Registry / Dispatcher / Search

推荐入口：

- `tool_dispatcher(action, tool_name=None, arguments=None, include=None, profile="default")`
- `agentengine_tool_dispatcher(...)` 仅作为兼容 alias。
- `tool_search(query, profile="coding", max_results=8, include_disabled=False)`
- `get_agentengine_tools(include=None, profile="default", mode="direct")`
- `describe_agentengine_tools(include=None, profile="default", mode="direct")`
- `get_ksadk_builtin_tools(profile="coding")`

语义：

- `tool_dispatcher.action` 支持 `list|describe|call`。
- `tool_dispatcher.call` 继续走现有 tool gateway、approval、result budget 和 observability，不绕过治理。
- `describe_agentengine_tools` 返回 `enabled/backend/boundary/requires_approval/args`；未配置 sandbox 或 search provider 时工具仍可描述，但 `enabled=false`。
- `tool_search` 基于本地 registry descriptors 做 keyword/BM25-like 打分，不联网、不执行工具，返回 `name/description/group/args/risk_level/enabled/boundary/score/reason/deferred_tool_names`。

### MCP Tool Discovery

`tool_search` 同时支持 ksadk builtins 和已登记的外部工具 descriptors。MCP 执行路径仍归原框架或 MCP runtime 所有，ksadk 只把 descriptor 纳入发现面。

托管/ADKRunner 管理的 MCP：

- 用户继续沿用 `KSADK_MCP_SERVERS` 和 `KSADK_ENABLE_MCP_TOOLS` 配置 MCP server。
- ADKRunner 注入 MCP toolset 后，会 best-effort 读取 `get_tools_with_prefix()` 并把 tool descriptors 注册到 `tool_search` catalog。
- 如果当前线程已有 event loop，ADKRunner 不会阻塞等待 async MCP list；这种情况下 MCP 仍可正常执行，只是 discovery 需要后续异步路径或显式 helper 补登记。

框架自己管理的 MCP：

```python
from ksadk.toolsets import register_external_tools, get_agentengine_tools

register_external_tools(mcp_tools, group="mcp:weather")
tools = get_agentengine_tools(include=["tool_search", "tool_dispatcher"])
```

边界：

- external MCP tools 在 `tool_search` 中返回 `execution="external"`。
- `tool_dispatcher` 不调用 external tools；它只治理 ksadk builtins。
- Remote/Responses deferred direct injection 会跳过 `execution="external"` 的工具，避免把 MCP descriptor 误注入成 ksadk direct function。

### Cross-Framework Injection

配置：

```text
KSADK_BUILTIN_TOOLS_MODE=off|dispatcher|focused|deferred
KSADK_BUILTIN_TOOLS_PROFILE=default|coding
```

语义：

- `off` 不注入内置工具，保持兼容默认。
- `dispatcher` 只注入 `tool_dispatcher`。
- `focused` 注入 coding profile 的 direct tools。
- `deferred` 默认只注入 `tool_search + tool_dispatcher`。

框架行为：

- ADKRunner 在 `load_agent()` 时按模式注入 builtins，并保持不注入旧 `execute_bash/execute_python`。
- ADKRunner 支持 `inject_deferred_tools_for_request([...])`，可在下一轮请求前追加 `tool_search` 返回的 direct tools，并按名称去重。
- LangChain/LangGraph 不强改用户 graph；standard hook 场景通过 `session_context["ksadk_tools"]` 暴露 descriptors，并标记 `deferred_direct_injection_supported=false`。
- Remote/Responses payload 会按模式注入 OpenAI function schema；`deferred_tool_names` 存在时会在 `tool_search + tool_dispatcher` 之外追加对应 direct tool schemas。
- capabilities endpoint 应继续通过 `describe_agentengine_tools()` 展示 builtins，并标出当前注入模式。

### Deferred Tools Flow

P1 deferred 主路径：

1. 第 1 轮只暴露 `tool_search + tool_dispatcher`。
2. 模型调用 `tool_search("edit file")` 后，runtime 从 tool output 中读取 `deferred_tool_names`。
3. runtime 写入 `run_status` 控制事件，`detail=deferred_tools_selected`。
4. 下一轮 `build_run_input` 读取该控制事件，把 `deferred_tool_names` 放入 request metadata。
5. Remote/Responses 根据 metadata 追加 direct tool schemas；ADK 可在 request 前调用 `inject_deferred_tools_for_request` 追加 direct tools。
6. 不支持 request-local direct injection 的框架继续使用 `tool_search + tool_dispatcher` fallback。

### Sandbox

保持原工具名：

- `sandbox_status()`
- `run_command(command, cwd=None, timeout=None, env=None, background=False)`
- `run_code(code, language="python", timeout=None, env=None)`

关键约束：

- `run_command` 和 `run_code` 使用会话级 sandbox registry。`KSADK_SANDBOX_SESSION_ID` 显式配置优先；否则有 tool execution session 时统一使用 `ksadk-session:{session_id}`，让同一 ksadk 会话内的 command/code 复用同一个 sandbox。
- 本地 direct tool call 没有 tool execution context 时保留旧 fallback key：`ksadk-direct-shared` 与 `ksadk-code-shared`。
- E2B 是托管默认隔离 backend。
- `local_process` 和 `pod_process` 只在显式配置时可用，且 `pod_process` 必须设置 `KSADK_ALLOW_POD_PROCESS_TOOLS=true`。
- `run_code` 是 snippet runner，不是 shell 替代品；它只允许 isolated sandbox backend。`local_process/pod_process` 下会返回 `isolated_sandbox_required`。
- `background=True` 在 P0 返回 `background_not_supported`。
- `sandbox_status` 返回 backend、enabled、isolated、sandbox_id、created_at、last_used_at、expires_at、idle_seconds、TTL、quota 和隔离边界描述。
- 首次创建隔离 sandbox 时会同步当前 workspace 文件到 sandbox 的 `/workspace`。

### Web

新增工具：

- `web_fetch(url, max_chars=None, timeout=30)`
- `web_search(query, max_results=5, recency_days=None)`

关键约束：

- `web_fetch` 仅支持 `http/https`。
- 请求前和 redirect 后都执行 SSRF 检查，默认拦截 loopback、link-local、private、reserved、metadata 地址。
- HTML 响应会转为纯文本，并移除 `script/style/noscript`。
- 大网页通过 `ToolResultBudget` 返回 preview 与落盘引用。
- `web_search` 只返回 `title/url/snippet/rank/provider`，不会自动抓全文。
- Search provider 可插拔；当前支持 `fake` 和 HTTP provider adapter。未配置时返回 `provider_not_configured`。

## 输出预算

`ksadk.tools.result_budget.ToolResultBudget` 统一处理大输出：

- `max_chars`
- `preview_chars`
- `persist_threshold_chars`
- `persist_dir`

返回结构包含：

- `truncated`
- `original_chars`
- `preview_chars`
- `persisted.path`
- `persisted.mime_type`

默认落盘目录：

```text
${AGENTENGINE_UI_DIR}/tool-results
```

未设置 `AGENTENGINE_UI_DIR` 时使用本地 session 目录下的 `tool-results`。P0 只支持 `text/plain` 和 `application/json`。

模型上下文投影时，只投影 preview 和 `[persisted-output] path (mime)` 引用；append-only transcript 仍保留原始事件元数据。

## Sandbox Backend 配置

```text
KSADK_SANDBOX_BACKEND=e2b|local_process|pod_process
KSADK_SANDBOX_TEMPLATE_ID=<e2b-template-id>
KSADK_SKILL_RUNTIME_TEMPLATE_ID=<legacy-template-id-alias>
KSADK_ALLOW_POD_PROCESS_TOOLS=true
KSADK_SANDBOX_TTL_SECONDS=900
KSADK_SANDBOX_IDLE_TTL_SECONDS=0
KSADK_SANDBOX_MAX_SESSIONS=0
```

语义：

- `e2b` 需要 template id；缺失时 `run_command/run_code` 返回明确不可用错误。
- `local_process` 仅用于本地或私有化显式配置。
- `pod_process` 必须显式开启 `KSADK_ALLOW_POD_PROCESS_TOOLS=true`。
- `local_process/pod_process` 的 `isolated=false`，它们共享宿主或 pod/container 的文件系统、网络和 allowlist 环境。
- 不做自动 fallback；E2B 不可用时不会偷偷切到 local/pod process。

## 命令策略

`check_command_policy` 对 `run_command` 做默认策略检查。

默认允许：

- `git status`
- `git diff`
- `git log`
- `git show`
- `git branch`
- `git ls-files`

默认拒绝：

- `git reset`
- `git clean`
- `git push`
- `git checkout`
- recursive `rm`，例如 `rm -r`、`rm -rf`
- `sudo`
- `kubectl`
- `docker`
- 访问 metadata/private endpoint 的命令

本阶段不做独立 Git tools。需要 Git 能力时，使用受策略约束的 `run_command`。P0 command policy 基于 `shlex` token 检查，不是 Bash AST；命令替换、管道和复杂 shell 语义的完整识别后续用 AST parser harden。

## 运行时治理

当前熔断配置：

```text
KSADK_MAX_TURNS=0
KSADK_MAX_TOOL_CALLS=0
KSADK_MAX_CONSECUTIVE_TOOL_FAILURES=0
KSADK_MAX_CONSECUTIVE_APPROVAL_DENIALS=0
KSADK_MAX_CONSECUTIVE_COMPACT_FAILURES=0
```

语义：

- `0` 表示不启用该限制。
- 超限后写入 `run_status=failed`。
- `run_status.metadata.governance` 会记录 reason、当前计数和配置阈值。
- tool result metadata 会记录 `tool_name/duration_ms/output_chars/truncated/persisted/exit_code/error_type`。

## P0 不做项

本阶段明确不做：

- V2 tool naming。
- 独立 Git tools。
- Plan Mode。
- Task/Sub-agent 一等工具。
- Hooks。
- LSP / language server 工具。
- Browser / Computer Use 一等封装。
- 后台进程列表、kill、端口预览 UI。
- 托管环境自动 fallback 到 pod local process。

## 已知限制

- `ToolExecutionContext` 基于 Python `contextvars` 传递。runner 在同一 async task 中执行工具时会自动继承；如果框架把工具调用派发到 thread pool，需要后续通过 `copy_context()` 或框架适配显式传播，否则会回落到 `default` direct-call 行为。

## 回归命令

```bash
python -m pytest \
  tests/test_agentengine_toolsets.py \
  tests/test_sandbox_backend.py \
  tests/test_tool_gateway.py \
  tests/test_tool_result_budget.py \
  tests/test_web_toolset.py \
  tests/test_conversation_runtime.py \
  tests/test_runner.py \
  tests/test_langchain_runner_session_continuity.py \
  tests/test_remote_runner.py \
  tests/long_task -q

python -m compileall ksadk
```
