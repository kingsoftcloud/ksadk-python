# KsADK Skills 功能设计

> 基于对 VeADK Skills Sandbox 的分析，提炼 ksadk-python 的 Skills 功能设计方案。

---

## 1. 背景与参考

### 1.1 VeADK Skills Sandbox 是什么

VeADK 的 Skills 是**可动态加载的工具包**，每个 Skill 是一个包含 `SKILL.md` 的目录。`SKILL.md` 用 frontmatter 定义 name/description，正文是给 LLM 的执行指令。

用户只需在 Agent 上声明 `skills=[...]`，运行时自动加载 Skill 元数据并注入对应工具。

### 1.2 两种执行模式

| 模式 | 配置 | 工具注入 | 执行位置 | 安全隔离 |
|------|------|---------|---------|---------|
| **local** | `skills_mode="local"` | skills_tool + bash + file_tools (7个) | 本地进程 | 无 |
| **sandbox** | `skills_mode="sandbox"` | execute_skills (1个) | 远程沙箱容器 | 有 |

### 1.3 沙箱执行本质

沙箱是一个**代码执行环境**（类似 Jupyter kernel），不是 HTTP 服务。通信方式是通过 API 发送代码过去执行（`OperationType: "RunCode"`）。

沙箱内预置了一个独立的 Agent，以 local 模式运行，加载并执行 skills。

```
外层 Agent (用户对话)
  │ 调用 execute_skills(workflow_prompt="...")
  ▼
execute_skills 构造代码:
  subprocess.Popen(["python", "agent.py", workflow_prompt],
                   cwd='/home/gem/veadk_skills')
  │ 通过 InvokeTool API 发送到沙箱
  ▼
沙箱容器内:
  agent.py (内层 Agent, local 模式)
    ├── skills_tool → 加载 SKILL.md
    ├── bash → 执行 skill 脚本
    └── stdout 输出结果
  │
  ▼
结果回传给外层 Agent
```

### 1.4 关键组件映射

| VeADK | ksadk 对应 |
|-------|-----------|
| `ve_request` + `InvokeTool` API | 沙箱 SDK (别的团队提供) |
| TOS (火山引擎对象存储) | KS3 (金山云对象存储) |
| `AGENTKIT_TOOL_ID` | `KSADK_SANDBOX_TOOL_ID` |
| `GetCallerIdentity` 获取 account_id | 金山云 STS 对应 API |
| `SKILL_SPACE_ID` | Skill Space 微服务 (已有) |
| 沙箱 `/home/gem/veadk_skills` | 沙箱 SDK 的代码执行环境 |
| `volcengine_sign` (HMAC-SHA256) | 金山云 API 签名方式 |
| 沙箱内 `agent.py` | **ksadk 提供** |

---

## 2. 分工边界

| 角色 | 负责内容 |
|------|---------|
| **沙箱 SDK (别的团队)** | 代码执行沙箱环境，提供 `RunCode` 类 API，接收代码并在隔离容器中执行 |
| **Skill Space 微服务 (已有)** | Skill 注册中心，提供 ListSkills / GetSkill / CreateSkill 等 API |
| **ksadk-python (我们)** | Skill 模型与加载、SkillsToolset、execute_skills 工具、沙箱内执行 Agent |

### 2.2 沙箱交付物

ksadk 交付以下文件，沙箱 SDK 团队将其打入沙箱镜像：

```
/home/ksadk/                         ← 沙箱内约定路径
├── agent.py                         ← 执行 Agent 入口 (ksadk 提供)
├── skills/                          ← 基础 skill (ksadk 提供，打入镜像)
│   ├── pdf/
│   │   ├── SKILL.md
│   │   └── scripts/
│   ├── docx/
│   ├── xlsx/
│   └── ...
└── requirements.txt                 ← 依赖清单
```

**Skills 更新策略：混合模式**

- **基础 skill 打包到镜像**：pdf、docx、xlsx 等常用 skill 随镜像发布，启动即用，无需下载
- **新 skill 运行时动态下载**：SkillsTool 发现不在镜像内的 skill 时，自动从 Skill Space / KS3 下载并解压到 session skills 目录

