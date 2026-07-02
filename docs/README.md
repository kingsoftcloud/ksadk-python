# ksadk-python 文档索引

本文件是 `ksadk-python` 仓库 `docs/` 目录的导航索引。

## docs/ 与 public-docs/ 的关系

- **`docs/`（本目录，master 分支）**：内部工作文档、设计稿、用户指南源稿和技术参考。不直接发布到 GitHub Pages。
- **`public-docs/`（main 分支）**：公开发布的 mkdocs 站点源（<https://kingsoftcloud.github.io/ksadk-python/>），由独立的 `mkdocs.yml` 构建，结构为 Getting Started / Build / Run / Deploy / Observe / Extend / Reference 七大板块。
- 本目录的 `guides/`、`reference/` 是 `public-docs/` 的源稿参考，两者保持口径一致但结构独立。新增用户文档时先在 `docs/` 落稿，再按需同步到 `public-docs/`。

## 目录结构

### guides/ — 用户指南

面向 Agent 开发者的 how-to 文档。

- `ksadk使用文档.md` — CLI 全景、最短成功路径、`/v1/responses` OpenAI 兼容接口、`build/deploy/launch`、`files`、Hermes、OpenClaw 命令主线
- `Agent 开发者上下文接入指南.md` — 多轮会话历史、图片/附件、模型能力元数据、各框架上下文接入
- `记忆使用指南.md` — 平台长期记忆工具与 OpenClaw memory backend（`openclaw_default` / `mem0` / `lancedb`）
- `知识库与记忆示例.md` — KB + LTM 组合示例与能力矩阵
- `DeepAgents说明.md` — DeepAgents 框架接入
- `LangGraph开发最佳实践.md` — LangGraph 接入与 interrupt / Responses approval

### reference/ — 技术参考

面向部署、运维和 SDK 集成排障的参考文档。

- `ksadk环境变量参考.md` — 全量环境变量（由 `tests/test_config_env_registry.py` 保证与 `ENV_VAR_REGISTRY` 一致）
- `远程Agent运行时接口说明.md` — 远程 runtime API 契约
- `ksadk技术设计.md` — 分层、`ksadk_runtime_common`、workspace、Hermes/OpenClaw、Docker、服务端边界
- `OpenClaw接口说明.md` — OpenClaw 服务端 API 接入
- `OpenClaw网关技术说明.md` — OpenClaw 网关技术文档
- `openclaw一键部署指南.md` — OpenClaw 一键部署
- `assets/` — OpenClaw 网关流程图与 HTML 资产

### release/ — 发布流程

- `public-release-workflow.md` — 公开分支与发布流程（`AGENTS.md` 引用的 canonical 文档）

### internal/ — 内部设计文档

内部架构与设计稿，不面向终端用户。

- `Runner_Approval_Architecture.md`、`coding-agent-p0-tooling.md`、`dry_run_refactor.md`、`ksadk-skills-analysis-and-design.md`、`sandbox-runtime-design.md`、`skill-runtime-e2e.md`
- `工作区文件技术设计.md`、`平台可观测与用户反馈设计方案.md`
- `0.6.8-release-prep-notes.md` — 0.6.8 文档优化记录

### archive/ — 历史归档

已完成或停用的规划稿与方案，保留历史。

- `hosted-ui-refactor-plan.md`、`ksadk-iteration-plan-condensed.md`、`ksadk开源准备计划.md`、`prompt-driven-agent-creation-draft.md`、`veadk-benchmark-and-iteration-plan.md`
- `kb-memory/`、`versions/`、`workspace/` — 早期设计稿

### preview/ — 预览资产

- `cli-demo/`、`images/` — 截图与 demo 资产

### superpowers/ — Superpowers skill 规格

- `plans/`、`specs/` — 流程型 skill 的计划与设计规格

## 按角色阅读路径

- **Agent 开发者（新接入）**：`guides/ksadk使用文档.md` → `guides/Agent 开发者上下文接入指南.md` → `guides/LangGraph开发最佳实践.md`
- **记忆 / 知识库场景**：`guides/记忆使用指南.md` → `guides/知识库与记忆示例.md`
- **部署 / 运维**：`reference/ksadk环境变量参考.md` → `reference/openclaw一键部署指南.md` → `release/public-release-workflow.md`
- **SDK 集成 / 排障**：`reference/ksadk技术设计.md` → `reference/远程Agent运行时接口说明.md` → `reference/ksadk环境变量参考.md`
- **内部架构 / 贡献者**：`internal/` → `reference/ksadk技术设计.md`

## 维护约定

- **环境变量文档**（`reference/ksadk环境变量参考.md`）必须与 `ksadk/configs/env_registry.py` 的 `ENV_VAR_REGISTRY` 保持一致，由 `tests/test_config_env_registry.py` 强制校验。新增 env var 时同步更新文档，否则测试失败。
- **内部规划稿**完成后应移入 `archive/`，避免与活跃用户文档混在顶层。
- **文档间相对链接**以本目录为根，移动文件时必须同步更新引用（`guides/` ↔ `reference/` ↔ `internal/` 跨目录用 `../`）。
- **公开发布**只在 `main` 分支的 `public-docs/` 进行；`docs/` 改动需按 `release/public-release-workflow.md` 流程同步到 `public-docs/`。
