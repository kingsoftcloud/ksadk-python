# KsADK Skill Runtime 研发设计方案

> 目标：评审并收敛“KsADK 是否要参考 VeADK/AgentKit 接入 Skill 中心”的方案，给出可落地的研发设计。
> 核验时间：2026-05-12。
> 核验来源：本地 `veadk-python`、`agentkit-samples`、`ksadk-python` 当前代码，以及 Skill Service OpenAPI
> `http://agent-api-pre.kspmas-internal.ksyun.com/agentengine/skill/api/v1/openapi.json`。

## 友商设计分析

KsADK 值得接入 Skill 中心，但不要照搬 VeADK 的全部形态。

建议做成两层能力：

1. P0：Skill 发现、下载、缓存、加载为运行时工具。用户在 `agentengine.yaml` 或部署参数里声明 Skill Space，KsADK 在创建/更新 Agent 时把 Skill Space ID 写入环境变量，运行时 Runner 自动读取并注入工具。
2. P1：Skill 注册、更新版本、发布到 Skill Space。当前版本暂不做 KsADK CLI 管理面，只保留为后续可选能力。

不建议 P0 阶段强行做完整“沙箱执行 Agent”。VeADK 的 `execute_skills` 依赖火山 `InvokeTool/RunCode` 沙箱；KsADK 当前还缺少明确的金山云沙箱 SDK 契约。可以先把 local/runtime 模式打通，沙箱模式等执行服务契约确定后再接。

但这不意味着沙箱相关产物都阻塞。**镜像内最小 skills agent 必须提前由 KsADK 产出**，作为沙箱团队构建默认镜像的输入；真正阻塞的是外层 `execute_skills` 如何调用远程 RunCode SDK、日志/超时/结果如何回传等集成细节。

## 关键修正

### 1. Skill Space ID 由 KsADK 部署链路下发

Skill Space ID 不应要求业务代码手写，也不应要求用户每轮请求传入。正确链路是：

```text
用户配置/CLI 参数
  -> agentengine deploy / agentengine agent update
  -> CreateAgent / UpdateAgent payload.env_vars
  -> Agent Pod 环境变量
  -> KsADK Runner / runtime adapter 读取
  -> 自动注入 skill 工具
```

建议环境变量：

| 变量 | 说明 |
|------|------|
| `KSADK_SKILL_SPACE_IDS` | KsADK 主变量，逗号分隔多个 Skill Space ID |
| `SKILL_SPACE_ID` | 兼容 VeADK/AgentKit 示例的单 Space 变量，可由 KsADK 同步写入第一个 ID |
| `KSADK_SKILLS_MODE` | `auto` / `local` / `sandbox`，默认 `auto` |
| `KSADK_SKILL_SERVICE_URL` | Skill Service API 地址 |
| `KSADK_SKILL_CACHE_DIR` | Skill 包下载和解压缓存目录 |

用户侧声明建议：

```yaml
# agentengine.yaml
skills:
  spaces:
    - ss-abc123
  mode: auto
```

CLI 可以补充：

```bash
agentengine deploy . --skill-space ss-abc123
agentengine agent update <agent-id> --skill-space ss-abc123
```

部署层需要做的事不是注入 Python 工具，而是把上述配置持久化到 Agent 环境变量。

### 2. 自动注入主要放在 Runner 层，不放在 Gateway 层

Gateway/控制面适合做：

- 保存 `skills.spaces` 配置；
- 在 CreateAgent/UpdateAgent 时写入 env vars；
- 在 Hosted UI bootstrap 或诊断接口里展示当前 Skill 配置；
- 必要时给运行时提供内部鉴权或下载代理。

Gateway 不适合做：

- 动态修改用户 Python 代码；
- 给已经编译好的 LangGraph graph 强行追加 tools；
- 在请求转发时临时拼接 skill 工具定义。

Runner/运行时适配层适合做：

- 读取 `KSADK_SKILL_SPACE_IDS`；
- 调 Skill Service 发现可用 skills；
- 构造 `skills_tool` / `read_file` / `write_file` / `edit_file` / `bash` 等工具；
- 根据框架能力追加 tools 或提供显式 helper。

不同运行目标的适配边界：

| 运行目标 | 注入位置 | 说明 |
|----------|----------|------|
| ADK Runner | `ksadk.runners.adk_runner.ADKRunner` | 可在 `load_agent()` 后追加 ADK Tool / Toolset，最适合先做 |
| LangChain Runner | Runner 或用户显式 helper | 只有 agent 暴露可变 tools 时才能自动追加；否则提供 `create_langchain_skill_tools()` |
| LangGraph / DeepAgents | 优先用户显式 helper | 多数 graph 已编译，Runner 运行时强行注入不可靠；提供 `create_langgraph_skill_tools()`，让用户在 compile 前加入 |
| Generic runtime pod | KsADK Runner | 如果请求确实经过 KsADK Python runtime，可以在 Runner 层处理 |
| OpenClaw | OpenClaw 镜像/启动脚本/plugin adapter | 不是 KsADK Python Runner 执行业务图，不能只靠通用 Runner 注入 |
| Hermes | Hermes runtime adapter | 通过 Hermes 镜像/入口/terminal skill 体系适配，不走 ADK Toolset 自动注入 |

因此 P0 的自动注入范围应定义为：先支持 ADK Runner 自动注入；LangChain 做 best-effort；LangGraph/DeepAgents 给 helper 和文档；OpenClaw/Hermes 单独走运行时模板适配。

### 3. Skill Service 接口已确认约束

当前已经确认：