```
沙箱镜像 (静态):
  /home/ksadk/skills/     ← 基础 skill，随镜像发布

运行时 (动态):
  SkillsTool 发现新 skill
  → 从 Skill Space API 查询元数据
  → 从 KS3 下载 zip
  → 解压到 /tmp/ksadk/{session_id}/skills/
  → 加载 SKILL.md 执行
```

优势：基础 skill 零延迟启动，新 skill 无需重新打镜像即可使用。

---

## 3. 用户 API 设计

### 3.1 核心原则：用户只声明 skills，运行时自动决定执行模式

用户不需要理解 local vs sandbox 的区别，不需要手动添加 `execute_skills` 工具。

```python
# 最简用法 — 90% 场景，自动模式
agent = Agent(
    name="my_agent",
    instruction="根据用户需求执行 skills 完成任务",
    skills=["/path/to/skills"],       # 只声明 skill 来源
)

# 高级用法 — 显式指定模式
agent = Agent(
    name="my_agent",
    instruction="...",
    skills=["/path/to/skills"],
    skills_mode="local",              # 强制本地执行
)
```

### 3.2 自动模式判断

```
                    用户 Agent 声明 skills=[...]
                              │
                    ┌─────────┴──────────┐
                    │                    │
              本地运行 (ksadk run)     云端运行 (agentengine-server)
                    │                    │
              自动用 local 模式     自动用 sandbox 模式
              注入 SkillsToolset    注入 execute_skills
              本地直接执行          沙箱隔离执行
```

| 环境 | 检测方式 | 自动选择的模式 |
|------|---------|--------------|
| `ksadk run` 本地调试 | 无 `KSADK_SANDBOX_TOOL_ID` | local |
| `ksadk web` 本地 UI | 无 `KSADK_SANDBOX_TOOL_ID` | local |
| agentengine-server 云端部署 | 有 `KSADK_SANDBOX_TOOL_ID` | sandbox |
| 用户显式 `skills_mode="local"` | 字段值 | local (强制) |
| 用户显式 `skills_mode="sandbox"` | 字段值 | sandbox (强制) |

### 3.3 skills 参数说明

```python
class Agent:
    skills: list[str] = []           # Skill 来源列表
    skills_mode: str | None = None   # 执行模式: "local" | "sandbox" | None(自动)
```

`skills` 列表中的每一项可以是：

| 类型 | 示例 | 说明 |
|------|------|------|
| 本地目录路径 | `"/home/user/my_skills"` | 从本地文件系统加载 |
| Skill Space ID | `"ss-abc123"` | 从 Skill Space 微服务加载 |

混合使用也支持：`skills=["/local/skills", "ss-abc123"]`

---

## 4. 架构设计

### 4.1 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│ 外层 Agent (用户对话)                                            │
│                                                                  │
│  skills_mode="local"              skills_mode="sandbox"         │
│  ┌─────────────────────┐          ┌────────────────────┐        │
│  │   SkillsToolset     │          │  execute_skills    │        │
│  │  ┌───────────────┐  │          │                    │        │
│  │  │ skills_tool   │  │          │  构造代码:          │        │
│  │  │ read_file     │  │          │  python agent.py   │        │
│  │  │ write_file    │  │          │  <workflow_prompt> │        │
│  │  │ edit_file     │  │          │                    │        │
│  │  │ bash          │  │          │  调用沙箱 SDK      │        │
│  │  │ register_     │  │          │  RunCode API       │        │
│  │  │   skills      │  │          └────────┬───────────┘        │
│  │  └───────────────┘  │                   │                    │
│  └─────────────────────┘                   │                    │
└────────────────────────────────────────────│────────────────────┘
                                             │
                                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ 沙箱容器 (沙箱 SDK 提供)                                         │
