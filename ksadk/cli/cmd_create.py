"""
ksadk create - 创建项目模板
"""

import click
from pathlib import Path


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
    model=f"openai/{{os.getenv('MODEL_NAME', 'deepseek-v3.2')}}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
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
    description="示例 Agent",
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
    model=os.getenv("MODEL_NAME", "deepseek-v3.2"),
    base_url=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
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
    model=os.getenv("MODEL_NAME", "deepseek-v3.2"),
    base_url=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
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
@click.argument('project_name')
@click.option('--framework', '-f', type=click.Choice(['adk', 'langchain', 'langgraph']),
              default='langgraph', help='框架类型 (default: langgraph)')
def create(project_name: str, framework: str):
    """创建新的 Agent 项目
    
    PROJECT_NAME: 项目名称
    """
    project_path = Path(project_name)
    
    if project_path.exists():
        click.secho(f"❌ 目录 '{project_name}' 已存在", fg='red')
        raise SystemExit(1)
    
    click.echo(f"📁 创建项目: {project_name}")
    click.echo(f"🔧 框架: {framework}")
    
    package_name = project_name.replace('-', '_')
    (project_path / package_name).mkdir(parents=True)
    
    # .env - 极简配置，只需填写 API Key
    (project_path / ".env").write_text("""# ======================
# 模型配置 (必填, 可以从星流平台获取https://ksp.console.ksyun.com/#/apiKey)
# ======================
OPENAI_API_KEY=your-api-key-here

# 可选：自定义 API 地址和模型
# OPENAI_API_BASE=http://kspmas.ksyun.com/v1
# OPENAI_MODEL_NAME=deepseek-v3.2

# ======================
# 可观测性 (可选)
# ======================
# LANGFUSE_PUBLIC_KEY=pk-xxx
# LANGFUSE_SECRET_KEY=sk-xxx

# ======================
# 金山云配置 (可选)
# ======================
# KSYUN_ACCESS_KEY=your-api-key-here
# KSYUN_SECRET_KEY=your-api-secret-here
# KSYUN_REGION=cn-beijing-6  # 默认区域
# KSYUN_ACCOUNT_ID=your-account-id
""")
    
    # agentengin.yaml - Agent 配置
    (project_path / "agentengin.yaml").write_text(f"""# AgentEngine 项目配置
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
""")
    
    # __init__.py
    (project_path / package_name / "__init__.py").write_text(f'''"""
{project_name} - KsADK Agent
"""
from .agent import root_agent
__all__ = ["root_agent"]
''')
    
    # agent.py
    template = TEMPLATES[framework]["agent.py"]
    (project_path / package_name / "agent.py").write_text(
        template.format(package_name=package_name)
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
agentengin run -i .    # 交互式
agentengin web .       # API Server
agentengin deploy .    # 部署到云端
```

## 项目结构

```
{project_name}/
├── .env                 # 环境变量 (API Key 等)
├── agentengin.yaml      # Agent 配置
├── {package_name}/
│   ├── __init__.py
│   └── agent.py         # Agent 实现
└── README.md
```
""")
    
    click.echo(click.style("\n✅ 项目创建成功!", fg='green'))
    click.echo(f"\n下一步:")
    click.echo(f"  cd {project_name}")
    click.echo(f"  vim .env              # 填写 OPENAI_API_KEY")
    click.echo(f"  agentengin run -i .   # 运行")
