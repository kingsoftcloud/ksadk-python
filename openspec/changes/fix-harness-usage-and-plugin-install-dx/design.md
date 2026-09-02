# Design: Harness 用量上报与 DSH 插件安装体验修复

## Context

实测证据链见 `docs/2026-09-02-harness-studio-e2e-report.md` §2/§5。三个缺陷的根因均已人工复现定位：

1. **用量丢失**：引擎侧链路完好——`ksadk/harness/runtime.py:221-236` 按 `input_tokens/output_tokens/cached_tokens/reasoning_tokens` 累加 `turn.usage` 并发 `usage.reported`；`HarnessReasoningTurn`（`ksadk/harness/reasoner.py:28`）已有 `usage` 字段；模型客户端 `ModelResponse.usage`（`ksadk/studio/model_client.py`，`Usage`：`input_tokens/output_tokens/total_tokens/cached_input_tokens/reasoning_output_tokens`）已解析。断点在 `_StudioHarnessReasoner.complete`（`ksadk/studio/plugin_runtime.py:139-142`）：构造 `HarnessReasoningTurn(final_text=..., tool_calls=...)` 时未传 `usage`，恒为 None。
2. **相对路径死链**：`_prepare_source`（`ksadk/plugins/bridges/dsh.py:869`）遇到"非绝对路径"直接把原始字符串作为 `dsh add` 参数；dsh 在 Profile 目录（`.agentkit/dsh-home/profiles/studio`）cwd 下解析相对路径，把不存在的相对位置写成 `link:` 依赖。实测警告：`Installing a dependency from a non-existent directory`。
3. **报错无指引**：`ksadk/cli/cmd_plugin.py:186-217` 把 `DshToolchainVersionMismatchError`/`DshToolchainUnavailableError` 映射成固定一句话，丢弃了异常携带的期望/实际版本信息。

## Goals / Non-Goals

**Goals**

- Studio PluginHost 链路 usage 端到端真实上报（reasoner → 引擎累计 → `usage.reported` → 运行记录 → 检查器）。
- 本地源安装路径解析确定性 + 死链防护。
- 工具链错误自带可执行修复指引。

**Non-Goals**

- 不改 DSH Profile 发现/注册/授权语义（显式 spec 仍须显式授权；仅模板默认创建预置权限）。
- 不改 `Usage`/`ModelResponse` 的用量解析与字段语义（仅新增 reasoning 捕获字段）。
- 不新增前端渲染能力（`thinking.*` 投影与思考过程卡片已存在，属复用）。

## Decisions

### D1. Reasoner 用量映射（选：显式字段映射，而非透传原始 dict）

`ModelResponse.usage` 字段名与引擎累加键不同（`cached_input_tokens`→`cached_tokens`、`reasoning_output_tokens`→`reasoning_tokens`）。在 `_StudioHarnessReasoner.complete` 内做一次显式映射；`response.usage` 为 None/全零时保持 `usage=None`（缺失态），让下游维持"不伪造 0 报告"的语义。理由：引擎契约键名稳定且已有 `_normalized_plugin_usage` 兼容多种命名，显式映射一处即可，不需要新增抽象层。

### D2. 相对源路径解析（选：仅"存在即绝对化"，不扩大错误面）

`_prepare_source` 开头增加一步：值非绝对时，若 `self._cwd / value` 存在则按该绝对路径继续既有打包流程；否则维持现状交给 dsh（兼容 npm 包名/git URL 等非本地 spec）。唯一新增的硬错误：值以 `.tgz` 结尾、按 cwd 解析不存在且不是绝对路径存在文件 → 抛 `DshPluginSourceError`（防死链）。理由：包名（如 `@scope/pkg@1.0`）与本地路径无法完全区分，"存在即绝对化"是最小充分修复；`.tgz` 后缀是明确的"用户以为在传本地文件"信号，此时静默死链不可接受。

### D3. CLI 指引（选：在既有 `CLIError.details` 中结构化携带）

`DshToolchainVersionMismatchError` 的 message 已含 "expected pnpm 11.7.0, got x"——映射处改为把 `str(err)` 并入 details（`expected`/`actual` 可用时结构化），并追加 `hint` 文本指向 `AGENTENGINE_PNPM_BIN` 与 corepack；`DshToolchainUnavailableError` 追加 hint：`agentengine plugin toolchain install`。不改 `abort_with_cli_error` 协议。

### D4. 推理内容透出（选：复用既有 reasoning 事件通道）

Studio 已把 `item_kind="reasoning"` 的 RuntimeEvent 投影为 `thinking.delta/completed`（`run_service.project_runtime_event`），前端已渲染思考过程卡片——缺的只是 harness 链路不产出 reasoning 事件。补三段：① `OpenAICompatibleModelClient` 的 chat-completions 解析捕获 `message.reasoning_content`（Responses wire 捕获 reasoning 输出项文本）到新字段 `ModelResponse.reasoning`；② `HarnessReasoningTurn` 增加 `reasoning` 字段并由 `_StudioHarnessReasoner` 透传；③ `ksadk/harness/runtime.py` 的 `execute_request` 累计各轮非空推理文本放入结果，`_stream` 在 usage 之前发射一对 `ItemStarted`/`ItemCompleted`（`item_kind="reasoning"`）。不新增事件类型、不改前端。

### D5. Harness 授权体验（选：模板预置 + 拒绝指引，不做无条件自动授权）

`test_scheduler_dsh_harness_product` 的 denied 用例确立了"显式不给权限必须被拒"的安全语义，因此只能在**调用方未显式提供 spec（走默认模板）且 Runtime 为 harness** 时预置 `process:host-user`（`create_studio_agent` / authoring `create` 两处入口，模板 spec 深拷贝后补权限）；显式 spec 一律不改写。同时把 `_preflight_bundle` 的 `PLUGIN_PERMISSION_DENIED` 错误 details 增加 `permission` 与修复指引文案，让友好错误卡可自助修复。

## Risks / Trade-offs

- D1 使 Studio 链路开始上报用量后，依赖"恒 0"的既有断言/快照需同步更新（实现阶段全量搜索确认）。
- D2 对"恰好与本地目录同名的 npm 包"存在理论冲突（本地同名目录存在时会被优先按本地源处理）——与修复前行为相比不新增歧义（修复前本地存在目录同样会被 pack），可接受。
- 用量键名映射散落在 studio 与引擎两处（`cached_tokens` vs `cached_input_tokens`），本次不统一命名，避免扩大改动面。

## Migration Plan

纯行为修复，无数据迁移。三处改动均向后兼容：不传相对路径/不触发工具链错误的用户路径行为不变。

## Open Questions

无。