- `ListSkillsBySpaceId` 已补齐 `SkillId`、`VersionId`、`Version`。
- `ContentHash` 是 sha256。
- `GetSkillDownloadUrl` 返回的预签名 URL 有效期是一小时。
- `GetSkillDownloadUrl` 入参必须是：`SkillId`、`VersionId`。

因此 P0 推荐运行时链路是：

```text
ListSkillsBySpaceId(SkillSpaceId)
  -> Skills[] includes SkillId, VersionId, Version, ContentHash, ArchiveUri
GetSkillDownloadUrl(SkillId, VersionId)
  -> DownloadUrl
HTTP GET DownloadUrl
  -> skill zip
safe extract
  -> read SKILL.md
```

`ListSkillsBySkillSpace` 仍可作为管理/关系视图接口，但 KsADK 运行时可以直接使用已补齐字段后的 `ListSkillsBySpaceId`。

### 4. 期望的 `ListSkillsBySpaceId` 返回 JSON

按渐进式披露原则，列表接口只做“Skill 目录/索引”，不批量生成临时下载 URL。运行时先让模型看到 `Name/Description`，只有模型实际调用某个 skill 时，KsADK 再用 `SkillId + VersionId` 调 `GetSkillDownloadUrl`。

期望响应：

```json
{
  "Code": 0,
  "Message": "OK",
  "RequestId": "req-2026051212000000000001",
  "Data": {
    "SkillSpaceId": "ss-abc123",
    "SkillSpaceName": "office-productivity",
    "Skills": [
      {
        "SkillId": "sk-pdf001",
        "VersionId": "sv-pdf001-v3",
        "Version": "v3",
        "Name": "pdf-processing",
        "Description": "PDF processing skill, supports text extraction, split, merge and watermark operations.",
        "Status": "Active",
        "ContentHash": "sha256:9b3f0f2e6c0d8a...",
        "ArchiveUri": "ks3://agentengine-skills/skills/sk-pdf001/v3/pdf-processing.zip"
      },
      {
        "SkillId": "sk-docx001",
        "VersionId": "sv-docx001-v2",
        "Version": "v2",
        "Name": "docx-editing",
        "Description": "DOCX editing skill for structured document rewrite and formatting.",
        "Status": "Active",
        "ContentHash": "sha256:6c4b1a0e8d9c2f...",
        "ArchiveUri": "ks3://agentengine-skills/skills/sk-docx001/v2/docx-editing.zip"
      }
    ]
  }
}
```

字段要求：

| 字段 | 必需 | 说明 |
|------|------|------|
| `SkillId` | 是 | Skill 主键，用于后续下载、诊断、trace |
| `VersionId` | 是 | Skill Space 当前生效版本主键，`GetSkillDownloadUrl` 必需 |
| `Version` | 是 | 人可读版本名，用于展示和诊断 |
| `Name` | 是 | 模型调用 `skills_tool` 时使用的稳定名称 |
| `Description` | 是 | 给模型做 skill 选择 |
| `Status` | 建议 | 运行时可过滤非 Active skill |
| `ContentHash` | 是 | 缓存命中和变更检测 |
| `ArchiveUri` | 建议 | 稳定对象存储定位，用于诊断；运行时下载仍走 `GetSkillDownloadUrl` |

不建议在列表接口默认返回 `DownloadUrl`。`DownloadUrl` 是短期预签名地址，有 TTL，列表里 100 个 skill 大多不会被使用。正确成本模型是：

```text
1 次 ListSkillsBySpaceId
+ N 次 GetSkillDownloadUrl
```

其中 `N` 是本轮实际使用的 skill 数量，通常是 1 到 3 个，而不是 Skill Space 中的总 skill 数。

## VeADK/AgentKit 可借鉴点

本地代码核验到的 VeADK 行为：

- Skill 是目录，不是单个文件。目录内以 `SKILL.md` 为入口，可包含 `scripts/` 和其他资源。
- `veadk.skills.skill.Skill` 只保留 name、description、path、skill_space_id、bucket_name、checklist、id 等轻量元数据。
- `SkillsToolset` 在 `local` 模式暴露 7 类工具：`skills_tool`、`read_file`、`write_file`、`edit_file`、`bash`、`register_skills`、`update_check_list`。
- `skills_sandbox` / `aio_sandbox` 模式不暴露 local 工具，而是由外层 `execute_skills` 调远程沙箱执行。
- `skills_tool` 被调用时才下载/挂载具体 skill，并把 `SKILL.md` 内容和 base directory 返回给模型。
- `execute_skills` 通过 `InvokeTool` + `OperationType=RunCode` 在远端沙箱执行 `/home/gem/veadk_skills/agent.py`。

值得借鉴：

- `SKILL.md + scripts/` 的目录协议；
- “先发现元数据，按需下载完整包”的懒加载；
- local 模式和 sandbox 模式分离；
- session 级工作目录隔离；
- 将 Skill 的可用列表写进工具描述，让模型自己选择是否加载。

不建议照搬：

- 把所有框架都假设为 ADK Toolset；
- 默认把 `register_skills` 暴露给普通业务 Agent；
- 在沙箱接口未确定前先实现一套伪 `RunCode`；
- 依赖单一 `SKILL_SPACE_ID`，不支持多 space 和部署配置持久化。

## Skill Service 当前能力

OpenAPI 当前可见端点共 23 个业务端点加 health/demo，核心为 21 个 Skill 相关 API。

### SkillSpace

