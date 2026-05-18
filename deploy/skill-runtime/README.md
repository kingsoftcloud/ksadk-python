# KsADK Skill Runtime 镜像契约

该目录提供沙箱团队构建默认 Skill Runtime 镜像时需要内置的入口文件和验收契约。

Skill Runtime 镜像是通用 KsADK Sandbox 底座的一个使用方。沙箱产品当前基于 E2B，并提供 AIO、Code、Browser 三类模板。Skill Runtime 默认建议使用 AIO 模板，因为真实 skill 往往需要 Terminal、文件系统、包管理器和网络访问。

该交付物不是一个完整镜像。它包含稳定入口 `agent.py` 和构建约定；镜像仍需要安装 Python、KsADK、Node 工具链和沙箱平台要求的基础服务。

## 必需路径

将 `agent.py` 复制到镜像内：

```text
/home/ksadk/agent.py
```

KsADK 的 E2B backend 会这样启动它：

```bash
python -u /home/ksadk/agent.py --prompt-file /tmp/ksadk-workflow-prompt.txt
```

该文件不需要作为容器 `ENTRYPOINT`。E2B backend 会在 sandbox 实例启动后通过 `commands.run()` 执行它。模板自己的启动命令只需要满足沙箱平台要求，并保持实例可接收命令和文件操作。

推荐权限：

```text
/home/ksadk/agent.py  owner=ksadk:ksadk  mode=0555
/home/ksadk           owner=ksadk:ksadk  mode=0755 或更宽松
/tmp                  对运行用户可写
```

如果设置了 `KSADK_SKILL_CACHE_DIR` 或 `KSADK_SKILL_WORKDIR`，对应目录也必须对运行用户可写。

## 运行用户

推荐镜像内创建非 root 用户：

```text
user:  ksadk
uid:   10001
group: ksadk
gid:   10001
home:  /home/ksadk
shell: /bin/bash
```

Skill Runtime 会下载 skill zip、解压缓存、创建临时项目、运行 skill 脚本。运行用户至少需要写入：

- `/tmp`
- `$HOME`，用于 npm/pnpm 缓存和工具默认目录
- `KSADK_SKILL_CACHE_DIR`，如果显式配置
- `KSADK_SKILL_WORKDIR`，如果显式配置

如果沙箱平台当前只能以 root 运行，功能上可以工作，但默认镜像仍建议按非 root 用户构建，避免 skill 脚本拿到不必要的容器权限。

## Python 依赖

镜像内需要安装带 Skill Runtime 依赖的 KsADK：

```bash
pip install "ksadk[skills]"
```

预发镜像测试时也可以安装本地 wheel 或源码包，但 runtime import 路径必须保持稳定。镜像内至少要通过：

```bash
python -c "from ksadk.skills.runtime.agent import main; print('ok')"
```

如果镜像只作为 sandbox 内部执行器使用，`agent.py` 本身不直接调用 E2B SDK；但仍推荐安装 `ksadk[skills]`，这样同一镜像可覆盖 Skill Runtime 和后续 sandbox 侧扩展能力。

## 运行时工具

最小 agent 可以加载任意包含 `SKILL.md` 的 Skill 包。首个 E2E fixture 是 `web-artifacts-builder`，因此默认镜像还需要：

- Python 3.10+
- Bash
- Node.js 20 LTS 优先，Node.js 18 为最低要求
- npm
- pnpm，建议构建期预装，不要依赖运行期 `npm install -g pnpm`
- tar、gzip、sed、coreutils、ca-certificates
- 可访问 Skill Service、skill archive 下载地址、npm registry 或内部 npm 镜像

`web-artifacts-builder` 的脚本会使用 `node`、`npm`、`pnpm`、`tar`、`sed`、`du` 等命令，并会安装 Vite、Tailwind、Radix、Parcel 等前端依赖。非 root 用户运行时，如果未预装 `pnpm`，脚本里的 `npm install -g pnpm` 很可能因为全局 npm 目录不可写而失败，所以默认镜像必须预装 `pnpm`。

## Dockerfile 片段

沙箱团队可以按现有 AIO 基础镜像调整，核心要求如下：

