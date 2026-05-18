# Skill Runtime E2E 说明

## 1. 分层

Sandbox 是通用隔离运行底座，Skill Runtime 只是它的一个上层使用方。

当前 E2E 链路是：

```text
ADK Runner
  └─ execute_skills(workflow_prompt)
      └─ ksadk.skills.runtime
          └─ ksadk.sandbox E2B backend
              └─ 沙箱控制台模板
                  └─ /home/ksadk/agent.py
```

沙箱产品控制台底层基于 E2B，目前支持 AIO、Code、Browser 三类模板。因此 KsADK 执行链路优先使用 E2B SDK。Sandbox Open API / KOP 继续作为模板与实例管理接口，后续在确认 commands/files 能力后再作为独立 backend 或控制面集成。

## 2. Runtime 配置

通用 Sandbox 变量：

- `KSADK_SANDBOX_BACKEND`：通用 sandbox backend，当前支持 `e2b`。
- `KSADK_SANDBOX_TYPE`：`aio`、`code`、`browser` 或 `private`。Skill Runtime 默认使用 `aio`。
- `KSADK_SANDBOX_TEMPLATE_ID`：沙箱控制台创建的模板 ID，新部署优先使用该变量。
- `KSADK_SANDBOX_TIMEOUT`：sandbox 会话超时秒数。
- `KSADK_SANDBOX_ALLOW_INTERNET_ACCESS`：是否允许 sandbox 会话出网。
- `E2B_API_URL`：E2B 兼容 manager endpoint。
- `E2B_API_KEY`：E2B API key，只能通过 Secret 或本地 shell 环境变量注入。

Skill Runtime 变量：

- `KSADK_SKILL_SPACE_IDS`：逗号分隔的 Skill Space id。
- `SKILL_SPACE_ID`：单 space 兼容变量。
- `KSADK_SKILLS_MODE`：`auto`、`local` 或 `sandbox`。
- `KSADK_SKILL_RUNTIME_BACKEND`：`e2b`、`local_process` 或 `disabled`。
- `KSADK_SKILL_RUNTIME_TEMPLATE_ID`：`KSADK_SANDBOX_TEMPLATE_ID` 的兼容别名。
- `KSADK_SKILL_RUNTIME_TIMEOUT`：workflow 超时秒数。
- `KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS`：远程 Skill Runtime 是否允许出网的兼容变量。
- `KSADK_SKILL_WORKDIR`：sandbox agent 内部执行 workflow 的工作目录。
- `KSADK_SKILL_ARTIFACT_PROJECT`：最小 artifact workflow 的项目目录名。
- `KSADK_SKILL_SERVICE_URL`：Skill Service API base URL。
- `KSADK_SKILL_SERVICE_TOKEN`：可选 bearer token，只能作为 secret 注入。
- `KSADK_SKILL_SERVICE_ACCESS_KEY` / `KSADK_SKILL_SERVICE_SECRET_KEY`：Skill Service KOP 签名凭证。本地直连可 fallback 到 `KSYUN_ACCESS_KEY` / `KSYUN_SECRET_KEY`，sandbox 执行建议显式注入 skill-service-scoped 变量。
- `KSADK_SKILL_SERVICE_ACCOUNT_ID`：Skill Service 租户账号 ID。
- `KSADK_SKILL_SERVICE_API_VERSION`：Skill Center KOP API 版本。预发 Skill Center 当前使用 `2024-06-12`，不要复用 Sandbox KOP 的 `2026-04-01`。

当前实现通过 `ksadk.sandbox` 调用 E2B SDK。Sandbox KOP 中的 `GetSandboxInstanceList` 等接口应作为未来 `ksyun_sandbox_kop` backend 或控制面集成，不和 E2B SDK backend 混在一起。

预发 Skill Center 直连 REST 地址可用于 OpenAPI 校验：

```text
https://agent-api-pre.kspmas-internal.ksyun.com/agentengine/skill/api/v1
```

内部 AICP 网关需要 KOP 签名：

```text
KSADK_SKILL_SERVICE_URL=http://maicp.inner.api.ksyun.com
KSADK_SKILL_SERVICE_API_VERSION=2024-06-12
```

KOP 模式下，Skill Center 已注册 `ListSkillsBySpaceId` action。KsADK 直接调用该 action，参数是 `SpaceId`。直连 REST 同样使用 `ListSkillsBySpaceId`，参数也是 `SpaceId`。

## 3. Template Gate

完整 E2B E2E 需要沙箱团队或控制台先提供 template id。Skill Runtime 默认使用 AIO 模板，除非确认 skill 只需要 Code 或 Browser 能力。