| API | Method | 用途 |
|-----|--------|------|
| `CreateSkillSpace` | POST | 创建 Skill Space |
| `GetSkillSpace` | GET | 获取 Skill Space |
| `ListSkillSpaces` | GET | 分页查询 Skill Space，支持 `Name` 过滤 |
| `DeleteSkillSpace` | POST | 删除 Skill Space |
| `UpdateSkillSpace` | POST | 更新名称/描述 |
| `AddSkillsToSkillSpace` | POST | 批量绑定 skill version |
| `RemoveSkillFromSpace` | POST | 移除绑定 |
| `UpdateSkillSpaceSkillVersion` | POST | 切换 Space 内某 skill 的生效版本 |

### Skill

| API | Method | 用途 |
|-----|--------|------|
| `CreateSkill` | POST | 从 `SourceUrl` 创建 Skill 初始版本 |
| `GetSkill` | GET | 获取 Skill 详情 |
| `ListSkills` | GET | 分页查询 Skill，支持 `Name` 过滤 |
| `UpdateSkill` | POST | 更新 Skill 元信息 |
| `DeleteSkill` | POST | 删除 Skill |
| `ListSkillSpacesBySkill` | GET | 查询某 Skill 所属 Space |
| `ListSkillsBySkillSpace` | GET | 管理/关系视图，可查看 Space 内 skill 与版本绑定 |
| `ListSkillsBySpaceId` | GET | 运行时推荐发现接口，已补齐 `SkillId/VersionId/Version` |

### SkillVersion 与对象存储

| API | Method | 用途 |
|-----|--------|------|
| `CreateSkillVersion` | POST | 给已有 Skill 创建新版本 |
| `ListSkillVersions` | GET | 分页列出版本 |
| `GetSkillVersionTree` | GET | 查询 Skill -> Versions 树 |
| `DeleteSkillVersion` | POST | 删除版本 |
| `GetSkillUploadUrl` | GET | 获取 KS3 预签名上传 URL |
| `GetSkillDownloadUrl` | GET | 获取 KS3 预签名下载 URL |

关键 schema：

| Schema | 关键字段 |
|--------|----------|
| `RuntimeSkillMetadataResponseSchema` | `SkillId`, `Name`, `Status`, `Description`, `VersionId`, `Version`, `ContentHash`, `ArchiveUri` |
| `SkillsBySpaceIdItemSchema` | `SkillId`, `VersionId`, `Version`, `Name`, `Description`, `ContentHash`, `ArchiveUri` |
| `SkillDownloadUrlDataSchema` | `DownloadUrl` |
| `SkillUploadUrlDataSchema` | `Bucket`, `Region`, `ObjectKey`, `Ks3Url`, `UploadPath` |
| `PackageSourceType` | `UPLOAD_ZIP`, `KS3`, `LOCAL_FILE` |

OpenAPI 当前没有声明 `securitySchemes`，所以客户端签名方式不能在 KsADK 文档里假设为某个 HMAC 版本。这里需要和 Skill Service 同事确认真实鉴权：Bearer、网关注入身份、AK/SK、STS 临时凭证，还是内网免签。

## KsADK 目标架构

```text
agentengine.yaml / CLI
  skills.spaces = [ss-xxx]
          |
          v
KsADK deploy / update
  env_vars:
    KSADK_SKILL_SPACE_IDS=ss-xxx
    SKILL_SPACE_ID=ss-xxx
          |
          v
Agent runtime pod
          |
          v
KsADK Runner / runtime adapter
  load config from env
  call Skill Service
  build skill registry
  inject supported tools
          |
          v
Model calls skills_tool("pdf-processing")
          |
          v
download zip by SkillId + VersionId
safe extract to session/cache dir
read SKILL.md
return instructions + base directory
          |
          v
Model uses bash/read/write/edit tools to execute scripts
```

### 模块设计

| 模块 | 建议路径 | Phase | 说明 |
|------|----------|-------|------|
| 配置解析 | `ksadk/skills/config.py` | P0 | 从 env / `agentengine.yaml` 解析 spaces、mode、cache dir |
| 数据模型 | `ksadk/skills/models.py` | P0 | `SkillRef`, `SkillPackage`, `SkillRegistry` |
| 本地加载 | `ksadk/skills/local_loader.py` | P0 | 扫描含 `SKILL.md` 的目录 |
| 云端客户端 | `ksadk/skills/service_client.py` | P0 | Skill Service REST client |
| 缓存与解压 | `ksadk/skills/package_store.py` | P0 | 下载 zip、校验 hash、安全解压 |
| 工具定义 | `ksadk/skills/tool_defs.py` | P0 | 框架无关 `ToolDef` |
| ADK 适配 | `ksadk/skills/adk_tools.py` | P0 | 生成 ADK tools/toolset |
| LangChain 适配 | `ksadk/skills/langchain_tools.py` | P1 | 生成 LangChain `StructuredTool` |
| LangGraph 适配 | `ksadk/skills/langgraph_tools.py` | P1 | compile 前 helper |
| CLI 管理 | `ksadk/cli/cmd_skills.py` | P2 | 后续可选；当前版本暂不直接管控 Skill CRUD |
| Skill Runtime 最小 Agent | `ksadk/skills/runtime/agent.py` 或镜像内 `/home/ksadk/agent.py` | P0/P1 | 提供给沙箱团队构建默认镜像 |
| Skill Runtime 后端适配 | `ksadk/skills/runtime/backends/e2b.py` | P2 | 先接 E2B-compatible 沙箱，后续可扩展其他 runtime |

### SkillRef 数据模型

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class SkillRef:
    name: str
    description: str
    source: str                  # local | skill_service
    path: str | None = None
    skill_space_id: str | None = None
    skill_id: str | None = None
    version_id: str | None = None
    version: str | None = None
    content_hash: str | None = None
    archive_uri: str | None = None
