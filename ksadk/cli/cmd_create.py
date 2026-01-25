"""
ksadk create - 创建项目模板
"""

import click
from pathlib import Path
import questionary
from ksadk.cli.cmd_config import custom_style


TEMPLATES = {
    "adk": {
        "agent.py": '''"""
{package_name} - ADK Agent
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

model = LiteLlm(
    model=f"openai/{{os.getenv('OPENAI_MODEL_NAME', 'deepseek-v3.2')}}",
    api_base=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    stream=True,  # 启用流式输出
)


def hello(name: str) -> dict:
    """问候工具
    
    Args:
        name: 名字
    
    Returns:
        问候语
    """
    return {{"message": f"你好, {{name}}!"}}


root_agent = Agent(
    name="{package_name}",
    model=model,
    description="ADK示例 Agent",
    instruction="你是一个友好的助手。请用中文回复。",
    tools=[hello],
)
''',
    },
    "langchain": {
        "agent.py": '''"""
{package_name} - LangChain Agent
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

llm = ChatOpenAI(
    model=os.getenv("OPENAI_MODEL_NAME", "deepseek-v3.2"),
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    streaming=True,
)

prompt = ChatPromptTemplate.from_messages([
    ("system", "你是一个友好的助手。请用中文回复。"),
    ("human", "{{input}}")
])

root_agent = prompt | llm | StrOutputParser()
''',
    },
    "langgraph": {
        "agent.py": '''"""
{package_name} - LangGraph Agent
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated
import operator

llm = ChatOpenAI(
    model=os.getenv("OPENAI_MODEL_NAME", "deepseek-v3.2"),
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    streaming=True,
)


class State(TypedDict):
    messages: Annotated[list, operator.add]


def chat(state: State):
    messages = state["messages"]
    response = llm.invoke(messages)
    return {{"messages": [response]}}


graph = StateGraph(State)
graph.add_node("chat", chat)
graph.set_entry_point("chat")
graph.add_edge("chat", END)

root_agent = graph.compile()
''',
    },
}


@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.argument('project_name', required=False)
@click.option('--framework', '-f', type=click.Choice(['adk', 'langchain', 'langgraph']),
              default='langgraph', help='框架类型 (default: langgraph)')
