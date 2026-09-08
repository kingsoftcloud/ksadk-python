# ksadk 开源准备计划

> 状态：草案  
> 目标读者：ksadk 维护者、AgentEngine 平台维护者、Hosted UI 维护者、法务/安全/发布负责人  
> 范围：`ksadk-python` 开源前的代码、文档、UI、构建、合规、验证和发布准备

## 1. 目标

`ksadk-python` 开源后应满足三个目标：

1. 外部开发者可以通过公开仓库理解、安装、运行、测试和本地部署 SDK。
2. `agentengine web` / 本地 Hosted UI 可以在无内部平台依赖的环境中运行，并具备可审计源码。
3. 生产 Hosted UI、SDK 内嵌 UI 和公开源码不再长期分叉。

本计划不要求一次性开放 AgentEngine 控制面、Skill Service、Sandbox Service、K8s 发布系统或内部网关。开源边界应明确：`ksadk` 是 SDK、CLI、运行时适配和本地开发体验，不是完整平台控制面。

## 2. 推荐总体架构

### 2.1 UI 真源拆分

当前状态：

- `agentengine-hosted-ui` 持有生产 Hosted UI 源码、Dockerfile、nginx、Helm 发布入口。
- `ksadk-python/ksadk/server/web-ui` 也持有一份 React UI 源码副本。
- `ksadk-python/ksadk/server/static` 是 SDK 包内实际服务的静态产物。

推荐状态：

```text
agentengine-ui-core        # 中性 UI 源码真源，可开源
  src/
  tests/
  package.json
  vite.config.ts
  public/

agentengine-hosted-ui      # 生产部署壳，可私有或延后开源
  Dockerfile
  nginx.conf
  deploy/helm/
  package.json             # 消费 agentengine-ui-core，构建 /chat/ bundle

ksadk-python               # Python SDK，可开源
  ksadk/server/static/     # 由 agentengine-ui-core 构建出的内嵌产物
  ksadk/server/app.py      # 本地服务静态产物和 API
```

关键规则：

- UI 源码唯一真源是 `agentengine-ui-core`，不再是 `agentengine-hosted-ui` 或 `ksadk-python/ksadk/server/web-ui`。
- `agentengine-hosted-ui` 负责生产部署差异：`/chat/` base path、nginx、Docker、Helm、内部镜像仓库。
- `ksadk-python` 不保留可编辑的 UI 源码副本，只保留构建产物和生成清单。
- SDK wheel/sdist 应包含 `ksadk/server/static`，确保用户 `pip install ksadk` 后不需要 Node 也能启动本地 UI。

### 2.2 本地部署能力

开源版本需要支持两种本地部署方式：

1. SDK 内嵌模式：
   - `pip install ksadk`
   - `agentengine web`
   - 直接使用 wheel 内置 `ksadk/server/static`

2. UI 开发模式：
   - 克隆 `agentengine-ui-core`
   - `npm ci`
   - `npm run dev`
   - 通过 Vite proxy 连接本地 `ksadk.server.app`

生产 Hosted UI 的 Helm/K8s 发布不应成为外部开发者运行 SDK 的前置条件。

## 3. 开源边界

### 3.1 应开源

- Python SDK 核心：
  - `ksadk/`
  - `ksadk_runtime_common/`
  - CLI、本地 server、runner、session、workspace、memory、toolset、MCP/A2A 基础能力
- 通用 runtime 适配：
  - ADK、LangChain、LangGraph、DeepAgents 等公开框架适配
  - Hermes/OpenClaw 的公开 contract、模板和本地调试能力
- 文档：
  - README、安装、快速开始、CLI、OpenAI-compatible runtime API、runner 上下文、workspace、memory、observability
- 测试：
  - 单元测试、集成测试、本地 E2E、协议 contract 测试
- 中性 UI core：
  - 通用聊天、session、workspace、artifact、streaming、feedback、terminal UI 能力

### 3.2 不应开源或需脱敏后再评估

- 内部 K8s 集群、kubeconfig、Helm values 中的真实域名和内网路由。
- 内部镜像仓库地址、KCE/KCR 项目名、真实 release tag 策略。
- Skill Service、Sandbox Service、AgentEngine Server 控制面的私有实现。
- 客户信息、真实 trace、真实日志、真实 token、cookie、AK/SK、临时下载 URL。
- 未确认授权的品牌素材、截图、图标、字体、三方图片。
- 内部平台运维 SOP、线上故障细节、未公开网络拓扑。