```

云端 Skill 必须有 `skill_id` 和 `version_id`，否则不能调用 `GetSkillDownloadUrl`。

## 工具注入设计

### 框架无关 ToolDef

KsADK 可以先把工具抽象成普通 callable + JSON schema：

```python
@dataclass
class SkillToolDef:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]
```

P0 工具建议：

| Tool | 默认暴露 | 说明 |
|------|----------|------|
| `skills_tool` | 是 | 加载指定 skill，返回 `SKILL.md` 和 base directory |
| `read_file` | 是 | 读取 session/workspace 文件 |
| `write_file` | 是 | 写入文件 |
| `edit_file` | 是 | 精确替换编辑 |
| `bash` | 是，但要受安全策略限制 | 执行 skill 脚本 |
| `register_skills` | 否 | 管理面能力，不默认给普通 Agent |
| `update_checklist` | 可选 | P1 做 session state 持久化后再默认启用 |

`bash` 必须受 KsADK 既有沙箱/执行策略约束，不能因为 Skill 中心接入就绕开命令安全策略。

### ADK 自动注入

`ADKRunner.load_agent()` 已经有自动注入 sandbox、MCP、KB、memory tools 的模式。Skills 可以沿用同一风格：

```text
load_agent()
  -> import root_agent
  -> inject safety prompt
  -> _inject_skill_tools()
  -> _inject_sandbox_tools()
  -> _inject_mcp_toolsets()
```

注意事项：

- 使用 `_append_tools_by_name()` 去重；
- toolset 生命周期放入 `_runtime_toolsets`；
- 注入失败应 warning，不应让无 skills 的普通 Agent 启动失败；
- 只有配置了 skills 时才注入。

### LangChain / LangGraph

LangChain：

- 如果加载到的对象有可变 `tools` 属性，可以 best-effort 追加；
- 如果是任意 Runnable，不应强行改对象，提供 helper：

```python
from ksadk.skills.langchain_tools import create_langchain_skill_tools

tools = [
    *create_langchain_skill_tools(),
    ...
]
```

LangGraph / DeepAgents：

- 编译后的 graph 不能可靠追加工具；
- 提供 compile 前 helper：

```python
from ksadk.skills.langgraph_tools import create_langgraph_skill_tools

tools = [
    *create_langgraph_skill_tools(),
    ...
]
```

Runner 层可以做诊断：发现配置了 `KSADK_SKILL_SPACE_IDS` 但当前 graph 没有 skill tools 时，在日志/healthz 里提示需要 compile 前接入 helper。

## Skill 下载与缓存

下载链路：

```text
SkillRef(skill_id, version_id)
  -> GetSkillDownloadUrl
  -> HTTP GET DownloadUrl
  -> cache zip by content_hash
  -> safe extract
  -> session skills dir symlink/copy
```

安全要求：

- 解压前拒绝绝对路径、`..`、符号链接逃逸；
- zip 解压目录必须在 `KSADK_SKILL_CACHE_DIR` 或 session 工作目录内；
- 云端包必须包含 `SKILL.md`；
- 本地 skill 目录优先只读 symlink，失败再 copy；
- `ContentHash` 命中时复用缓存；
- 记录 `skill.name`、`skill.id`、`skill.version_id`、`skill.space_id` 到 trace attributes。

建议缓存路径：

```text
${KSADK_SKILL_CACHE_DIR:-/tmp/ksadk-skills}/
  packages/<content_hash>.zip
  extracted/<content_hash>/<skill-name>/SKILL.md
  sessions/<session_id>/skills/<skill-name> -> extracted/<content_hash>/<skill-name>
```

## CLI 与部署链路改造

### agentengine.yaml

```yaml
skills:
  spaces:
    - ss-abc123
  mode: auto