│                                                                  │
│  /home/ksadk/                                                    │
│  ├── agent.py              ← ksadk 提供的执行 Agent 入口         │
│  ├── skills/               ← 预置 skills                        │
│  │   ├── pdf/                                                    │
│  │   │   ├── SKILL.md                                            │
│  │   │   └── scripts/                                            │
│  │   ├── docx/                                                   │
│  │   └── ...                                                     │
│  └── requirements.txt                                            │
│                                                                  │
│  agent.py 内部:                                                  │
│  ┌─────────────────────────────────┐                             │
│  │ 内层 Agent (skills_mode=local)  │                             │
│  │  ├── skills_tool → 加载 SKILL.md│                             │
│  │  ├── bash → 执行 skill 脚本    │                             │
│  │  └── stdout 输出结果           │                             │
│  └─────────────────────────────────┘                             │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 调用全流程 (sandbox 模式)

```
1. 用户: "帮我写一个pdf skill并注册到skill space"
       │
2. 外层 Agent 判断需要执行 skill
       │
3. 调用 execute_skills(workflow_prompt="帮我写一个pdf skill并注册到skill space")
       │
4. execute_skills 函数:
   ├── 获取认证 (AK/SK 或 IAM 临时凭证)
   ├── 构造代码:
   │   subprocess.Popen(
   │       ["python", "agent.py", "帮我写一个pdf skill并注册到skill space"],
   │       cwd='/home/ksadk',
   │       env={
   │           "KSADK_SKILLS_DIR": "/home/ksadk/skills",
   │           "SKILL_SPACE_ID": "ss-abc123",
   │           "KS3_SKILLS_DIR": "ks3://ksadk-platform-{account_id}/skills/",
   │           "KSADK_API_BASE": "...",
   │           "KSADK_API_KEY": "...",
   │       }
   │   )
   ├── 注入环境变量
   └── 调用沙箱 SDK RunCode API
       │
5. 沙箱容器内执行:
   python agent.py "帮我写一个pdf skill并注册到skill space"
       │
6. 内层 Agent (skills_mode=local):
   ├── skills_tool("pdf") → 加载 pdf skill 的 SKILL.md
   ├── bash → 执行 skill 脚本，生成代码
   ├── write_file → 保存生成的 skill 文件
   └── register_skills → 注册到 Skill Space
       │
7. stdout 输出结果
       │
8. execute_skills 格式化结果返回给外层 Agent
       │
9. 外层 Agent 回复用户
```

### 4.3 调用全流程 (local 模式)

```
1. 用户: "帮我写一个pdf skill并注册到skill space"
       │
2. Agent 直接调用 SkillsToolset 中的工具:
   ├── skills_tool("pdf") → 加载 pdf skill 的 SKILL.md
   │   ├── skill 在本地? → symlink 到 session skills 目录
   │   ├── skill 在 Skill Space? → 从 KS3 下载 zip → 解压到 session skills 目录
   │   └── 返回 SKILL.md 内容
   ├── bash → 执行 skill 脚本
   ├── write_file → 生成文件
   └── register_skills → 注册到 Skill Space
       │
3. Agent 回复用户
```

---

## 5. Skill 数据模型

### 5.1 Skill 模型

```python
# ksadk/skills/skill.py
from pydantic import BaseModel

class Skill(BaseModel):
    name: str                            # Skill 名称
    description: str                     # Skill 描述 (给 LLM 看)
    path: str                            # 本地路径或 KS3 路径
    skill_space_id: str | None = None    # 所属 Skill Space ID
    bucket_name: str | None = None       # KS3 bucket 名称
    checklist: list[dict[str, str]] = [] # 执行检查项
    id: str | None = None                # Skill 唯一 ID (云端)
```

### 5.2 SKILL.md 格式

```markdown
---
name: pdf-processing
description: PDF处理技能，支持加载PDF、编辑PDF和从PDF中提取文字信息
---

# PDF Processing

## When to use
当用户需要处理 PDF 文件时使用此技能。

## Prerequisites
pip install pymupdf reportlab

## Capability overview
- 加载并读取 PDF 文件
- 编辑 PDF (合并、拆分、加水印)
- 提取 PDF 中的文字信息

## Usage
python scripts/run.py <subcommand> <args>
```

### 5.3 Skill 加载

