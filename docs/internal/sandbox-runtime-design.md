# KsADK Sandbox Runtime 设计

## 1. 定位

Sandbox 是通用隔离执行底座，不是 Skill 专用能力。Skill Runtime 只是 Sandbox 的一个上层使用方。

KsADK 的分层应保持为：

```text
ADK / LangGraph / Hosted Agent
  │
  ├─ 代码执行工具
  ├─ 浏览器 / Computer Use 工具
  ├─ Sub-agent 任务隔离
  └─ Skill Runtime
       │
       └─ Sandbox backend
            ├─ e2b
            ├─ local_process
            └─ 未来 ksyun_sandbox_kop / 其他 provider
```

当前阶段优先使用 E2B SDK。原因是沙箱产品控制台底层也是 E2B，控制台负责创建 AIO / Code / Browser 模板，KsADK 通过 template id 使用 E2B SDK 创建会话、写文件、执行命令和销毁实例。

Sandbox Open API / KOP 先定位为模板与实例生命周期管理接口，后续可以作为独立 backend 或控制面集成，但不替代当前 E2B SDK 执行链路。

## 2. 控制台类型映射

沙箱控制台当前支持三类预制 Sandbox，和 VeADK 的 `aio_sandbox`、`code_sandbox`、`browser_sandbox` 思路一致：

| 控制台类型 | KsADK 类型 | 用途 |
| --- | --- | --- |
| `AIO SandBox` | `aio` | 一体化运行环境，包含 Browser、Terminal、Code 能力。Skill Runtime 默认使用该类型，因为真实 skill 往往需要 shell、文件系统、包管理器和网络访问。 |
| `Code SandBox` | `code` | 多语言安全代码执行环境，后续可对齐 ADK `code_executor` 或 Code Interpreter。 |
| `浏览器 SandBox` | `browser` | 浏览器自动化、CDP/noVNC、网页调试、UI 测试和 Computer Use 类场景。 |

`Private` 是自定义镜像模板模式。它可以承载 AIO、Code 或 Browser 能力，具体取决于镜像内暴露的服务和端口。

## 3. KsADK 抽象

通用 Sandbox API 位于 `ksadk.sandbox`：

```python
from ksadk.sandbox import create_sandbox_backend

backend = create_sandbox_backend("e2b")
session = backend.create_session(
    session_id="sess-1",
    env={"APP_ENV": "test"},
)
try:
    session.write_file("/tmp/input.txt", b"hello")
    result = session.run_command("python -V", timeout=30)
finally:
    session.kill()
```

首版实现为 `E2BSandboxBackend`，包装 E2B SDK 的最小通用能力：

- `Sandbox.create(template=..., timeout=..., metadata=..., envs=..., allow_internet_access=...)`
- `sandbox.files.write(...)`
- `sandbox.files.read(...)`
- `sandbox.commands.run(...)`
- `sandbox.get_host(port)`
- `sandbox.kill()`

Skill Runtime 不拥有这些通用能力，只组合它们来执行镜像内的最小 agent：

```bash
python -u /home/ksadk/agent.py --prompt-file /tmp/ksadk-workflow-prompt.txt
```

## 4. Backend 策略

### 4.1 E2B backend

E2B 是当前主路径。

沙箱团队或控制台创建模板，KsADK 拿到 template id 后用 E2B SDK 创建短生命周期会话并执行命令。这样可以复用 E2B SDK 的 commands/files 协议，避免 KsADK 自己重写底层协议。

必需部署参数：

```text
E2B_API_URL=https://mgr.cn-beijing-6.sandbox.ksyun.com
E2B_API_KEY=<secret>
KSADK_SANDBOX_TEMPLATE_ID=<template-id>
```

`KSADK_SKILL_RUNTIME_TEMPLATE_ID` 仅作为 Skill Runtime 兼容别名保留。新部署优先使用 `KSADK_SANDBOX_TEMPLATE_ID`。

