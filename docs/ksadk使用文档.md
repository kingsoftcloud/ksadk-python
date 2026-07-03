# ksadk使用文档

本文档面向使用 `agentengine` / `ksadk` 的开发者、SA 与交付同学，口径以当前仓库代码、CLI 帮助、测试断言和 Docker/Makefile 默认值为准。

## 1. 适用范围

当前文档覆盖这些主线能力：

- 本地初始化、配置、运行与调试
- `build / deploy / launch` 的构建与部署参数
- `agentengine files` 的完整工作区文件管理链路
- Hermes 与 OpenClaw 的部署和常用验证路径
- PVC 默认值、默认挂载目录、容量约束
- `agentengine agent invoke` 的 Hermes 远端 native 调用与本地目录同步

## 2. 安装与入口

```bash
pip install -U ksadk
```

可选 extras：

```bash
pip install "ksadk[langgraph]"
pip install "ksadk[langchain]"
pip install "ksadk[deepagents]"
pip install "ksadk[adk]"
pip install "ksadk[skills]"
```

知识库和长期记忆使用的 `kingsoftcloud-sdk-python` 已包含在默认依赖中，不需要额外安装 `ksadk[kb]`。

命令入口等价：

```bash
agentengine --help
ksadk --help
```

## 3. CLI 全景

当前主线命令组包括：

- `init`
- `config`
- `run`
- `web`
- `build`
- `deploy`
- `launch`
- `agent`
- `files`
- `dashboard`
- `hermes`
- `openclaw`
- `mcp`
- `a2a`

全局选项包括：

- `--output pretty|json`
- `--dry-run`
- `--no-color`

```mermaid
flowchart LR
  classDef client fill:#dbeafe,stroke:#1d4ed8,stroke-width:2px,color:#1e3a8a;
  classDef control fill:#ede9fe,stroke:#7c3aed,stroke-width:2px,color:#581c87;
  classDef data fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#166534;
  classDef runtime fill:#e2e8f0,stroke:#475569,stroke-width:2px,color:#1e293b;

  Init["init / config / run / web"]:::client --> Local["本地开发链路"]:::data
  Build["build / deploy / launch"]:::client --> Server["agentengine-server"]:::control
  Files["files / agent invoke"]:::client --> Runtime["远端 runtime / Hosted Action"]:::data
  Hermes["hermes *"]:::client --> HermesRT["Hermes Runtime"]:::runtime
  OpenClaw["openclaw *"]:::client --> OpenClawRT["OpenClaw Runtime"]:::runtime
```

## 4. 最短成功路径

### 4.1 本地项目初始化

```bash
agentengine init my-agent -f langgraph
cd my-agent
agentengine config
agentengine run -i
```

本地 Web UI：

```bash
agentengine web --port 8080
```

### 4.2 云端一键部署

```bash
agentengine launch . --target serverless
```

### 4.3 Hermes 云端部署

```bash
agentengine init my-hermes -f hermes
cd my-hermes
agentengine hermes deploy --name my-hermes
agentengine hermes status
```

### 4.4 OpenClaw 云端部署

```bash
agentengine init my-openclaw -f openclaw
cd my-openclaw
agentengine openclaw deploy
agentengine openclaw status
```

### 4.5 Skill Runtime 与内置工具接入

`0.6.2` 新增 `ksadk.toolsets`，开发者可以在 LangGraph、LangChain、DeepAgents 或自定义 runner 中显式绑定 AgentEngine 内置工具。推荐默认使用渐进式披露，避免把所有低频或高风险工具直接塞进模型上下文：

```python
from ksadk.toolsets import describe_agentengine_tools, get_agentengine_tools

tools = get_agentengine_tools(include=["focused", "agentengine_tool_dispatcher"])
tool_specs = describe_agentengine_tools(include=["focused", "agentengine_tool_dispatcher"])
```

`focused` 默认直接暴露这些高频工具：

- Skill Space：`list_skills`、`search_skills`、`load_skill`
- Workspace：`workspace_status`、`search_workspace_files`、`edit_workspace_file`、`lint_workspace_file`
- Platform：`component_status`
- Sandbox：`sandbox_status`