```python
# ksadk/skills/loader.py

def load_skills_from_directory(dir_path: str) -> list[Skill]:
    """扫描目录下所有含 SKILL.md 的子目录，解析 frontmatter"""
    ...

def load_skills_from_cloud(skill_space_id: str) -> list[Skill]:
    """从 Skill Space 微服务加载 skill 列表"""
    ...

def load_skills(sources: list[str]) -> dict[str, Skill]:
    """统一加载入口，自动识别本地路径或 Skill Space ID"""
    skills_dict = {}
    for source in sources:
        path = Path(source)
        if path.exists() and path.is_dir():
            for skill in load_skills_from_directory(source):
                skills_dict[skill.name] = skill
        else:
            # 视为 Skill Space ID
            for skill in load_skills_from_cloud(source):
                skills_dict[skill.name] = skill
    return skills_dict
```

---

## 6. SkillsToolset 设计

### 6.1 工具清单

| 工具名 | 作用 | local | sandbox |
|--------|------|-------|---------|
| `skills_tool` | 按 name 加载 SKILL.md | Yes | No |
| `read_file` | 读取文件内容 | Yes | No |
| `write_file` | 写入/创建文件 | Yes | No |
| `edit_file` | 精确替换文件内容 | Yes | No |
| `bash` | 执行 shell 命令 | Yes | No |
| `register_skills` | 注册 skill 到 Skill Space | Yes | No |
| `execute_skills` | 远程沙箱执行 workflow | No | Yes |

### 6.2 SkillsToolset 实现

```python
# ksadk/tools/skills_toolset.py

class SkillsToolset(BaseToolset):
    def __init__(self, skills: dict[str, Skill], skills_mode: str):
        self.skills_mode = skills_mode
        self._tools = {
            "skills": SkillsTool(skills),
            "read_file": FunctionTool(read_file_tool),
            "write_file": FunctionTool(write_file_tool),
            "edit_file": FunctionTool(edit_file_tool),
            "bash": FunctionTool(bash_tool),
            "register_skills": FunctionTool(register_skills_tool),
        }

    async def get_tools(self) -> list[BaseTool]:
        match self.skills_mode:
            case "local":
                return list(self._tools.values())
            case "sandbox":
                return []   # sandbox 模式不注入本地工具
```

### 6.3 SkillsTool 核心逻辑

```python
# ksadk/tools/skills_tool.py

class SkillsTool(BaseTool):
    """按 name 加载 skill 的 SKILL.md 内容"""

    def _invoke_skill(self, skill_name: str, tool_context: ToolContext) -> str:
        working_dir = get_session_path(session_id=tool_context.session.id)
        skill_dir = working_dir / "skills"

        if skill_name in self.skills:
            skill = self.skills[skill_name]
            if skill.skill_space_id:
                # 从 KS3 下载 zip → 解压到 session skills 目录
                self._download_from_ks3(skill, skill_dir)
            else:
                # 本地 symlink
                self._symlink_local(skill, skill_dir)
        else:
            # 尝试从 KS3_SKILLS_DIR 下载
            self._download_from_ks3_dir(skill_name, skill_dir)

        # 读取 SKILL.md
        skill_file = skill_dir / skill_name / "SKILL.md"
        return self._format_skill_content(skill_name, skill_file.read_text(), skill_dir)
```

### 6.4 Session Path 文件隔离

每个 session 有独立的工作目录：

```
/tmp/ksadk/{session_id}/
├── skills/      → skill 文件 (从云端下载或本地 symlink)
├── uploads/     → 用户上传的文件
└── outputs/     → Agent 生成的输出文件
```

```python
# ksadk/skills/session_path.py

def initialize_session_path(session_id: str) -> Path:
    base_path = Path("/tmp") / "ksadk"
    session_path = base_path / session_id
    (session_path / "skills").mkdir(parents=True, exist_ok=True)
    (session_path / "uploads").mkdir(parents=True, exist_ok=True)
    (session_path / "outputs").mkdir(parents=True, exist_ok=True)
    return session_path
```

---