def create(project_name: str, framework: str):
    """创建新的 Agent 项目
    
    PROJECT_NAME: 项目名称
    """
    # 如果没有提供项目名称，进入交互模式
    if not project_name:
        click.secho("🚀 初始化新项目", fg='blue', bold=True)
        click.echo("─" * 50)
        
        project_name = questionary.text(
            "请输入项目名称:",
            style=custom_style
        ).ask()
        
        if not project_name:
            click.echo("\n❌ 取消创建")
            raise SystemExit(0)
            
        framework = questionary.select(
            "请选择开发框架:",
            choices=['langgraph', 'langchain', 'adk'],
            default='langgraph',
            style=custom_style
        ).ask()
        
        if not framework:
            click.echo("\n❌ 取消创建")
            raise SystemExit(0)
            
    project_path = Path(project_name)
    
    if project_path.exists():
        click.secho(f"❌ 目录 '{project_name}' 已存在", fg='red')
        raise SystemExit(1)
    
    click.echo(f"📁 创建项目: {project_name}")
    click.echo(f"🔧 框架: {framework}")
    
    package_name = project_name.replace('-', '_')
    (project_path / package_name).mkdir(parents=True)
    
    # 检测全局配置
    from ksadk.configs.global_config import (
        global_config_exists,
        get_env_from_global_config,
    )
    
    global_env = {}
    if global_config_exists():
        global_env = get_env_from_global_config()
        if global_env:
            click.echo(click.style("ℹ️  检测到全局配置，已自动填充凭证", fg='cyan'))
    
    # .env - 生成配置文件
    # 如果有全局配置，使用全局配置的值；否则使用占位符
    # 如果有全局配置，使用全局配置的值；否则使用空字符串
    api_key = global_env.get("OPENAI_API_KEY", "")
    base_url = global_env.get("OPENAI_BASE_URL", "")
    model_name = global_env.get("OPENAI_MODEL_NAME", "")
    
    langfuse_public = global_env.get("LANGFUSE_PUBLIC_KEY", "")
    langfuse_secret = global_env.get("LANGFUSE_SECRET_KEY", "")
    langfuse_url = global_env.get("LANGFUSE_BASE_URL", "")
    
    ks_ak = global_env.get("KSYUN_ACCESS_KEY", "")
    ks_sk = global_env.get("KSYUN_SECRET_KEY", "")
    ks_region = global_env.get("KSYUN_REGION", "cn-beijing-6")
    ks_account = global_env.get("KSYUN_ACCOUNT_ID", "")
    
    # 构建 .env 内容
    env_content = f"""# ======================
# 模型配置 (必填, 可以从星流平台获取https://ksp.console.ksyun.com/#/apiKey)
# ======================
OPENAI_API_KEY={api_key}
"""
    
    # 可选字段：如果有值则启用，否则注释掉
    if base_url:
        env_content += f"OPENAI_BASE_URL={base_url}\n"
    else:
        env_content += "# OPENAI_BASE_URL=http://kspmas.ksyun.com/v1\n"
    
    if model_name:
        env_content += f"OPENAI_MODEL_NAME={model_name}\n"
    else:
        env_content += "# OPENAI_MODEL_NAME=deepseek-v3.2\n"
    
    env_content += """
# ======================
# 可观测性 (可选)
# ======================
"""
    if langfuse_public:
        env_content += f"LANGFUSE_PUBLIC_KEY={langfuse_public}\n"
    else:
        env_content += "# LANGFUSE_PUBLIC_KEY=pk-xxx\n"
    
    if langfuse_secret:
        env_content += f"LANGFUSE_SECRET_KEY={langfuse_secret}\n"
    else:
        env_content += "# LANGFUSE_SECRET_KEY=sk-xxx\n"
    
    if langfuse_url:
        env_content += f"LANGFUSE_BASE_URL={langfuse_url}\n"
    else:
        env_content += "# LANGFUSE_BASE_URL=https://cloud.langfuse.com\n"
    
    env_content += """
# ======================
# 金山云配置 (可选,需要部署时必选)
# ======================
"""
    if ks_ak:
        env_content += f"KSYUN_ACCESS_KEY={ks_ak}\n"
    else:
        env_content += "# KSYUN_ACCESS_KEY=your-api-key-here\n"
    
    if ks_sk:
        env_content += f"KSYUN_SECRET_KEY={ks_sk}\n"
    else:
        env_content += "# KSYUN_SECRET_KEY=your-api-secret-here\n"
    
    env_content += f"KSYUN_REGION={ks_region}\n"
    
    if ks_account:
        env_content += f"KSYUN_ACCOUNT_ID={ks_account}\n"
    else:
        env_content += "# KSYUN_ACCOUNT_ID=your-account-id\n"
    
    # 使用 utf-8-sig 编码 (带 BOM)，确保 Windows 程序正确识别为 UTF-8
    (project_path / ".env").write_text(env_content, encoding="utf-8-sig")
    
    # agentengine.yaml - Agent 配置
    (project_path / "agentengine.yaml").write_text(f"""# AgentEngine 项目配置
name: {package_name}
version: "1.0.0"

# 框架类型: adk, langchain, langgraph
framework: {framework}

# Agent 入口
entry_point: {package_name}/agent.py
agent_variable: root_agent

# 部署配置 (可选)
# deploy:
#   timeout: 300
#   memory: 512
""", encoding="utf-8-sig")
    
    # __init__.py
    (project_path / package_name / "__init__.py").write_text(f'''"""
{project_name} - KsADK Agent
"""
from .agent import root_agent
__all__ = ["root_agent"]
''', encoding="utf-8-sig")
    
    # agent.py
    template = TEMPLATES[framework]["agent.py"]
    (project_path / package_name / "agent.py").write_text(
        template.format(package_name=package_name),
        encoding="utf-8-sig"
    )
    
    # README.md
    (project_path / "README.md").write_text(f"""# {project_name}

基于 AgentEngine 创建的 {framework.upper()} Agent.

## 快速开始

```bash
cd {project_name}

# 1. 编辑 .env 填写 API Key
vim .env

# 2. 运行
agentengine run -i .    # 交互式
agentengine web .       # API Server
agentengine deploy .    # 部署到云端
```

## 项目结构

```
{project_name}/
├── .env                 # 环境变量 (API Key 等)
├── agentengine.yaml      # Agent 配置
├── requirements.txt      # Python 依赖
├── {package_name}/
│   ├── __init__.py
│   └── agent.py         # Agent 实现
└── README.md
```
""", encoding="utf-8-sig")

    # requirements.txt
    reqs = "requests_aws4auth\n"  # Minimum required for ksadk.common.auth
    if framework == "langchain":
        reqs += "langchain\nlangchain-openai\npython-dotenv\n"
    elif framework == "langgraph":
        reqs += "langchain\nlangchain-openai\nlanggraph\npython-dotenv\n"
    elif framework == "adk":
        reqs += "google-adk\npython-dotenv\n"
    
    (project_path / "requirements.txt").write_text(reqs, encoding="utf-8")
    
    click.echo(click.style("\n✅ 项目创建成功!", fg='green'))
    click.echo("")
    
    # 检测操作系统，提供对应的组合命令
    import platform
    is_windows = platform.system() == "Windows"
    
    if is_windows:
        # Windows: 使用 && 连接命令
        combined_cmd = f"cd {project_name} && agentengine config"
        run_cmd = f"cd {project_name} && agentengine run -i ."
    else:
        # Unix (macOS/Linux): 使用 && 连接命令
        combined_cmd = f"cd {project_name} && agentengine config"
        run_cmd = f"cd {project_name} && agentengine run -i ."
    
    click.echo(click.style("📋 快速开始 (复制并执行):", fg='cyan', bold=True))
    click.echo("")
    click.echo(click.style(f"   {combined_cmd}", fg='yellow'))
    click.echo("")
    click.echo(click.style("🚀 或直接运行 (环境变量中需包含模型 API Key):", fg='blue', bold=True))
    click.echo("")
    click.echo(click.style(f"   {run_cmd}", fg='blue'))
    click.echo("")