低频、高风险或上下文较重的工具通过 `agentengine_tool_dispatcher` 按需 `list` / `describe` / `call`：

```python
from ksadk.toolsets import agentengine_tool_dispatcher

agentengine_tool_dispatcher("describe", tool_name="run_code")
agentengine_tool_dispatcher(
    "call",
    tool_name="run_code",
    arguments={"code": "print(42)", "language": "python"},
)
```

`get_agentengine_tools()` 无参仍返回全量工具，兼容旧项目；新项目建议显式写 `include=["focused", "agentengine_tool_dispatcher"]`。如果只需要某个分组，也可以写 `include=["skill"]`、`include=["workspace"]`、`include=["platform"]` 或 `include=["sandbox"]`。

Skill Runtime 执行入口是 `execute_skills`。它只用于 workflow 型任务，普通 instruction-first Skill 推荐先 `load_skill` 读取 `SKILL.md`，再由外层 agent 按指令完成。隔离执行 backend 由环境变量决定：

- `KSADK_SKILL_RUNTIME_BACKEND=local_process`：走本地 agent 进程
- `KSADK_SKILL_RUNTIME_BACKEND=e2b`：走远程 sandbox / E2B backend
- 未设置 backend 但存在 `KSADK_SANDBOX_TEMPLATE_ID`：自动走 E2B
- 显式 `KSADK_SKILL_RUNTIME_BACKEND=disabled`：禁用隔离执行

Workspace 内置工具只访问 AgentEngine UI workspace，不访问任意宿主机路径。`edit_workspace_file` 是 exact snippet replacement；匹配不到返回 `snippet_not_found`，匹配次数不符合预期返回 `ambiguous_edit`。`lint_workspace_file` 提供 Python AST、JSON parse 和通用文本轻量检查。

Sandbox direct tools 只通过 configured isolated sandbox backend 执行。`run_command` / `run_code` 不会退化为宿主机 shell；未配置 sandbox 时会返回诊断。`execute_skills`、Workspace 写入/删除、sandbox command/code 等中高风险工具会经过 Tool Gateway；strict 模式下会返回 `approval_required`，由 UI 或调用方回传批准后继续。

## 5. `/v1/responses` OpenAI 兼容接口

本地 `agentengine run -i` 启动后，AgentEngine 暴露 `/v1/responses`。这一接口优先兼容 OpenAI Responses 的返回结构和 SSE 生命周期，同时保留少量 `ksadk` 扩展字段，方便会话和本地 CLI 继续工作。

### 5.1 非流式调用

```bash
curl http://127.0.0.1:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.1",
    "input": "请用一句话介绍 AgentEngine",
    "instructions": "只用中文回答，语气简洁",
    "metadata": {"source": "local-doc"}
  }'
```

当前支持的常用请求字段：

- `input`：字符串或 OpenAI message/input item 列表。
- `model`：本轮请求使用的模型，会同步到运行时环境。
- `instructions`：本轮系统/开发者指令，不写入用户消息正文；LangGraph 会转为 system message，字符串输入类 runner 会作为 prompt 前缀。
- `metadata`：请求元数据，会回显到 response object，并记录到本轮事件 metadata；不参与模型生成。
- `stream`：`true` 时返回 SSE。
- `session_id`：复用已有会话；未传时自动创建。

非流式响应包含官方风格字段：`id`、`object`、`created_at`、`status`、`model`、`output`、`metadata`、`usage`、`error`、`incomplete_details`。同时保留 `output_text` 和 `session_id` 作为 `ksadk` 扩展，兼容现有调用方。

### 5.2 流式调用

```bash
curl -N http://127.0.0.1:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.1",
    "session_id": "sess-demo-001",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "分析这个任务"}]}],
    "stream": true
  }'
```

`model` 和 `session_id` 在流式与非流式调用中都可传；`session_id` 用来复用同一会话，未传时自动创建。

流式事件按 Responses 生命周期输出：