## 7. execute_skills 设计

### 7.1 外层 Agent 的沙箱执行工具

```python
# ksadk/tools/builtin_tools/execute_skills.py

def execute_skills(workflow_prompt: str, tool_context: ToolContext = None) -> str:
    """在远程沙箱中执行 skills 工作流"""

    timeout = 900  # 15 分钟硬限制

    # 1. 获取认证
    ak, sk, headers = resolve_credentials(tool_context)

    # 2. 获取 account_id (用于构造 KS3 bucket)
    account_id = get_account_id(ak, sk)

    # 3. 构造环境变量
    env_vars = {
        "KSADK_SKILLS_DIR": "/home/ksadk/skills",
        "SKILL_SPACE_ID": os.getenv("SKILL_SPACE_ID", ""),
        "KS3_SKILLS_DIR": f"ks3://ksadk-platform-{account_id}/skills/",
        "TOOL_USER_SESSION_ID": f"{agent_name}_{user_id}_{session_id}",
        "KSADK_API_BASE": os.getenv("KSADK_API_BASE", ""),
        "KSADK_API_KEY": os.getenv("KSADK_API_KEY", ""),
        "KSADK_MODEL": os.getenv("KSADK_MODEL", ""),
    }

    # 4. 构造沙箱执行代码
    cmd = ["python", "agent.py", workflow_prompt]
    code = generate_sandbox_code(cmd, env_vars, timeout)

    # 5. 调用沙箱 SDK RunCode API
    result = sandbox_sdk.run_code(
        tool_id=os.getenv("KSADK_SANDBOX_TOOL_ID"),
        code=code,
        timeout=timeout,
        session_id=tool_user_session_id,
    )

    # 6. 格式化并返回结果
    return format_execution_result(result)
```

### 7.2 沙箱执行代码生成

```python
def generate_sandbox_code(cmd: list[str], env_vars: dict, timeout: int) -> str:
    """生成在沙箱内执行的 Python 代码"""
    return f"""
import subprocess
import os
import time
import select
import sys

env = os.environ.copy()
for key, value in {env_vars!r}.items():
    if key not in env:
        env[key] = value

process = subprocess.Popen(
    {cmd!r},
    cwd='/home/ksadk',
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    env=env,
    bufsize=1,
    universal_newlines=True
)

start_time = time.time()
timeout = {timeout - 10}

with open('/tmp/agent.log', 'w') as log_file:
    while True:
        if time.time() - start_time > timeout:
            process.kill()
            print("Process timeout", end='', file=sys.stderr)
            break

        reads = [process.stdout.fileno(), process.stderr.fileno()]
        ret = select.select(reads, [], [], 1)

        for fd in ret[0]:
            if fd == process.stdout.fileno():
                line = process.stdout.readline()
                if line:
                    log_file.write(line)
                    log_file.flush()
                    print(line, end='')
            if fd == process.stderr.fileno():
                line = process.stderr.readline()
                if line:
                    log_file.write(line)
                    log_file.flush()
                    print(line, end='', file=sys.stderr)

        if process.poll() is not None:
            break

    for line in process.stdout:
        print(line, end='')
    for line in process.stderr:
        print(line, end='', file=sys.stderr)
"""
```

---

## 8. 沙箱内执行 Agent

### 8.1 入口 agent.py

这是打包到沙箱镜像中的 Agent，由外层 `execute_skills` 通过 `python agent.py <workflow_prompt>` 启动。