## 4. 任务分解

### Phase 0：开源决策与治理

目标：在动大规模代码前确定开源策略，避免技术实现和合规结论冲突。

- [ ] 确认开源许可证：Apache-2.0、MIT 或公司标准许可证。
- [ ] 确认公开组织和仓库名：例如 `kingsoftcloud/ksadk-python`、`kingsoftcloud/agentengine-ui-core`。
- [ ] 确认商标和品牌使用规则：`ksadk`、`AgentEngine`、logo、截图是否可公开。
- [ ] 确认安全披露流程：安全邮箱、漏洞上报 SLA、`SECURITY.md`。
- [ ] 确认开源支持范围：哪些 issue 会维护，哪些平台服务不承诺开源支持。

交付物：

- `LICENSE`
- `SECURITY.md`
- `CONTRIBUTING.md`
- 公开 README 支持范围说明

### Phase 1：敏感信息和内部依赖清理

目标：确保公开仓库不含内部凭证、内网细节和不可复现依赖。

- [ ] 对全仓运行 secret scan，覆盖源码、测试、docs、snapshot、HTML、SVG、JSON、YAML。
- [ ] 清理 README、CHANGELOG、docs 中的内网域名、真实镜像地址、真实账号、真实 token、真实客户信息。
- [ ] 将内部地址改为占位符，例如 `<AGENTENGINE_API_BASE>`、`<K8S_NAMESPACE>`、`<IMAGE_REPOSITORY>`。
- [ ] 将内部发布说明移动到私有发布手册，不进入公开仓库。
- [ ] 检查 `.gitignore`、`.dockerignore`、`MANIFEST.in`、`pyproject.toml`，避免把缓存、构建产物、私有配置打包出去。
- [ ] 检查测试 fixture，确保只使用假数据或公开示例。

建议命令：

```bash
cd /Users/xiayu/kingsoft/code/agent-sdk/agentengine/ksadk-python
rg -n "AKIA|SECRET|TOKEN|COOKIE|kubeconfig|ksyun|kce|kcr|internal|corp|password|AccessKey|SecretKey" .
git grep -n "http://" -- .
git grep -n "https://" -- .
```

验收标准：

- 公开文档不包含真实内网域名、凭证、客户信息。
- secret scan 无高危发现；保留项必须有解释和脱敏证明。

### Phase 2：中性 UI core 拆分

目标：消除 UI 源码双写，让 SDK 本地 UI 和生产 Hosted UI 从同一份源码构建。

- [ ] 新建 `agentengine-ui-core` 仓库或 monorepo package。
- [ ] 从 `agentengine-hosted-ui/src` 迁移通用 UI 源码、测试、Vite 配置、public assets。
- [ ] 删除或冻结 `ksadk-python/ksadk/server/web-ui/src`，明确不再接受功能改动。
- [ ] 在 UI core 中支持两个构建目标：
  - `npm run build:hosted`：`VITE_BASE_PATH=/chat/`，供 `agentengine-hosted-ui` 使用。
  - `npm run build:ksadk`：`VITE_BASE_PATH=./`，供 `ksadk-python/ksadk/server/static` 使用。
- [ ] 生成 `ksadk-static-manifest.json`，记录 UI core git commit、构建时间、base path、构建命令。
- [ ] 在 `ksadk-python` 增加检查命令，验证 `ksadk/server/static` 与 UI core 构建产物一致。
- [ ] 更新 docs，说明 UI 修改必须进入 UI core，再由两个消费者构建。

推荐同步模型：

```text
agentengine-ui-core
  npm run build:ksadk
  dist-ksadk/
        |
        v
ksadk-python/ksadk/server/static

agentengine-ui-core
  npm run build:hosted
  dist-hosted/
        |
        v
agentengine-hosted-ui Docker image
```

验收标准：

- `agentengine-hosted-ui` 不再直接拥有通用 UI 源码，只拥有部署壳。
- `ksadk-python` 不再从 `ksadk/server/web-ui` 构建 UI。
- 本地 `agentengine web` 和生产 `/chat/` 使用同一 UI core commit。
- CI 能阻止 `ksadk/server/static` 漂移。