- `response.created` / `response.in_progress`：响应创建与开始运行。
- `response.output_item.added` / `response.content_part.added`：开始输出 message、reasoning、function call 或 MCP approval request。
- `response.output_text.delta` / `response.output_text.done`：正文增量与正文完成。
- `response.reasoning.delta`：思考内容增量，前端可选择单独渲染。
- `response.function_call_arguments.delta` / `response.function_call_arguments.done`：工具调用参数；底层一次性拿到参数时也会按一次 delta + done 输出。
- `response.output_item.done` / `response.content_part.done`：输出项或内容块完成。
- `response.completed`：本轮完成。
- `response.failed`：运行失败。
- `response.incomplete`：需要人工审核或中断恢复。

工具结果和人工审核的兼容策略：

- `response.ksadk.tool_result`：工具执行结果。
- 能明确识别为工具审批的 interrupt 会渲染为官方风格 `mcp_approval_request` output item。
- 其他通用 interrupt 使用 `response.ksadk.approval_request` 扩展事件；最终 response 会以 `status: "incomplete"` 返回，并在 `incomplete_details.ksadk_interrupt` 中包含中断信息。

### 5.3 图片与附件输入

`/v1/responses` 是默认主维护协议，按 OpenAI Responses 语义接收 `input_text` / `input_image` / `input_file`。运行时会把这些输入块原样投影到 runner 的 `input_content` / `input_messages`，同时生成 legacy/internal 的 `input_parts` 兼容旧 runner。

当前 `/v1/responses` 推荐输入块：

- `input_text`
- `input_image`
- `input_file`

旧客户端仍可传 KOP 风格 part 数组，运行时会兼容：

- `text`
- `inlineData`
- `fileData`

推荐图片传法：

1. 按 OpenAI Responses 官方形态传 `input_image.image_url`，其中 `image_url` 可以是远程图片 URL，也可以是 `data:image/...;base64,...`
2. 老客户端可先调用 `UploadFile` 上传图片，再通过兼容扩展 `fileData.fileUri` 引用
3. 老客户端可直接把图片 base64 放进兼容扩展 `inlineData.data`

OpenAI 风格 data URL 示例：

```json
{
  "input": [
    {
      "role": "user",
      "content": [
        { "type": "input_text", "text": "请分析这张图片" },
        {
          "type": "input_image",
          "image_url": "data:image/png;base64,<base64-encoded-image-bytes>"
        }
      ]
    }
  ],
  "stream": false
}
```

运行时会保留这个官方输入块到 `input_content`，并额外归一化为内部附件上下文，因此业务代码可以继续通过 `has_current_files` / `current_attachments` 判断本轮是否带图。远程图片 URL 会作为引用保留，并可在支持原生图片输入的 LangGraph 路径下继续传给模型；KsADK 不会主动拉取远程图片做 OCR。需要平台提取、OCR 或本地附件内容时，请使用 data URL、`inlineData` 或 `fileData`。

多模态模型“看图”和平台 OCR 是两条不同链路：推荐让支持图片的模型直接消费 `input_image` / `input_content`，这样不需要在代码包里安装本地 OCR 依赖。平台本地 OCR 只用于需要把图片预先转成 `current_attachment_results[*].text` 的场景；源码构建默认不打包 OCR 二进制栈，如需启用请在构建环境设置 `KSADK_BUILD_ENABLE_ATTACHMENT_OCR=true`，或在项目 `requirements.txt` 中显式加入 OCR 相关依赖。

OpenAI 风格文件示例：

```json
{
  "input": [
    {
      "role": "user",
      "content": [
        { "type": "input_text", "text": "请总结这个文件" },
        {
          "type": "input_file",
          "filename": "resume.txt",
          "file_data": "<base64-encoded-file-bytes>"
        }
      ]
    }
  ]
}
```

`input_file.file_data` 会保留在 `input_content`，并归一化为内部 `inlineData`；`input_file.file_url` / `input_file.file_id` 会保留为引用，并归一化为内部 `fileData`。KsADK 不会主动拉取远程 `file_url` 内容；需要平台提取或 OCR 时，请使用 `file_data`、`inlineData` 或先上传后用 `fileData.fileUri`。

旧客户端先上传再引用的兼容示例：

```json
{
  "input": [
    {
      "role": "user",
      "content": [
        { "text": "请分析这张图片" },
        {
          "fileData": {
            "fileUri": "ksadk-upload://abc123.png",
            "displayName": "diagram.png",
            "mimeType": "image/png"
          }
        }
      ]
    }
  ],
  "stream": false
}
```