```dockerfile
RUN groupadd -g 10001 ksadk \
    && useradd -m -u 10001 -g 10001 -s /bin/bash ksadk

RUN python -m pip install --no-cache-dir "ksadk[skills]"

# Node.js/npm 由基础镜像或平台规范安装；这里仅展示 pnpm 要在构建期预装。
RUN npm install -g pnpm

COPY deploy/skill-runtime/agent.py /home/ksadk/agent.py
RUN chown -R ksadk:ksadk /home/ksadk \
    && chmod 0555 /home/ksadk/agent.py

ENV HOME=/home/ksadk
USER 10001:10001
WORKDIR /home/ksadk
```

如果使用私有 PyPI 或内部 npm 源，可以在构建期配置 pip index 和 npm registry；不要把 API key、AK/SK、E2B key 或 Skill Service token 写进镜像层。

## 环境变量契约

外层 KsADK runtime 会向 sandbox 注入这些变量：

- `KSADK_SANDBOX_BACKEND`：通用 sandbox backend，当前为 `e2b`。
- `KSADK_SANDBOX_TYPE`：`aio`、`code`、`browser` 或 `private`。
- `KSADK_SANDBOX_TEMPLATE_ID`：沙箱控制台创建的模板 ID，新部署优先使用该变量。
- `KSADK_SANDBOX_TIMEOUT`：sandbox 会话超时秒数。
- `KSADK_SANDBOX_ALLOW_INTERNET_ACCESS`：是否允许 sandbox 会话出网。
- `KSADK_SKILL_SPACE_IDS`：逗号分隔的 Skill Space id。
- `SKILL_SPACE_ID`：单 space 兼容变量。
- `KSADK_SKILL_SERVICE_URL`：Skill Service base URL。
- `KSADK_SKILL_SERVICE_ACCOUNT_ID`：租户账号 ID，用作 `X-Ksc-Account-Id`。
- `KSADK_SKILL_SERVICE_TOKEN`：可选 bearer token。
- `KSADK_SKILL_SERVICE_ACCESS_KEY` / `KSADK_SKILL_SERVICE_SECRET_KEY`：使用 AICP KOP endpoint 时的可选签名凭证。
- `KSADK_SKILL_SERVICE_REGION`：签名 region，默认 `cn-beijing-6`。
- `KSADK_SKILL_SERVICE_API_VERSION`：Skill Center KOP version，默认 `2024-06-12`。
- `KSADK_SKILL_CACHE_DIR`：可选 Skill 下载缓存目录。
- `KSADK_SKILL_WORKDIR`：可选 workflow 工作目录。
- `KSADK_SKILL_ARTIFACT_PROJECT`：可选输出项目目录名。
- `KSADK_SKILL_RUNTIME_TIMEOUT`：Skill workflow 命令超时秒数兼容变量。

不要把 API key 烘焙进镜像。所有凭证都必须来自运行时环境变量或 Secret 注入。

使用 AICP KOP endpoint 时，镜像内 agent 会通过 `ListSkillsBySpaceId` action 读取技能列表，参数为 `SpaceId`。Skill archive 下载后会按 `ContentHash` 做 sha256 校验；校验失败时必须失败退出，不能降级跳过校验。

## Smoke Test

安装 `ksadk[skills]` 后，镜像内至少执行以下检查：

```bash
id
test -r /home/ksadk/agent.py
python -c "from ksadk.skills.runtime.agent import main; print('ok')"
node -v
npm -v
pnpm -v
```

即使未配置 Skill Service，镜像也应该能启动并返回 `no_skills`：

```bash
python -u /home/ksadk/agent.py "noop"
```

源码树构建镜像前可在仓库根目录执行：

```bash
PYTHONPATH=. python -u deploy/skill-runtime/agent.py "noop"
```

期望输出包含：

```text
workflow=noop
loaded_skills=
workflow_result={"commands": [], "executed_skill": "", "output_files": [], "status": "no_skills"}
```

配置 Skill Space 后，期望输出包含 `workflow_result=...` JSON 行。KsADK 会从该 JSON 中解析 `output_files`。