模板镜像必须包含 KsADK runtime agent：

```text
/home/ksadk/agent.py
```

源码入口：

```text
ksadk/skills/runtime/agent.py
```

面向沙箱团队的镜像入口：

```text
deploy/skill-runtime/agent.py
```

构建镜像时把它复制为 `/home/ksadk/agent.py`。该文件只是 `ksadk.skills.runtime.agent` 的薄 wrapper，方便 SDK 内部实现演进时保持镜像契约稳定。

首个 `web-artifacts-builder` 验证需要镜像具备：

- Python 3.10+，并安装当前 KsADK。
- Bash。
- Node.js 18+。
- npm。
- 能出网安装 pnpm 和前端依赖，或者镜像预装相关依赖。

## 4. Fixture Skill

本地 Skill Service fixture：

```text
/Users/xiayu/Downloads/web-artifacts-builder.zip
```

期望 SHA256：

```text
b95f0735357fcf879bd53ed85cb242679ec74438e3bc8e85b1f27193169b6ecf
```

它用于预发 Skill Service 首个种子数据和 E2E 验证。运行 `execute_skills` 前，需要确认 `ListSkillsBySpaceId` 返回 `SkillId`、`VersionId`、`Version`、`ContentHash`、`ArchiveUri`。

## 5. 已验证的预发 Skill Center 信息

2026-05-12 已在预发管理集群验证：

- Namespace：`agent-manager`
- Service：`skill-service-skill-service`
- Cluster service port：`8000`
- Ingress host/path：`agent-api-pre.kspmas-internal.ksyun.com/agentengine/skill`
- Image：`hub.kce.ksyun.com/cbd-serverless/skill-service:0.0.1-pre`

直连 REST 服务端需要 `X-Ksc-Account-Id` 做租户隔离。不带该 header 时，`ListSkillSpaces` 和 `ListSkills` 可能返回空租户视图。

已验证的直连 REST 流程：

1. `GET GetSkillUploadUrl` 返回 `web-artifacts-builder.zip` 上传 URL。
2. 使用 `Content-Type: application/octet-stream` PUT 上传成功。
3. `POST CreateSkillSpace` 创建临时 `ksadk_e2e_*` space。
4. `POST CreateSkillVersion` 创建 `web-artifacts-builder` 的 `v5` 版本。
5. `GET ListSkillsBySpaceId` 返回 `SkillId`、`VersionId`、`Version`、`ContentHash`、`ArchiveUri`。
6. `GET GetSkillDownloadUrl` 返回 1 小时下载 URL，下载 bytes 的 sha256 等于本地 fixture：`b95f0735357fcf879bd53ed85cb242679ec74438e3bc8e85b1f27193169b6ecf`。
7. 临时 space/version 已通过 `RemoveSkillFromSpace`、`DeleteSkillVersion`、`DeleteSkillSpace` 清理。

已修复的上游数据问题：预发 Skill Service 曾出现 `ContentHash` 与实际下载 zip sha256 不一致。`web-artifacts-builder v5` 示例中，`ListSkillsBySpaceId` 曾返回 `4e5062c146a021b1a5de6a9844b8f5c8165d95fd479604bacf96a080cf0a6218`，实际下载 zip sha256 是 `b95f0735357fcf879bd53ed85cb242679ec74438e3bc8e85b1f27193169b6ecf`。该问题已由上游修复；KsADK 仍必须继续 fail closed，不能绕过 hash 校验。

## 6. 预发 E2E 流程

1. 从 Secret 或本地环境变量设置 `E2B_API_URL`、`E2B_API_KEY`、`KSADK_SANDBOX_TEMPLATE_ID`、`KSADK_SKILL_SERVICE_URL`、`KSADK_SKILL_SPACE_IDS`。`KSADK_SKILL_RUNTIME_TEMPLATE_ID` 仍作为兼容别名可用。
2. 先跑 E2B preflight：`Sandbox.list()`，或创建短生命周期 sandbox 执行 `echo ok`。
3. 验证 Skill Service 能返回并下载 `web-artifacts-builder.zip`。
4. 运行 ADK 测试 Agent，设置 `KSADK_SKILLS_MODE=sandbox` 和 `KSADK_SKILL_RUNTIME_BACKEND=e2b`。
5. 调用 `execute_skills("使用 web-artifacts-builder 初始化并打包一个最小 artifact")`。
6. 验收 stdout/stderr、exit code、runtime id、超时行为、`bundle.html` 产物和 sandbox cleanup。

不要在日志、提交文件、测试 fixture 或 snapshot 中记录 `E2B_API_KEY`。