旧客户端直接内联的兼容示例：

```json
{
  "input": [
    {
      "role": "user",
      "content": [
        { "text": "请分析这张图片" },
        {
          "inlineData": {
            "data": "<base64-encoded-image-bytes>",
            "displayName": "diagram.png",
            "mimeType": "image/png"
          }
        }
      ]
    }
  ]
}
```

当前附件类型支持矩阵：

| 类型 | 典型扩展名 / MIME | 传输支持 | 平台内容提取 | 原生多模态直通 |
| --- | --- | --- | --- | --- |
| 文本 | `.txt` `.md` `.json` `.yaml` `.yml` `.csv` `.tsv` `.log` | 支持 | 支持 | 不适用 |
| 文档 | `.pdf` `.docx` `.pptx` `.xlsx` `.html` `.htm` | 支持 | 部分支持：文本提取 / OCR | 不适用 |
| 图片 | `.png` `.jpg` `.jpeg` `.webp` / `image/*` | 支持 | 元信息提取默认支持；OCR 需构建时显式启用 | 部分支持，见下方 |
| 压缩包 | `.zip` | 支持 | 支持：目录/可读文件抽样提取 | 不适用 |
| 其他二进制 | 其他后缀或 `application/octet-stream` | 支持 | 通常仅保留为附件引用 | 不支持 |

框架差异：

- `ADK`
  - 图片会优先按原生 bytes part 传给底层 SDK
  - 如果模型支持原生多模态，可直接吃图
- `LangGraph`
  - 若模型支持图片输入，默认消息构造会把图片附件转换成多模态 `HumanMessage.content` blocks
- `LangChain`
  - 当前不保证所有 agent 自动原生吃图
  - 如需原生多模态，建议在 `ksadk_prepare_input(payload, session_context)` 中优先消费 `input_content / input_messages`，必要时再兼容 `input_parts / current_attachments / attachments`
  - 判断当前轮是否传文件用 KsADK runner payload 扩展字段 `has_current_files`；该字段不是 OpenAI Responses API 官方字段

模型能力判断优先级：

1. 请求里显式传入的 `model_metadata`
2. runtime 通过 `OPENAI_BASE_URL` / `OPENAI_API_KEY` 查询上游 `/v1/models` 返回的 `architecture.input_modalities`
3. 本地默认兜底（按文本模型处理）

### 5.4 当前不支持

本期不支持 `previous_response_id`、`store`、复杂 `reasoning/text` 控制、完整 tool schema 请求面，也不新增原生 LangGraph state endpoint。自定义 LangGraph State 请使用 `ksadk_prepare_state` 或 `agentengine init --from-agent` 自动生成的 adapter。

## 6. Framework、挂盘与 workspace 约定

### 6.1 默认 PVC 规则

来自 `ksadk/cli/storage.py` 的统一约束：

- 默认容量：`20Gi`
- 最小容量：`20Gi`
- 最大容量：`500Gi`

### 6.2 默认挂载目录

| Framework | 默认挂载目录 | workspace 逻辑根 | 当前代码里可直接确认的绝对路径 |
| --- | --- | --- | --- |
| `adk` | `/home/node/.agentengine` | `workspace:/` | 运行时对外统一以逻辑根 `workspace` 暴露 |
| `langchain` | `/home/node/.agentengine` | `workspace:/` | 运行时对外统一以逻辑根 `workspace` 暴露 |
| `langgraph` | `/home/node/.agentengine` | `workspace:/` | 运行时对外统一以逻辑根 `workspace` 暴露 |
| `deepagents` | `/home/node/.agentengine` | `workspace:/` | 运行时对外统一以逻辑根 `workspace` 暴露 |
| `hermes` | `/home/node/.hermes` | `workspace:/` | `/home/node/.hermes/workspace` |
| `openclaw` | `/home/node/.openclaw` | `workspace:/` | `/home/node/.openclaw/workspace` |

补充说明：

- 本地 `ksadk server` 的 workspace 根目录是 `<project>/.agentengine/ui/workspace`。
- 对外 CLI 和 Hosted UI 一律展示逻辑根 `workspace:/...`。
- 当运行时响应里带有 `workspace_real_root` 或 `workspace_path` 时，CLI 会同时显示“实际目录”。