```python
# ksadk/sandbox/agent.py

import sys
import os
import asyncio

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.models.lite_llm import LiteLlm
from google.genai.types import Content, Part

from ksadk.skills import SkillsToolset, load_skills
from ksadk.skills.session_path import initialize_session_path


async def run(workflow_prompt: str):
    """沙箱内执行 Agent 主函数"""

    # 1. 加载预置 skills
    skills_dir = os.getenv("KSADK_SKILLS_DIR", "/home/ksadk/skills")
    skills_dict = load_skills([skills_dir])

    # 2. 模型配置 (环境变量注入)
    model = LiteLlm(
        model=os.getenv("KSADK_MODEL", "openai/doubao-1.5-pro"),
        api_base=os.getenv("KSADK_API_BASE"),
        api_key=os.getenv("KSADK_API_KEY"),
    )

    # 3. 创建 Agent (local 模式，注入全部工具)
    agent = Agent(
        name="skill_executor",
        instruction=workflow_prompt,
        model=model,
        tools=[SkillsToolset(skills_dict, skills_mode="local")],
    )

    # 4. 初始化 session
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="ksadk_sandbox",
        user_id="sandbox_user",
    )
    initialize_session_path(session.id)

    # 5. 执行
    runner = Runner(
        agent=agent,
        session_service=session_service,
        app_name="ksadk_sandbox",
    )

    async for event in runner.run_async(
        user_id="sandbox_user",
        session_id=session.id,
        new_message=Content(parts=[Part(text=workflow_prompt)]),
    ):
        if event.is_final_response():
            print(event.content.parts[0].text)


if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    asyncio.run(run(prompt))
```

### 8.2 沙箱镜像内目录结构

```
/home/ksadk/
├── agent.py                    # 执行 Agent 入口 (ksadk 提供，打入镜像)
├── skills/                     # 基础 skill (打入镜像，启动即用)
│   ├── pdf/
│   │   ├── SKILL.md
│   │   └── scripts/
│   ├── docx/
│   ├── xlsx/
│   └── ...
└── requirements.txt
```

**运行时动态加载**：不在镜像内的 skill，SkillsTool 自动从 Skill Space / KS3 下载解压到 session 目录。

---

## 9. Runner 集成

### 9.1 ADKRunner 自动注入

```python
# ksadk/runners/adk_runner.py 新增方法

def _inject_skill_toolsets(self):
    """根据 skills_mode 自动注入 skill 相关工具"""
    if not self.agent_skills:
        return

    # 加载 skills
    skills_dict = load_skills(self.agent_skills)

    # 解析模式
    mode = self.agent.skills_mode or self._auto_detect_skills_mode()

    if mode == "local":
        # 注入 SkillsToolset
        toolset = SkillsToolset(skills_dict, skills_mode="local")
        self.root_agent.tools.append(toolset)
        # 修改 instruction，提示使用 skills_tool
        self.root_agent.instruction += (
            "\n\nYou can use the skills by calling the `skills_tool` tool.\n"
        )

    elif mode == "sandbox":
        # 只注入 execute_skills
        self.root_agent.tools.append(execute_skills)
        # 修改 instruction，提示使用 execute_skills
        self.root_agent.instruction += (
            "\n\nYou can use the skills by calling the `execute_skills` tool.\n"
        )

def _auto_detect_skills_mode(self) -> str:
    """自动检测 skills 执行模式"""
    if os.getenv("KSADK_SANDBOX_TOOL_ID"):
        return "sandbox"
    return "local"
```

---

## 10. 环境变量

| 变量 | 用途 | 使用场景 |
|------|------|---------|
| `KSADK_SKILLS_DIR` | 预置 skills 目录路径 | 沙箱内 |
| `SKILL_SPACE_ID` | Skill Space ID | 外层 + 沙箱内 |
| `KS3_SKILLS_DIR` | KS3 存储 skills 的路径 | 沙箱内 |
| `KSADK_SANDBOX_TOOL_ID` | 沙箱工具 ID (存在则 sandbox 模式) | 外层 |
| `KSADK_SANDBOX_HOST` | 沙箱服务地址 | 外层 |
| `KSADK_SANDBOX_REGION` | 沙箱服务区域 | 外层 |
| `KSADK_API_BASE` | LLM API 地址 | 沙箱内 |
| `KSADK_API_KEY` | LLM API Key | 沙箱内 |
| `KSADK_MODEL` | LLM 模型名称 | 沙箱内 |
| `KINGSOFTCLOUD_ACCESS_KEY` | 金山云 AK | 外层 + 沙箱内 |
| `KINGSOFTCLOUD_SECRET_KEY` | 金山云 SK | 外层 + 沙箱内 |