```

### deploy/update payload

部署或更新时将配置转为环境变量：

```json
{
  "env_vars": {
    "KSADK_SKILL_SPACE_IDS": "ss-abc123",
    "SKILL_SPACE_ID": "ss-abc123",
    "KSADK_SKILLS_MODE": "auto"
  }
}
```

对于现有 Agent 更新，需要注意：

- update 时不要丢失用户已有 env；
- 新的 skills env 覆盖旧的 skills env；
- 清空 skills 时要显式删除或置空对应 env；
- 本地 `.agentengine.state` 可以记录 skills 配置，便于 status/open/invoke 诊断。

当前版本建议只做部署/更新绑定，不做 Skill CRUD CLI：

```bash
agentengine deploy . --skill-space ss-abc123
agentengine agent update <agent-id> --skill-space ss-abc123
```

`agentengine skills list/register/version` 可作为后续管理面能力，不进入当前版本范围。

## 与 Skill Service 同事确认项

P0 必须确认：

| 问题 | 原因 |
|------|------|
| 鉴权方式是什么 | OpenAPI 没有 `securitySchemes` |
| `ArchiveUri` 格式是否稳定 | 仅展示/诊断，下载仍建议走 `GetSkillDownloadUrl` |
| `CreateSkill` 是否从包内解析 name/description | 影响 CLI 注册入参和错误提示 |

P1 可选增强：

| 建议 | 说明 |
|------|------|
| `ListSkillsBySpaceName` 或稳定 name 精确查询 | 用户配置更适合用 name，但 ID 更稳定 |
| Skill tags | 规模化后用于分类/检索 |
| 包结构校验 API | 上传后返回缺失 `SKILL.md`、非法 frontmatter 等明确错误 |
| 批量下载 URL | 大量 Skill 预热时减少请求 |

## 实施路径

### Phase 0：接口与配置收敛，2-3 天

- 定义 `agentengine.yaml skills` schema；
- deploy/update 写入 `KSADK_SKILL_SPACE_IDS`；
- 确认 Skill Service 鉴权；
- 按已补齐字段后的 `ListSkillsBySpaceId` 实现运行时发现。

验收：

- dry-run payload 能展示 skills env；
- update 不丢 existing env；
- 无 skills 配置时行为完全不变。

### Phase 1：ADK local/runtime 模式，1-2 周

- 实现 `ksadk.skills` 基础模块；
- 支持本地目录和 Skill Service 发现；
- 实现安全下载、缓存、解压；
- ADKRunner 自动注入 `skills_tool`、file tools、bash；
- 产出镜像内最小 skills agent，供沙箱团队构建默认镜像；
- 增加单元测试。

验收：

- `agentengine run` 本地可通过 Skill Space ID 加载 skill；
- `skills_tool("xxx")` 返回 `SKILL.md` 和 base directory；
- 同一个 `ContentHash` 不重复下载；
- zip slip 测试通过；
- 无 Skill Service 凭据/网络失败时错误可诊断。
- `/home/ksadk/agent.py` 可在普通容器内用环境变量启动，读取 Skill Space 并执行 local skills agent。

### Phase 2：多框架适配，1-2 周

- LangChain helper；
- LangGraph/DeepAgents compile 前 helper；
- Runner 层诊断提示；
- 文档和示例。

验收：

- LangChain 示例可用；
- LangGraph 示例在 compile 前接入 tools 可用；
- 配置了 skills 但无法自动注入时有明确提示。

### Phase 3：CLI 管理能力，后续可选

- `agentengine skills list/register/version`；
- 注册时校验 `SKILL.md` frontmatter；
- 上传 zip 走 `GetSkillUploadUrl` + `CreateSkill/CreateSkillVersion`；
- status 展示 Agent 当前绑定的 Skill Space。

验收：

- 能把本地 skill 注册到指定 Space；
- 能创建新版本；
- 能列出 Space 下有效版本；
- 错误信息包含 RequestId。

### Phase 4：Skill Runtime E2B 后端集成

前置条件：

- 沙箱团队提供 E2B-compatible SDK；
- KsADK 确认 SDK endpoint/API key/template id 等部署参数；
- 沙箱镜像已经内置 KsADK 提供的 `agent.py` 和基础 skills。

职责边界：

| 团队 | 负责内容 |
|------|----------|
| 远程沙箱团队 | 提供 E2B-compatible SDK/API、隔离容器、超时、日志、文件系统、网络/文件策略、执行结果返回 |
| KsADK | 提供镜像内最小 skills agent、Skill Service 加载逻辑、工具注入、模型配置读取、skill 执行约定、E2B 适配封装 |

### Skill Runtime E2B 后端对接基线

沙箱团队已说明当前服务遵循 E2B SDK v2 系列主要协议，并推荐：

```bash
pip install "e2b>=2.0.0" "e2b-code-interpreter>=2.0.0"
```

已验证版本：

```bash
pip install e2b==2.15.3 e2b-code-interpreter==2.5.0 python-dotenv==1.2.1 httpx==0.28.1 playwright==1.58.0
```

因此 KsADK 不再定义一套自有 Sandbox 协议。设计基线改为：**直接使用 E2B-compatible SDK，KsADK 只做薄适配层**，把平台环境变量、错误格式、日志截断、trace attributes 和 `execute_skills` tool contract 收敛在 `ksadk.skills.sandbox`。

首版 skills sandbox 不需要 `e2b-code-interpreter` 的 notebook/context 语义，优先使用 `e2b.Sandbox` 即可：

- `Sandbox.create(template=..., timeout=..., metadata=..., envs=..., allow_internet_access=True)` 创建实例；
- `sandbox.commands.run(..., on_stdout=..., on_stderr=...)` 执行 `/home/ksadk/agent.py`；
- `sandbox.files.*` 上传输入文件、下载产物；
- `sandbox.kill()` 在 finally 中销毁实例；
- `Sandbox.connect()` / `Sandbox.list()` 用于重试、诊断和后台任务恢复。

`e2b-code-interpreter`、Browser/CDP、PTY、Git 能力作为后续扩展，不进入首版 `execute_skills` 的最小依赖。

#### 可插拔后端边界

沙箱设计应从顶层重新抽象，不再以现有 `ksadk.sandbox.BaseSandbox` 为基础做兼容。当前 `ksadk.sandbox` 是早期“执行一段代码”的实验性模块，引用面主要是测试和 ADKRunner 的默认 sandbox tools 注入；既然当前没有稳定用户，就应在 Skills 沙箱方案中直接替换，而不是背兼容包袱。

新的对外抽象应以“执行一个 skills workflow”为中心，而不是以 E2B SDK 对象或通用 code execution 为中心。更准确的命名是 **Skill Runtime**：它描述 Skill 在哪里执行、用什么隔离级别执行、如何传入输入文件、如何回收产物和日志。Sandbox 只是 Skill Runtime 的一种 backend。E2B-compatible SDK 是首个生产 backend，不是 KsADK 对外抽象本身。`execute_skills` 不应直接把 `e2b.Sandbox`、`CodeSandbox`、`CommandHandle` 等 SDK 类型暴露给 Runner、Agent 或业务代码。

可参考但不照搬的边界：

| 参考对象 | 可借鉴点 | 不照搬点 |
|----------|----------|----------|
| VeADK / AgentKit `execute_skills` | 外层 Agent 只暴露一个 `execute_skills`，真实 skill 在远程执行环境内跑 | 不绑定火山 `InvokeTool/RunCode`、`AGENTKIT_TOOL_ID`、固定目录 `/home/gem/veadk_skills` |
| AgentKit `skills_sandbox` 示例 | 镜像内置最小 agent，由它加载 Skill Space 并执行 workflow | 不把示例脚本直接搬进 KsADK，需要适配 Skill Service、模型配置、日志和错误结构 |
| E2B SDK v2 | 标准生命周期：create、commands、files、connect、kill | 不把 E2B SDK 类型暴露为 KsADK 公共 API，避免未来换 backend 困难 |
| KsADK KB/LTM 远程访问 | runtime 通过 env 配置直接访问远端服务 | 不强制所有沙箱 backend 都走 AICP/KSYUN AKSK 模式 |

建议新增顶层模块：

```text
ksadk/skills/
  service_client.py          # Skill Service: list/download/verify/cache
  loader.py                  # local skill load + tool manifest
  runtime/
    base.py                  # SkillRuntimeBackend Protocol + data classes
    factory.py               # backend registry/env config
    agent.py                 # image 内最小 skills agent，可复制到 /home/ksadk/agent.py
    backends/
      e2b.py                 # E2B-compatible sandbox implementation
      local.py               # local debug implementation
      remote_http.py         # optional simple HTTP implementation