```mermaid
flowchart TB
  classDef runtime fill:#e2e8f0,stroke:#475569,stroke-width:2px,color:#1e293b;
  classDef storage fill:#ffedd5,stroke:#ea580c,stroke-width:2px,color:#9a3412;
  classDef data fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#166534;

  Local["本地项目"]:::runtime --> LocalRoot[".agentengine/ui/workspace"]:::storage
  Generic["adk / langchain / langgraph / deepagents"]:::runtime --> GenericMount["/home/node/.agentengine"]:::storage
  Hermes["hermes"]:::runtime --> HermesRoot["/home/node/.hermes/workspace"]:::storage
  OpenClaw["openclaw"]:::runtime --> OpenClawRoot["/home/node/.openclaw/workspace"]:::storage
  LocalRoot --> Logical["逻辑展示统一为 workspace:/"]:::data
  GenericMount --> Logical
  HermesRoot --> Logical
  OpenClawRoot --> Logical
```

## 7. `build / deploy / launch` 参数

### 7.1 构建体积与依赖策略

`agentengine build` 默认优先保持源码包轻量，不会把所有平台增强能力的重依赖都打进包：

- 默认包含：KsADK runtime 必需依赖、附件基础解析依赖、`kingsoftcloud-sdk-python`、`requests-aws4auth`。
- 推荐多模态图片写法：让支持图片的模型直接消费 OpenAI Responses `input_image` / runner `input_content`，不要为了“看图”默认启用本地 OCR。
- 兼容 OCR 写法：如果业务明确需要平台先把图片转成 `current_attachment_results[*].text`，再设置 `KSADK_BUILD_ENABLE_ATTACHMENT_OCR=true`，或在项目 `requirements.txt` 显式写入 OCR 依赖。
- MCP adapter：默认不打包；当项目 import `mcp` / `langchain_mcp_adapters`，或 `.env` 配置了非空 `KSADK_MCP_SERVERS` 时自动加入。自动发现不到时可设置 `KSADK_BUILD_ENABLE_MCP=true`。
- PostgreSQL session：默认不打包 `asyncpg`；当 `.env` 设置 `KSADK_SESSION_BACKEND=postgres` 或 PostgreSQL DSN 时自动加入。自动发现不到时可设置 `KSADK_BUILD_ENABLE_POSTGRES_SESSION=true`。

构建会复用 `.agentengine/code_build/pip_cache`，依赖清单未变化时也会复用 `.agentengine/code_build/linux_deps`，避免第二次构建从头下载。`pip install` 默认超时为 45 分钟，可用 `KSADK_BUILD_PIP_INSTALL_TIMEOUT_SECONDS` 调整。

构建完成会打印 zip 体积、解压体积和 Top 体积来源。只有当解压体积超过 500 MB 或 zip 超过 300 MB 时，才会提示切换 container 模式：

```bash
agentengine build . --mode container --push --registry <registry>
```

源码包里依赖本身很多时，优先建议业务拆分不必要依赖、使用环境变量显式关闭未用能力、或切到已有的 container 模式；本轮不建议把 `ksadk` 内置进固定 base 镜像，因为 SDK 更新频繁，固定 base 镜像会降低版本灵活性。

### 7.2 部署、存储与网络参数

以下参数在 `deploy`、`launch` 以及对应 framework 命令中统一存在：

- `--storage-size-gi`
- `--storage-mount-path`
- `--no-storage`

以下 network 参数在 `agentengine deploy`、`agentengine launch` 和 `agentengine openclaw deploy` 中统一存在：

- `--enable-public-access / --disable-public-access`
- `--enable-vpc-access`
- `--vpc-id`
- `--subnet-id`
- `--security-group-id`
- `--availability-zone`

示例：

```bash
agentengine deploy . --target serverless --storage-size-gi 50
agentengine launch . --target kce --storage-mount-path /home/node/.agentengine
agentengine hermes deploy --storage-size-gi 20
agentengine openclaw deploy --no-storage
```

VPC 网络示例：