### 4.2 Sandbox KOP backend

当前 Sandbox Open API / KOP 覆盖的是模板和实例生命周期：

- 模板：创建、更新、详情、列表、删除。
- 实例：启动、删除、详情、列表。
- 二期新增：暂停、恢复、更新超时、获取 token、镜像预热相关接口。

这些接口适合控制面集成，但仅凭这些接口还不能替代 E2B SDK 的执行链路。新增 KOP 执行 backend 前，需要沙箱团队确认：

1. `StartSandboxInstance` 是否支持每次实例启动传入 env。Skill Runtime 需要按请求注入 `KSADK_SKILL_SPACE_IDS`、`KSADK_SKILL_SERVICE_URL` 等变量。
2. `Endpoint + Token` 是否能直接用于 E2B SDK 或 envd 兼容的 commands/files API。
3. AIO / Code / Browser 模板的端口与 `get_host` 行为是否稳定。
4. 预发和生产的 KOP endpoint、签名 service、region 和鉴权方式。

## 5. Skill Runtime 组合方式

`ksadk.skills.runtime` 继续作为 Skill 专用层，负责：

- 读取 `KSADK_SKILL_SPACE_IDS` / `SKILL_SPACE_ID`。
- 查询 Skill Service。
- 通过 `GetSkillDownloadUrl` 下载 skill archive。
- 使用 `ContentHash` 做 sha256 校验。
- 安全解压 zip，防止 zip slip。
- 加载 `SKILL.md`。
- 在 sandbox agent 内执行 workflow。

ADK Runner 行为：

- `KSADK_SKILLS_MODE=local`：扫描本地 `SKILL.md` 包并注入 `skills_tool`。
- `KSADK_SKILLS_MODE=sandbox`：只注入 `execute_skills`。
- `auto`：配置了 sandbox backend/template 时优先进入 sandbox；否则发现本地 skills 时进入 local。

这样不会把 100 个 skill 展开成 100 个模型可见工具。模型只看到一个渐进式披露工具，runtime agent 根据 space id 拉取列表并按需加载 skill。

## 6. 环境变量

通用 Sandbox 变量：

| 名称 | 说明 |
| --- | --- |
| `KSADK_SANDBOX_BACKEND` | 通用 Sandbox backend。当前支持 `e2b`。 |
| `KSADK_SANDBOX_TYPE` | `aio`、`code`、`browser` 或 `private`。默认 `aio`。 |
| `KSADK_SANDBOX_TEMPLATE_ID` | 沙箱控制台创建的模板 ID。 |
| `KSADK_SANDBOX_TIMEOUT` | Sandbox 会话超时秒数。 |
| `KSADK_SANDBOX_ALLOW_INTERNET_ACCESS` | 是否允许会话出网。 |
| `E2B_API_URL` | E2B 兼容 manager endpoint。 |
| `E2B_API_KEY` | E2B API key，只能通过 Secret 或本地环境变量注入。 |

Skill Runtime 变量继续使用 `KSADK_SKILL_*` 前缀，不作为通用 Sandbox 契约。迁移期内 `KSADK_SKILL_RUNTIME_TEMPLATE_ID` 保留为 template id 兼容别名。

## 7. 当前限制

- 首版 E2B backend 只包装生命周期、命令、文件读写和 host lookup。
- Code Interpreter、Browser、Computer Use 还没有作为 KsADK 一等工具封装。
- Sandbox KOP 暂作为后续 provider / 控制面集成，不是当前执行 backend。
- Skill Runtime E2E 需要模板镜像内置 `/home/ksadk/agent.py`。
- Skill Service 的 `ListSkillsBySpaceId` KOP action 已注册。KsADK KOP 模式直接调用 `ListSkillsBySpaceId`，参数为 `SpaceId`。
- 预发 Skill Service 的 `ContentHash` 与实际 zip sha256 不一致问题已修复。KsADK 仍必须继续 fail closed，不能绕过 hash 校验。