```

旧 `ksadk/sandbox/` 处理建议：

- 首版实现前标记为 internal/experimental，不继续扩展；
- 新的 `ksadk.skills.runtime` 成型后，删除旧模块或迁移为新模块内部实现细节；
- `ADKRunner._inject_sandbox_tools()` 改成新的 skills sandbox 注入逻辑；
- 旧 `test_sandbox_system.py` 不作为兼容测试保留，改写为 `test_skill_sandbox_*`，覆盖 workflow backend、E2B mock、本地 agent 调用和错误分类。

顶层 Skill Runtime backend 只表达 KsADK 需要的最小 workflow 能力：

```python
class SkillRuntimeBackend(Protocol):
    def run_workflow(
        self,
        workflow_prompt: str,
        *,
        skill_space_ids: list[str],
        session_id: str,
        env: dict[str, str] | None = None,
        input_files: list[SandboxInputFile] | None = None,
        timeout: int = 900,
    ) -> SkillRuntimeResult: ...
```

后端选择通过 factory/registry 完成：

```python
backend = create_skill_runtime_backend(
    backend=os.environ.get("KSADK_SKILL_RUNTIME_BACKEND", "e2b"),
)
result = backend.run_workflow(...)
```

环境变量分为通用变量和 backend 专属变量：

| 类型 | 变量 |
|------|------|
| 通用 | `KSADK_SKILL_RUNTIME_BACKEND`、`KSADK_SKILL_RUNTIME_TEMPLATE_ID`、`KSADK_SKILL_RUNTIME_TIMEOUT`、`KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS` |
| E2B backend | `E2B_API_URL`、`E2B_API_KEY` |
| 其他 backend | 只在对应 backend 内解释，例如 `FOO_SANDBOX_ENDPOINT`、`FOO_SANDBOX_TOKEN` |

这样未来接入其他沙箱时，只需要新增 backend 类和配置解析，不需要改 Runner、Skill Service client、`execute_skills` 工具 schema、最小 skills agent 或业务代码。

首版可以内置这些 Skill Runtime backend 名称：

| backend | 用途 | 状态 |
|---------|------|------|
| `e2b` | 金山云 E2B-compatible 沙箱，生产路径 | P0 |
| `local_process` | 本地调试，直接运行最小 skills agent | P1 |
| `remote_http` | 未来如有简单 HTTP 执行服务，可作为轻量后端 | P2 |
| `k8s_job` | 未来如需隔离批处理，可映射到 Kubernetes Job | P2/P3 |
| `disabled` | 不启用远程 runtime，只暴露 local/runtime skills | P0 |

因此目录不建议叫 `ksadk/skills/sandbox`。如果放在 `skills/sandbox` 下，后续扩展非沙箱形态的 Skill Runtime 会被命名误导。更合适的是 `ksadk/skills/runtime`，再在 `runtime/backends/e2b.py` 里表达“沙箱 backend”。

### E2B 后端服务访问方式

运行时 Pod 能出公网，原则上就可以由 KsADK runtime 直接连远程沙箱服务；这和当前知识库、长期记忆的实现方式一致。但“能出公网”只是必要条件，不是充分条件，还需要确认沙箱 endpoint 在该 Pod 网络下可 DNS 解析、路由可达、鉴权可用、TLS/代理策略匹配，并且没有被安全组、NetworkPolicy、egress allowlist 或企业网关拦截。

当前 KsADK 已有三类可复用参考：

| 能力 | 当前实现 | 访问方式 | 可复用到沙箱的设计点 |
|------|----------|----------|----------------------|
| 知识库 | `ksadk.knowledge_base.client.KnowledgeBaseClient` | runtime 进程读取 `KSADK_KB_*` / `KSYUN_*`，通过 `kingsoftcloud-sdk-python` 直连 AICP `RetrieveKnowledge` | env 驱动配置、SDK 懒加载、endpoint/region/scheme 可覆盖 |
| 长期记忆 SDK backend | `ksadk.memory.adk.backends.sdk_ltm_backend.SdkLTMBackend` | runtime 进程读取 `KSADK_LTM_*` / `KSYUN_*`，通过 `kingsoftcloud-sdk-python` 直连 AICP `CreateMemorySdk` / `QueryMemorySdk` | runtime 直连远端服务，不经过 Gateway 代理 |
| 长期记忆 HTTP backend | `ksadk.memory.adk.backends.http_ltm_backend.HttpLTMBackend` | runtime 进程读取 `KSADK_LTM_HTTP_URL` / `KSADK_LTM_HTTP_TOKEN`，用 `httpx` 直连 HTTP 服务 | 对非金山云 SDK 的远程服务，用 base URL + token 的薄客户端 |

因此沙箱首选也应走 runtime 直连：

```text
Agent Pod
  -> KsADK execute_skills tool
  -> KsADK Skill Runtime adapter
  -> E2B-compatible SDK / HTTP client
  -> remote sandbox service
  -> sandbox container /home/ksadk/agent.py