```bash
agentengine deploy . \
  --target serverless \
  --disable-public-access \
  --enable-vpc-access \
  --vpc-id vpc-xxx \
  --subnet-id subnet-xxx \
  --security-group-id sg-xxx \
  --availability-zone cn-beijing-6a

agentengine launch . \
  --enable-vpc-access \
  --vpc-id vpc-xxx \
  --subnet-id subnet-xxx \
  --security-group-id sg-xxx

agentengine openclaw deploy \
  --enable-vpc-access \
  --vpc-id vpc-xxx \
  --subnet-id subnet-xxx \
  --security-group-id sg-xxx
```

配置文件也可写入 network。CLI 显式参数优先级高于配置文件：

```yaml
network:
  enable_public_access: false
  enable_vpc_access: true
  vpc_id: vpc-xxx
  subnet_id: subnet-xxx
  security_group_id: sg-xxx
  availability_zone: cn-beijing-6a

deploy:
  network:
    enable_public_access: false
    enable_vpc_access: true
    vpc_id: vpc-xxx
    subnet_id: subnet-xxx
    security_group_id: sg-xxx
```

行为要点：

- 不传时使用框架默认挂载目录。
- 容量会在客户端侧先做 `20~500Gi` 校验。
- `--no-storage` 会显式关闭默认 PVC 挂载。
- 只要开启 VPC 访问，或传入 `--vpc-id` / `--subnet-id` / `--security-group-id` 中任意一个，就必须同时具备 `VpcId`、`SubnetId`、`SecurityGroupId`。
- `--availability-zone` 是可选字段，不替代子网或安全组。

## 8. `agentengine files` 工作区文件管理

### 8.1 子命令清单

- `agentengine files list`
- `agentengine files upload`
- `agentengine files download`
- `agentengine files delete`
- `agentengine files push`
- `agentengine files pull`

### 8.2 路径语义

- 远端路径统一是 workspace 相对路径。
- `.`、空字符串和 `/` 会被解释为逻辑根 `workspace:/`。
- 输出里会同时给出：
  - 逻辑路径：`workspace:/docs/readme.md`
  - 真实路径：当运行时返回真实根目录时，显示绝对路径

### 8.3 常用示例

列目录：

```bash
agentengine files list --path .
agentengine files list --path docs --recursive
```

上传单文件：

```bash
agentengine files upload \
  --local-path ./report.md \
  --remote-path reports/report.md
```

下载文件：

```bash
agentengine files download \
  --remote-path reports/report.md \
  --output-path ./downloads/report.md
```

删除文件：

```bash
agentengine files delete --remote-path reports/report.md
```

推送目录：

```bash
agentengine files push \
  --local-dir ./dist \
  --remote-path releases/current
```

拉取目录：

```bash
agentengine files pull \
  --remote-path releases/current \
  --local-dir ./synced
```

### 8.4 `push / pull` 覆盖策略

- 默认不会强制覆盖已有文件。
- 加 `--force` 时，已有同名文件会进入 `overwritten` 结果集。
- 输出会区分：
  - `created`
  - `overwritten`
  - `skipped`

### 8.5 JSON 输出

所有 `files` 子命令都可配合 `--output json` 使用。典型字段包括：

- `workspace_root`
- `workspace_display_path`
- `workspace_real_path`
- `entry_count`
- `size_bytes`
- `size_human`
- `transport_mode`
- `results.created / overwritten / skipped`

### 8.6 大小限制

当前统一上限来自 `ksadk_runtime_common.workspace_files.constants`：

- 单文件上传上限：`100MB`

目录同步时还有两条额外限制：

- 本地目录中任一单文件不能超过上限
- 本地目录总大小也不能超过同一个上限

### 8.7 传输模式

CLI 内部会在两种模式间切换：

- `runtime_direct`：直连 runtime 的 `/_ksadk/workspace/v1/*`
- `action_proxy`：经由控制面 Action 调用

当前代码里的实际策略：

- 常规 agent：优先使用 `runtime_direct`
- OpenClaw：优先使用 `action_proxy`

更完整的协议与安全说明见 [工作区文件技术设计](./工作区文件技术设计.md)。

## 9. `agentengine agent invoke`