### Phase 3：SDK 包结构和安装体验

目标：外部用户可以稳定安装、运行和调试。

- [ ] 审计 `pyproject.toml` dependencies 和 extras，避免默认安装面过大。
- [ ] 明确 extras：
  - `ksadk[adk]`
  - `ksadk[langgraph]`
  - `ksadk[langchain]`
  - `ksadk[deepagents]`
  - `ksadk[skills]`
  - `ksadk[all]`
- [ ] 确认 `ksadk/server/static/**/*` 被 wheel 和 sdist 正确包含。
- [ ] 清理或改造内部默认镜像、内部默认 endpoint、内部对象存储配置。
- [ ] 提供 `.env.example`，只包含公开可理解配置项。
- [ ] 让本地最小示例不依赖公司内部服务。
- [ ] 确认 `agentengine web` 在干净虚拟环境中可启动。

验收命令：

```bash
cd /Users/xiayu/kingsoft/code/agent-sdk/agentengine/ksadk-python
uv build
python -m twine check dist/*
python -m venv /tmp/ksadk-open-source-smoke
source /tmp/ksadk-open-source-smoke/bin/activate
pip install dist/*.whl
agentengine --help
agentengine web --help
```

### Phase 4：文档体系重写

目标：公开文档能让外部开发者独立完成从安装到本地运行的闭环。

- [ ] 重写 README：
  - 项目定位
  - 安装
  - 5 分钟 quickstart
  - 支持框架
  - OpenAI-compatible runtime API
  - 本地 UI
  - 贡献方式
- [ ] 拆分公开文档和内部文档：
  - `docs/` 只放可公开内容。
  - `docs/internal/` 不进入公开发布，或迁移到私有仓库。
- [ ] 更新 API 文档：
  - `/v1/responses` 是默认主协议。
  - `/v1/chat/completions` 是兼容协议。
  - `attachments/current_attachments/has_current_files` 是 KsADK runner 扩展，不冒充 OpenAI 官方字段。
- [ ] 更新 UI 文档：
  - UI core 是源码真源。
  - `ksadk/server/static` 是派生产物。
  - 本地开发和生产部署分别如何构建。
- [ ] 删除或脱敏内部部署文档中的真实环境、真实域名和真实版本号。

验收标准：

- 新用户只看 README 和 docs 可以完成本地 quickstart。
- PyPI README 渲染通过。
- Mermaid 或相对链接不会破坏 PyPI 页面。

### Phase 5：测试、CI 和真实 E2E

目标：公开仓库中的质量门禁可复现，内部发布仍保留真实环境 E2E。

公开 CI 建议包含：

- Python lint / type check。
- Python unit tests。
- package build。
- wheel install smoke。
- UI core unit tests。
- UI core `build:ksadk`。
- `ksadk/server/static` drift check。

内部 CI 建议包含：

- 真实 `/v1/responses` E2E。
- 真实 `/v1/chat/completions` E2E。
- Hosted UI 浏览器上传图片/文件 E2E。
- 预发部署后 smoke。
- OpenAI 双协议 contract 回归。

本地关键命令：

```bash
cd /Users/xiayu/kingsoft/code/agent-sdk/agentengine/ksadk-python
uv run pytest -q
uv run pytest tests/test_openai_protocol_e2e.py -q
uv build

cd /Users/xiayu/kingsoft/code/agent-sdk/agentengine/agentengine-ui-core
npm ci
npm test
npm run build:ksadk
npm run build:hosted
npm run check:ksadk-static
```

验收标准：

- 公开 CI 不依赖内部凭证也能通过。
- 内部预发 E2E 覆盖真实网关、server、runtime、Hosted UI。
- 发布说明必须列出真实执行过的验证，不写“应该可用”。

### Phase 6：发布和版本策略

目标：公开版本、内部平台版本和 UI core 版本有可追踪关系。

- [ ] 确定 `ksadk-python` 语义化版本策略。
- [ ] 确定 UI core 版本策略，建议独立版本或 git commit pin。
- [ ] `ksadk-static-manifest.json` 记录 UI core commit。
- [ ] `agentengine-hosted-ui` 镜像 tag 记录 UI core commit。
- [ ] CHANGELOG 分为公开条目和内部条目，公开条目不泄漏内部服务细节。
- [ ] 发布前执行 TestPyPI 或内部等价 dry run。