```

建议部署参数：

| 变量 | 说明 |
|------|------|
| `E2B_API_URL` | E2B SDK 原生变量，例如 `https://mgr.cn-beijing-6.sandbox.ksyun.com` |
| `E2B_API_KEY` | E2B SDK 原生鉴权变量，应由 Secret 注入 |
| `KSADK_SKILL_RUNTIME_BACKEND` | `e2b` / `local_process` / `disabled`，默认 `disabled` 或由 `KSADK_SKILLS_MODE=sandbox` 推导 |
| `KSADK_SKILL_RUNTIME_TEMPLATE_ID` | 内置 `/home/ksadk/agent.py` 的默认 skills 镜像/template id |
| `KSADK_SKILL_RUNTIME_TIMEOUT` | 单次 workflow 超时，建议默认 900 秒 |
| `KSADK_SKILL_RUNTIME_REGION` | 可选，runtime backend 需要 region 时再启用 |
| `KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS` | 是否给远程 runtime 开启外网访问，默认 `true`，用于拉 Skill 包、访问模型或外部资源 |

`KSADK_SKILL_RUNTIME_ENDPOINT` / `KSADK_SKILL_RUNTIME_API_KEY` 可以作为 KsADK 别名配置，但 E2B backend 最终应映射到 E2B SDK 原生变量 `E2B_API_URL` / `E2B_API_KEY`，避免和官方 SDK 行为偏离。

需要向沙箱团队确认的不是“Pod 能不能出公网”这一项，而是：

- endpoint/base URL 是公网、内网还是 VPC 私网地址；当前样例为 `https://mgr.cn-beijing-6.sandbox.ksyun.com`；
- E2B-compatible SDK 包名和版本；当前建议 `e2b>=2.0.0`，验证版本 `e2b==2.15.3`；
- `E2B_API_KEY` 的发放方式、权限边界、Secret 注入方式和轮转机制；
- template id/image id 是什么，是否已经内置 KsADK 最小 agent；
- create sandbox 时是否支持 env 注入、TTL、cwd、命令超时、stdout/stderr streaming；
- 文件上传/下载 API 是否兼容 E2B；
- sandbox 容器自身是否能访问 Skill Service、对象存储下载地址、模型服务。

如果 E2B 后端只能内网访问，则处理方式参考 AICP 内网 endpoint：把 `E2B_API_URL` 或 `KSADK_SKILL_RUNTIME_ENDPOINT` 配成内网地址，而不是让 Gateway 兜底转发。只有当运行时 Pod 不能直连、鉴权不允许下发到 Pod、或需要统一审计/限流时，才考虑 Gateway/控制面代理。

KsADK 首版只使用 E2B-compatible SDK 中的以下能力：

| 能力 | 优先级 | 期望接口形态 | 说明 |
|------|--------|--------------|------|
| 创建沙箱 | P0 | E2B sandbox create / code interpreter create | 指定默认 skills template，注入 `SKILL_SPACE_ID`、模型配置、凭据引用等 env |
| 连接已有沙箱 | P0 | E2B connect / reconnect | 外层重试、日志补拉、长任务恢复需要 |
| 销毁沙箱 | P0 | E2B kill / close / context manager cleanup | 必须支持显式释放，避免资源泄漏 |
| 设置/延长 TTL | P0 | E2B timeout API | skill 任务可能超过默认生命周期，但应有硬上限 |
| 运行命令 | P0 | E2B commands/run 或等价接口 | KsADK 首版主要运行 `python /home/ksadk/agent.py <prompt>` |
| 返回结构化结果 | P0 | E2B command execution result | 外层 `execute_skills` 需要稳定判断成功/失败 |
| 流式日志 | P0 | stdout/stderr callback 或 iterator | 用户需要看到长任务进度，外层也需要截断/汇总 |
| 文件上传/下载 | P0 | E2B filesystem API | 输入文件、产物文件、调试日志需要进出沙箱 |
| 工作目录控制 | P0 | `cwd` / workspace root | 最小 agent 默认 `/home/ksadk`，skill 产物放 workspace/output |
| 环境变量注入 | P0 | per-sandbox env + per-command env | 不要把临时凭据写进代码字符串 |
| 网络策略 | P0 | allow/deny internet, egress allowlist | Skill 下载、模型访问、外网访问需要可控 |
| 错误分类 | P0 | E2B-compatible exceptions | 便于 KsADK 给用户可诊断错误，不只返回 500 |
| 资源规格 | P1 | template/cpu/memory/disk | 不同 skill 对资源需求不同 |
| PTY/交互命令 | P1 | E2B PTY 或等价接口 | 首版不必须，后续调试和交互式工具有用 |
| 多 code context | P2 | E2B code interpreter context | skills agent 首版可不依赖 |

当前明确不依赖的能力：

- `sandbox.pause()` / resume / auto_resume；
- snapshot create/list/delete；
- 官方 E2B template build API；
- 运行中网络规则动态更新；
- Browser noVNC/CDP；
- Code Interpreter context 生命周期。