`agentengine agent invoke` 是当前主线命令；`agentengine invoke` 仍保留为兼容别名。

### 9.1 常见用法

```bash
agentengine agent invoke my-agent
agentengine agent invoke my-agent --message "你好"
agentengine agent invoke my-agent --transport chat
```

### 9.2 Hermes 远端 native 模式

`--local-workspace` 只支持 Hermes 的远端 native 模式：

```bash
agentengine agent invoke my-hermes \
  --transport native \
  --local-workspace ./local-workspace
```

可选指定远端目录：

```bash
agentengine agent invoke my-hermes \
  --transport native \
  --local-workspace ./local-workspace \
  --remote-workspace-path demos/hermes-pre
```

当前行为：

- 如果不传 `--remote-workspace-path`，默认使用本地目录名作为远端子目录名
- 本地空目录不会被同步
- 会先读取 `GetAgentUiBootstrap` 中的 `WorkspaceFiles.MaxUploadBytes`
- 如 bootstrap 获取失败，则回退到默认 `100MB`

### 9.3 约束

- `--remote-workspace-path` 必须与 `--local-workspace` 一起使用
- `--local-workspace` 不能和单次 `--message` 模式一起使用
- 当前只支持 Hermes 远端 native 模式

## 10. Hermes 命令主线

常用命令：

```bash
agentengine hermes deploy --name hermes-demo
agentengine hermes status
agentengine hermes open --chat
agentengine hermes connect
agentengine hermes exec -- status
```

部署相关默认值：

- 默认 PVC 大小：`20Gi`
- 默认挂载目录：`/home/node/.hermes`
- 默认 workspace 根目录：`/home/node/.hermes/workspace`

## 11. OpenClaw 命令主线

常用命令：

```bash
agentengine openclaw deploy
agentengine openclaw list
agentengine openclaw status
agentengine openclaw gateway doctor
agentengine openclaw channel status --probe
```

### 11.1 当前支持的记忆参数

```bash
agentengine openclaw deploy --memory-system openclaw_default
agentengine openclaw deploy \
  --memory-system mem0 \
  --mem0-instance-id <uuid> \
  --mem0-instance-name my-mem0 \
  --mem0-region cn-beijing-6
```

约束：

- `--memory-system mem0` 时必须传 `--mem0-instance-id`
- `--memory-system openclaw_default` 时不能再传 mem0 细节参数
- 不显式传 `--memory-system` 时，CLI 不会主动覆盖现有服务端配置

### 11.2 当前 mem0 行为

- OpenClaw 镜像内置 mem0 插件资产
- bootstrap 默认把 `openclaw-mem0` 视为延迟同步插件
- 只有在存在 `MEMORY_BACKEND_MANIFEST` 且渲染结果要求该插件时，才会把插件真正同步到实例目录
- 不使用 mem0 时，不会把该插件种到实例的持久化状态里

### 11.3 OpenClaw 存储默认值

- 默认 PVC 大小：`20Gi`
- 默认挂载目录：`/home/node/.openclaw`
- 默认 workspace 根目录：`/home/node/.openclaw/workspace`

更多 OpenClaw 细节见 [OpenClaw一键部署指南](./openclaw一键部署指南.md)。

## 12. 常见验证项

### 12.1 验证工作区文件

```bash
agentengine files list --output json
agentengine files push --local-dir ./workspace --remote-path demo
agentengine files pull --remote-path demo --local-dir ./downloaded --force
```

### 12.2 验证 Hermes remote workspace

```bash
agentengine agent invoke hermes-demo \
  --transport native \
  --local-workspace ./workspace
```

### 12.3 验证 OpenClaw mem0 参数

```bash
agentengine openclaw deploy \
  --memory-system mem0 \
  --mem0-instance-id e52b7fac-e641-4b34-b9f7-6b0b9f190cd4
```

## 13. 相关文档

- [ksadk技术设计](./ksadk技术设计.md)
- [工作区文件技术设计](./工作区文件技术设计.md)
- [记忆使用指南](./记忆使用指南.md)
- [知识库与记忆示例](./知识库与记忆示例.md)
- [OpenClaw一键部署指南](./openclaw一键部署指南.md)
- [DeepAgents说明](./DeepAgents说明.md)