---

## 11. 新增模块清单

| 模块 | 文件路径 | 作用 |
|------|---------|------|
| Skill 模型 | `ksadk/skills/skill.py` | `Skill` Pydantic 数据模型 |
| Skill 加载 | `ksadk/skills/loader.py` | 从目录/云端加载 skill 列表 |
| Session Path | `ksadk/skills/session_path.py` | 会话文件隔离管理 |
| SkillsToolset | `ksadk/tools/skills_toolset.py` | local 模式工具集 |
| SkillsTool | `ksadk/tools/skills_tool.py` | 按 name 加载 SKILL.md |
| File Tools | `ksadk/tools/skills_tools/file_tools.py` | read_file, write_file, edit_file |
| Bash Tool | `ksadk/tools/skills_tools/bash_tool.py` | shell 命令执行 |
| Register Tool | `ksadk/tools/skills_tools/register_skills_tool.py` | 注册 skill 到 Skill Space |
| execute_skills | `ksadk/tools/builtin_tools/execute_skills.py` | 远程沙箱执行工具 |
| 沙箱 Agent | `ksadk/sandbox/agent.py` | 沙箱内运行的执行 Agent |
| Skill Space 客户端 | `ksadk/skills/skill_space_client.py` | Skill Space 微服务 API 客户端 |
| KS3 集成 | `ksadk/integrations/ks3.py` | KS3 对象存储下载 skill |

---

## 12. 实施路径

### Phase 1: 本地模式 (最小可用，无外部依赖)

```
Skill 模型 + 本地目录加载 + SkillsToolset (local) + Session Path
→ 用户可以在本地 ksadk run 中使用 skills
```

交付物：
- `ksadk/skills/skill.py`
- `ksadk/skills/loader.py` (仅本地目录加载)
- `ksadk/skills/session_path.py`
- `ksadk/tools/skills_toolset.py`
- `ksadk/tools/skills_tool.py`
- `ksadk/tools/skills_tools/file_tools.py`
- `ksadk/tools/skills_tools/bash_tool.py`
- Runner `_inject_skill_toolsets()`

### Phase 2: 云端 Skill 加载

```
Skill Space 客户端 + KS3 下载 + register_skills
→ 用户可以从 Skill Space 加载 skills，也可以注册新 skill
```

交付物：
- `ksadk/skills/skill_space_client.py`
- `ksadk/skills/loader.py` (增加云端加载)
- `ksadk/integrations/ks3.py`
- `ksadk/tools/skills_tools/register_skills_tool.py`

### Phase 3: 沙箱执行

```
execute_skills + 沙箱内 Agent + 沙箱 SDK 对接
→ 完整的 sandbox 模式
```

交付物：
- `ksadk/tools/builtin_tools/execute_skills.py`
- `ksadk/sandbox/agent.py`
- Runner sandbox 模式支持
- 沙箱 SDK 集成

### Phase 4: 动态重载与优化

```
Skill 动态检测 + 变更重载 + checklist + 遥测指标
```

交付物：
- `ksadk/skills/check_skills_callback.py`
- Skill 执行遥测指标
- Checklist 状态管理

---

## 13. 两种模式对比总结

| | Local 模式 | Sandbox 模式 |
|---|---|---|
| **工具注入** | SkillsToolset (7个工具) | execute_skills (1个工具) |
| **执行位置** | 本地进程 | 远程沙箱容器 |
| **Skill 加载** | 本地读取 / symlink / KS3 下载 | 沙箱内预装 / KS3 下载 |
| **安全隔离** | 无，直接操作本地文件系统 | 有，在沙箱内隔离执行 |
| **Agent 数量** | 单层 Agent | 双层 Agent (外层对话 + 内层执行) |
| **适用场景** | 本地开发调试 | 云端托管生产环境 |
| **超时** | 无硬性限制 | 900秒 (15分钟) |
| **凭证** | 不需要额外凭证 | 需要金山云 AK/SK 或 IAM 临时凭证 |
| **外部依赖** | 无 | 沙箱 SDK + KS3 |