这些能力缺失不阻塞 `execute_skills` 首版，因为首版模型是“创建短生命周期 sandbox -> 运行一个 skills workflow 命令 -> 拉取结果 -> kill”。

KsADK 薄适配层建议：

```python
class KsSkillRuntime:
    def run_workflow(
        self,
        workflow_prompt: str,
        *,
        skill_space_ids: list[str],
        session_id: str,
        timeout: int = 900,
    ) -> SkillRuntimeResult: ...
```

配套数据结构：

```python
@dataclass
class SkillRuntimeResult:
    sandbox_id: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    error_type: str | None = None
    error_message: str | None = None
```

E2B backend 内部直接调用 E2B-compatible SDK；外部只暴露 KsADK 的 `execute_skills(workflow_prompt)` 语义。首版仍不要求完整 Jupyter/code-interpreter 语义。KsADK Skill Runtime 的第一目标是可靠运行镜像内的 `/home/ksadk/agent.py`，所以命令执行、文件进出、流式日志、错误分类比 notebook context、rich result、图表提取更重要。

建议首版调用骨架：

```python
import os
import shlex

from e2b import Sandbox


def run_workflow(prompt: str, *, skill_space_ids: list[str], session_id: str):
    sandbox = Sandbox.create(
        template=os.environ["KSADK_SKILL_RUNTIME_TEMPLATE_ID"],
        timeout=int(os.environ.get("KSADK_SKILL_RUNTIME_TIMEOUT", "900")),
        metadata={
            "runtime": "ksadk",
            "session_id": session_id,
        },
        envs={
            "KSADK_SKILL_SPACE_IDS": ",".join(skill_space_ids),
            "SKILL_SPACE_ID": skill_space_ids[0] if skill_space_ids else "",
        },
        allow_internet_access=os.environ.get(
            "KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS", "true"
        ).lower() in ("1", "true", "yes"),
    )
    try:
        return sandbox.commands.run(
            "python -u /home/ksadk/agent.py " + shlex.quote(prompt),
            on_stdout=append_stdout,
            on_stderr=append_stderr,
            timeout=int(os.environ.get("KSADK_SKILL_RUNTIME_TIMEOUT", "900")),
        )
    finally:
        sandbox.kill()
```

如果 workflow prompt 可能很长，正式实现可改为把 prompt 写入 `/tmp/ksadk-workflow-prompt.txt`，再执行 `python -u /home/ksadk/agent.py --prompt-file ...`，避免命令行长度限制和 quoting 风险。如果 SDK 的 `commands.run(..., timeout=...)` 参数名与当前兼容实现有差异，以沙箱团队实际 SDK 为准；KsADK 适配层要把这种差异封装掉，不暴露给业务代码。

远程 runtime 镜像内最小 agent 由 KsADK 在 Phase 1 提供，形态对齐 AgentKit `skills_sandbox` 示例：

```text
/home/ksadk/
  agent.py
  skills/
    <bundled-skill>/SKILL.md
  requirements.txt
```

外层 Agent 调用 `execute_skills(workflow_prompt)` 后，通过沙箱 SDK 运行：

```bash
python /home/ksadk/agent.py "$workflow_prompt"
```

`agent.py` 最小职责：

- 读取 `SKILL_SPACE_ID` / `KSADK_SKILL_SPACE_IDS`；
- 从 Skill Service 拉取 skill 索引；
- 构造内层 local skills agent；
- 暴露 `skills_tool`、`read_file`、`write_file`、`edit_file`、`bash`；
- 按需下载被调用的 skill zip；
- 执行完成后把结果写到 stdout，由沙箱 SDK 回传给外层 Agent。

验收：

- `KSADK_SKILLS_MODE=sandbox` 时只给外层 Agent 暴露 `execute_skills`；
- 内层沙箱 Agent 能读取同一个 Skill Space；
- stdout/stderr/超时/错误能回传；
- 凭据不泄露到模型输出。

## 风险

| 风险 | 影响 | 处理 |
|------|------|------|
| 鉴权方式未定 | client 无法实现 | 先做 client interface + mock，等契约确认 |
| LangGraph 已编译 graph 难自动注入 | “全框架自动”不可达 | 明确 helper 接入边界 |
| `bash` 能力扩大攻击面 | 安全风险 | 复用现有执行策略和 allowlist |
| OpenClaw/Hermes 不经过 KsADK Runner | 通用注入无效 | 单独做 runtime/template adapter |
| 沙箱服务未就绪 | `execute_skills` 远程调用链路阻塞 | 最小 skills agent 先交付给沙箱团队构建镜像，SDK 集成后补外层调用 |

## 推荐优先级

P0 必做：

- deploy/update 下发 Skill Space env；
- Skill Service runtime client；
- `ListSkillsBySpaceId` 发现链路；
- 安全下载和缓存；
- ADK Runner 自动注入；
- 镜像内最小 skills agent；
- status/healthz 里展示 Skill Space 绑定诊断。

P1 再做：

- LangChain helper；
- LangGraph/DeepAgents helper；
- status/bootstrap 展示 skills 配置。

P2 暂缓：

- Skill CRUD 管理 CLI；
- 外层远程沙箱 `execute_skills` SDK 集成；
- 全框架无感自动注入；
- tags/marketplace/ranking；
- 复杂 checklist 状态持久化。

最终建议：先把 Skill Center 当作“平台管理的可复用工具包注册表 + KsADK Runner 的工具加载源”，不要一开始就做成“全框架自动沙箱执行系统”。这样能最小闭环、风险可控，也和当前 KsADK 的部署/运行时架构更匹配。