建议版本映射：

```text
ksadk-python v0.x.y
  embeds agentengine-ui-core commit <sha>

agentengine-hosted-ui image v0.x.y-hosted-ui-<sha>
  builds agentengine-ui-core commit <sha>
```

## 5. 风险和决策点

### 5.1 是否公开 UI core 源码

建议公开。理由：

- SDK 内嵌静态 UI 如果没有对应源码，开源审计和贡献体验都不好。
- 生产 Hosted UI 和 SDK 本地 UI 可以保持一致。
- 外部用户可以本地开发和调试 UI。

风险：

- UI 中可能含内部产品文案、图标、接口假设。
- 需要额外维护公开构建链路。

缓解：

- 把生产部署壳留在 `agentengine-hosted-ui`，UI core 只放中性能力。
- 对内部能力使用 capability-driven 展示，默认隐藏不可用入口。

### 5.2 是否提交 `ksadk/server/static`

建议提交或随 release artifact 生成并打包，但必须有 manifest 和 drift check。

理由：

- Python 用户不应被迫安装 Node 才能使用本地 UI。
- wheel/sdist 需要自包含本地 UI。

风险：

- 构建产物 diff 很大，review 噪音高。

缓解：

- UI 代码 review 在 UI core 完成。
- `ksadk-python` 只 review manifest 和必要产物更新。
- CI 强制产物与 UI core commit 一致。

### 5.3 是否继续保留 `ksadk/server/web-ui`

建议不保留为源码真源。

可选路径：

1. 直接删除 `ksadk/server/web-ui/src`，只保留 `ksadk/server/static`。
2. 短期保留目录但加 README 标记 deprecated，并从 Makefile/CI 移除构建入口。

推荐先走路径 2，等 UI core 和同步链路稳定后删除。

## 6. 初始里程碑

### Milestone A：开源审计草案

- 完成 License / SECURITY / CONTRIBUTING 草案。
- 完成敏感信息扫描清单。
- 完成公开/内部文档分层清单。

### Milestone B：UI core POC

- 从 `agentengine-hosted-ui` 抽出 `agentengine-ui-core`。
- 构建 `dist-hosted` 和 `dist-ksadk`。
- `ksadk-python/ksadk/server/static` 可由 UI core 生成。
- 本地 `agentengine web` 可打开并完成图片/文件上传 E2E。

### Milestone C：ksadk 开源候选

- README 和 docs 完成公开版重写。
- 默认安装和 extras 清晰。
- wheel/sdist 构建和安装 smoke 通过。
- 公开 CI 通过。
- 内部预发 E2E 通过。

### Milestone D：首次公开发布

- 打 tag。
- 发布 GitHub release。
- 发布 PyPI/TestPyPI 或正式 PyPI。
- 发布公告只包含公开能力和已验证事项。

## 7. 开源前验收清单

- [ ] License 已批准并提交。
- [ ] SECURITY / CONTRIBUTING 已提交。
- [ ] README 不含内部域名、真实凭证、客户信息。
- [ ] docs 中内部内容已迁移或脱敏。
- [ ] secret scan 无未解释高危发现。
- [ ] UI core 是唯一 UI 源码真源。
- [ ] `ksadk/server/static` 有 manifest，可追踪 UI core commit。
- [ ] `ksadk/server/web-ui` 不再作为构建入口。
- [ ] `uv run pytest -q` 通过。
- [ ] OpenAI 双协议真实 E2E 通过。
- [ ] Hosted UI 浏览器上传图片/文件 E2E 通过。
- [ ] `uv build` 和 `twine check` 通过。
- [ ] 干净虚拟环境安装 wheel 后 `agentengine web` 可启动。
- [ ] 公开 CI 不依赖内部凭证。
- [ ] 内部发布流程和公开发布流程分离。

## 8. 后续实施建议

建议先做两个小切口，不要一次性大爆炸：

1. UI core POC：验证源码真源拆分和 `ksadk/server/static` 单向生成。
2. 开源审计清单：跑 secret scan 和文档敏感信息扫描，列出必须脱敏项。

这两个切口完成后，再决定是否进入完整开源迁移。这样能尽早暴露最大风险：UI 分叉、内部依赖、公开文档不可复现。
