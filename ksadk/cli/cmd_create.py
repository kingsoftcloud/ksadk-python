"""
ksadk create - 创建项目模板
"""

import click
from pathlib import Path
import shutil
import questionary
from ksadk.cli.cmd_config import custom_style
from ksadk.cli.ui import (
    print_error,
    print_info,
    print_kv,
    print_rule,
    print_success,
    print_title,
    print_warn,
)


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
    model=f"openai/{{os.getenv('OPENAI_MODEL_NAME', 'glm-5.1')}}",
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
    model=os.getenv("OPENAI_MODEL_NAME", "glm-5.1"),
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
    model=os.getenv("OPENAI_MODEL_NAME", "glm-5.1"),
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
    "deepagents": {
        "agent.py": '''"""
{package_name} - DeepAgents Agent
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model=os.getenv("OPENAI_MODEL_NAME", "glm-5.1"),
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    streaming=True,
)

root_agent = create_deep_agent(model=llm)
''',
    },
    "openclaw": {
        "agent.py": '''"""
{package_name} - OpenClaw placeholder

OpenClaw 使用预构建容器运行，当前目录主要用于保存 .env 和 .agentengine.state。
此入口仅用于兼容项目模板结构。
"""

root_agent = None
''',
    },
}


def _detect_framework(content: str) -> str:
    """检测 Agent 文件使用的框架"""
    if 'from deepagents' in content or 'import deepagents' in content or 'create_deep_agent(' in content:
        return 'deepagents'
    elif 'from langgraph' in content or 'import langgraph' in content:
        return 'langgraph'
    elif 'from google.adk' in content or 'import google.adk' in content:
        return 'adk'
    elif 'from langchain' in content or 'import langchain' in content:
        return 'langchain'
    return 'unknown'


def _detect_agent_variable(content: str) -> str | None:
    """检测 Agent 变量名"""
    import re
    
    # 优先级匹配: root_agent > compiled graph > StateGraph > Agent()
    patterns = [
        (r'^\s*(root_agent)\s*=', 'root_agent'),
        (r'^\s*(root_agent)\s*:\s*[^=]+\s*=', 'root_agent'),  # type annotated root_agent
        (r'^\s*(\w+)\s*=\s*\w*graph\w*\.compile\(', None),  # e.g., agent = graph.compile()
        (r'^\s*(\w+)\s*=\s*StateGraph', None),  # e.g., graph = StateGraph(...)
        (r'^\s*(\w+)\s*=\s*Agent\(', None),  # ADK: agent = Agent(...)
        (r'^\s*(\w+)\s*=\s*create_react_agent\(', None),  # create_react_agent
        (r'^\s*(\w+)\s*=\s*create_agent\(', None),  # langchain create_agent
        (r'^\s*(\w+)\s*=\s*build_agent\(', None),  # adapter style build_agent
    ]
    
    for pattern, fixed_name in patterns:
        match = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
        if match:
            return fixed_name if fixed_name else match.group(1)
    
    return None


def _load_entry_from_agentengine_yaml(directory: Path) -> tuple[Path, str] | None:
    """
    从已有 agentengine.yaml 读取入口信息
    返回 (入口文件路径, Agent 变量名) 或 None
    """
    config_path = directory / "agentengine.yaml"
    if not config_path.exists():
        return None

    try:
        import yaml  # type: ignore

        config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    except Exception:
        return None

    entry_point = config.get("entry_point")
    if not entry_point or not isinstance(entry_point, str):
        return None

    # 兼容 Windows 路径分隔符
    entry_path = directory / Path(entry_point.replace("\\", "/"))
    if not entry_path.exists() or not entry_path.is_file():
        return None

    agent_var = config.get("agent_variable")
    if not isinstance(agent_var, str) or not agent_var.strip():
        agent_var = "root_agent"

    return entry_path, agent_var


def _load_framework_from_agentengine_yaml(directory: Path) -> str | None:
    """从已有 agentengine.yaml 读取 framework"""
    config_path = directory / "agentengine.yaml"
    if not config_path.exists():
        return None

    try:
        import yaml  # type: ignore

        config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    except Exception:
        return None

    framework = config.get("framework")
    if not isinstance(framework, str):
        return None
    framework = framework.strip().lower()
    if framework in {"adk", "langchain", "langgraph", "deepagents", "openclaw", "hermes"}:
        return framework
    return None


def _iter_python_files(directory: Path):
    """递归迭代目录下的 Python 文件（排除常见无关目录）"""
    excluded_dirs = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
    }

    for py_file in directory.rglob("*.py"):
        rel_parts = py_file.relative_to(directory).parts
        if any(part in excluded_dirs or part.startswith(".venv") for part in rel_parts):
            continue
        yield py_file


def _find_entry_file(directory: Path) -> tuple[Path, str] | None:
    """
    在目录中查找入口文件
    返回 (入口文件路径, Agent 变量名) 或 None
    """
    # 1) 优先读取已有 agentengine.yaml（兼容已配置项目）
    config_entry = _load_entry_from_agentengine_yaml(directory)
    if config_entry:
        entry_path, configured_var = config_entry
        try:
            content = entry_path.read_text(encoding="utf-8")
            detected_var = _detect_agent_variable(content)
            return (entry_path, detected_var or configured_var)
        except Exception:
            return (entry_path, configured_var)

    # 2) 按候选文件名递归查找（agent.py/main.py/app.py/...）
    entry_candidates = ["agent.py", "main.py", "app.py", "agentengine_adapter.py", "__init__.py"]
    for candidate in entry_candidates:
        candidate_files = sorted(
            (p for p in _iter_python_files(directory) if p.name == candidate),
            key=lambda p: (len(p.relative_to(directory).parts), str(p)),
        )
        for entry_path in candidate_files:
            try:
                content = entry_path.read_text(encoding="utf-8")
            except Exception:
                continue
            agent_var = _detect_agent_variable(content)
            if agent_var:
                return (entry_path, agent_var)

    # 3) 全量扫描，按得分选择最可能入口
    best_match: tuple[int, Path, str] | None = None
    for py_file in _iter_python_files(directory):
        if py_file.name.startswith("_") and py_file.name != "__init__.py":
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        agent_var = _detect_agent_variable(content)
        if not agent_var:
            continue

        rel = py_file.relative_to(directory)
        depth = len(rel.parts)
        score = 0
        if agent_var == "root_agent":
            score += 100
        if py_file.name in entry_candidates:
            score += 50
        if "src" in rel.parts:
            score += 20
        score -= depth

        if best_match is None or score > best_match[0]:
            best_match = (score, py_file, agent_var)

    if best_match:
        return (best_match[1], best_match[2])

    return None


def _fix_dotenv_paths_in_file(file_path: Path, depth: int = 1) -> bool:
    """
    修复单个文件中的 load_dotenv 路径
    depth: 文件相对于项目根目录的深度（用于确定需要多少个 .parent）
    返回是否修改了文件
    """
    import re
    
    try:
        content = file_path.read_text(encoding='utf-8')
    except Exception:
        return False
    
    # 构建替换的 parent 链
    parent_chain = '.parent' * depth
    
    # 匹配各种 load_dotenv 模式
    patterns = [
        # load_dotenv(Path(__file__).parent / ".env")
        (r'(load_dotenv\s*\(\s*Path\s*\(\s*__file__\s*\)\s*)\.parent(\s*/\s*["\']\.env["\'])',
         rf'\1{parent_chain}\2'),
        # load_dotenv(Path(__file__).parent.parent / ".env") -> 根据 depth 调整
    ]
    
    modified = False
    new_content = content
    
    for pattern, replacement in patterns:
        new_content_temp = re.sub(pattern, replacement, new_content)
        if new_content_temp != new_content:
            new_content = new_content_temp
            modified = True
    
    if modified:
        file_path.write_text(new_content, encoding='utf-8')
    
    return modified


def _fix_dotenv_paths_recursive(directory: Path, depth: int = 1) -> int:
    """
    递归修复目录中所有 .py 文件的 load_dotenv 路径
    返回修复的文件数量
    """
    fixed_count = 0
    
    for py_file in directory.rglob('*.py'):
        # 计算相对深度
        rel_path = py_file.relative_to(directory)
        file_depth = len(rel_path.parts) + depth - 1  # 相对于项目根目录
        
        if _fix_dotenv_paths_in_file(py_file, file_depth):
            fixed_count += 1
    
    return fixed_count


def _fix_nested_imports(file_path: Path, subdirs: list[str]) -> bool:
    """
    修复文件中的嵌套目录导入
    
    当源目录包含子包目录（如 my_agent/）时，
    入口文件中的 `from my_agent import xxx` 需要改为 `from .my_agent import xxx`
    
    Args:
        file_path: 需要修复的文件
        subdirs: 同级子目录名列表
    
    Returns:
        是否修改了文件
    """
    import re
    
    if not subdirs:
        return False
    
    try:
        content = file_path.read_text(encoding='utf-8')
    except Exception:
        return False
    
    original = content
    
    # 为每个子目录生成修复规则
    for subdir in subdirs:
        # from my_agent import xxx -> from .my_agent import xxx
        pattern = rf'^(from\s+)({re.escape(subdir)})(\s+import\s+)'
        replacement = rf'\1.\2\3'
        content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
        
        # import my_agent -> from . import my_agent
        # 只处理独立的 import 语句
        pattern_import = rf'^import\s+({re.escape(subdir)})\s*$'
        replacement_import = rf'from . import \1'
        content = re.sub(pattern_import, replacement_import, content, flags=re.MULTILINE)
    
    if content != original:
        file_path.write_text(content, encoding='utf-8')
        return True
    
    return False


def _generate_env_content(global_env: dict) -> str:
    """生成 .env 文件内容"""
    api_key = global_env.get("OPENAI_API_KEY", "")
    base_url = global_env.get("OPENAI_BASE_URL", "")
    model_name = global_env.get("OPENAI_MODEL_NAME", "")
    ks_ak = global_env.get("KSYUN_ACCESS_KEY", "")
    ks_sk = global_env.get("KSYUN_SECRET_KEY", "")
    ks_region = global_env.get("KSYUN_REGION", "cn-beijing-6")
    ks_account = global_env.get("KSYUN_ACCOUNT_ID", "")
    
    env_content = f"""# 模型配置
OPENAI_API_KEY={api_key}
"""
    if base_url:
        env_content += f"OPENAI_BASE_URL={base_url}\n"
    else:
        env_content += "# OPENAI_BASE_URL=\n"
    if model_name:
        env_content += f"OPENAI_MODEL_NAME={model_name}\n"
    else:
        env_content += "# OPENAI_MODEL_NAME=\n"
    
    env_content += f"""
# 金山云配置
"""
    if ks_ak:
        env_content += f"KSYUN_ACCESS_KEY={ks_ak}\n"
    else:
        env_content += "# KSYUN_ACCESS_KEY=\n"
    if ks_sk:
        env_content += f"KSYUN_SECRET_KEY={ks_sk}\n"
    else:
        env_content += "# KSYUN_SECRET_KEY=\n"
    env_content += f"KSYUN_REGION={ks_region}\n"
    if ks_account:
        env_content += f"KSYUN_ACCOUNT_ID={ks_account}\n"
    else:
        env_content += "# KSYUN_ACCOUNT_ID=\n"
    
    return env_content


def _generate_requirements_from_imports(directory: Path, framework: str) -> str:
    """
    扫描目录中的 Python 文件，从 import 语句生成 requirements.txt
    """
    import re
    
    # 常用包名到 PyPI 包名的映射
    import_to_package = {
        'langchain': 'langchain',
        'langchain_openai': 'langchain-openai',
        'langchain_anthropic': 'langchain-anthropic',
        'langchain_core': 'langchain-core',
        'langchain_community': 'langchain-community',
        'langgraph': 'langgraph',
        'deepagents': 'deepagents',
        'openai': 'openai',
        'anthropic': 'anthropic',
        'dotenv': 'python-dotenv',
        'pydantic': 'pydantic',
        'httpx': 'httpx',
        'requests': 'requests',
        'google.adk': 'google-adk',
        'google.genai': 'google-genai',
        'tiktoken': 'tiktoken',
        'faiss': 'faiss-cpu',
        'chromadb': 'chromadb',
        'tavily': 'tavily-python',
    }
    
    found_packages = set()
    
    # 扫描所有 .py 文件
    for py_file in directory.rglob('*.py'):
        try:
            content = py_file.read_text(encoding='utf-8')
        except Exception:
            continue
        
        # 匹配 import 语句
        # from xxx import yyy
        # import xxx
        import_patterns = [
            r'^from\s+([\w\.]+)',
            r'^import\s+([\w\.]+)',
        ]
        
        for pattern in import_patterns:
            for match in re.finditer(pattern, content, re.MULTILINE):
                module = match.group(1).split('.')[0]  # 取顶级模块
                full_module = match.group(1)
                
                # 检查是否在映射中
                if full_module in import_to_package:
                    found_packages.add(import_to_package[full_module])
                elif module in import_to_package:
                    found_packages.add(import_to_package[module])
                # 检查常见的下划线包名
                elif module.replace('_', '-') in ['langchain-openai', 'langchain-anthropic', 
                                                   'langchain-core', 'langchain-community']:
                    found_packages.add(module.replace('_', '-'))
    
    # 确保框架相关的核心依赖在列表中
    if framework == 'langgraph':
        found_packages.add('langgraph')
        found_packages.add('langchain')
        found_packages.add('langchain-openai')
    elif framework == 'deepagents':
        found_packages.add('deepagents')
        found_packages.add('langgraph')
        found_packages.add('langchain')
        found_packages.add('langchain-openai')
    elif framework == 'langchain':
        found_packages.add('langchain')
        found_packages.add('langchain-openai')
    elif framework == 'adk':
        found_packages.add('google-adk')
    
    # 总是添加 python-dotenv
    found_packages.add('python-dotenv')
    
    # 按字母顺序排序
    sorted_packages = sorted(found_packages)
    return '\n'.join(sorted_packages) + '\n'


def _wrap_agent_file(from_agent_path: Path, project_name: str, framework: str, agent_var: str):
    """包装单个 Agent 文件到新项目"""
    import re
    
    project_path = Path(project_name)
    package_name = project_path.name.replace('-', '_')
    
    if project_path.exists():
        print_error(f"目录 '{project_name}' 已存在")
        raise SystemExit(1)
    
    print_title("包装 Agent 文件")
    print_kv("创建项目", project_name)
    print_kv("框架", framework)
    print_kv("包装文件", str(from_agent_path))
    print_kv("Agent 变量", agent_var)
    
    # 创建目录
    (project_path / package_name).mkdir(parents=True)
    
    # 复制源文件并修复 .env 路径
    source_filename = from_agent_path.name
    dest_path = project_path / package_name / source_filename
    
    # 读取源文件内容
    source_content = from_agent_path.read_text(encoding='utf-8')
    
    # 修复 load_dotenv 路径：parent -> parent.parent (因为文件被移到了子目录)
    fixed_content = re.sub(
        r'(load_dotenv\s*\(\s*Path\s*\(\s*__file__\s*\)\s*\.parent)(\s*/\s*["\']\.env["\'])',
        r'\1.parent\2',
        source_content
    )
    
    if fixed_content != source_content:
        print_info("已自动修复 .env 加载路径")
    
    # 写入目标文件
    dest_path.write_text(fixed_content, encoding='utf-8')
    
    # 检测全局配置
    from ksadk.configs.global_config import (
        global_config_exists,
        get_env_from_global_config,
    )
    
    global_env = {}
    if global_config_exists():
        global_env = get_env_from_global_config()
        if global_env:
            print_info("检测到全局配置，已自动填充凭证")
    
    # 生成 .env
    (project_path / ".env").write_text(_generate_env_content(global_env), encoding="utf-8-sig")
    
    # 生成 agentengine.yaml
    entry_module = source_filename.replace('.py', '')
    (project_path / "agentengine.yaml").write_text(f"""# AgentEngine 项目配置 (Wrapped)
name: {package_name}
version: "1.0.0"

framework: {framework}
entry_point: {package_name}/{source_filename}
agent_variable: {agent_var}
""", encoding="utf-8-sig")
    
    # 生成 __init__.py
    (project_path / package_name / "__init__.py").write_text(f'''"""
{project_name} - Wrapped Agent
"""
from .{entry_module} import {agent_var} as root_agent
__all__ = ["root_agent"]
''', encoding="utf-8-sig")
    
    # 生成 requirements.txt
    reqs = ""
    if framework == "langchain":
        reqs = "langchain\nlangchain-openai\npython-dotenv\n"
    elif framework == "langgraph":
        reqs = "langchain\nlangchain-openai\nlanggraph\npython-dotenv\n"
    elif framework == "deepagents":
        reqs = "deepagents\nlangchain\nlangchain-openai\nlanggraph\npython-dotenv\n"
    elif framework == "adk":
        reqs = "google-adk\npython-dotenv\n"
    (project_path / "requirements.txt").write_text(reqs, encoding="utf-8")
    
    # 生成 README.md
    (project_path / "README.md").write_text(f"""# {project_name}

Wrapped Agent project (from `{from_agent_path.name}`).

## Quick Start

```bash
cd {project_name}
agentengine run -i .
```
""", encoding="utf-8-sig")
    
    print_success("包装完成")
    print_rule("快速开始")
    print_info(f"cd {project_name} && agentengine run -i .")


def _wrap_agent_directory(from_agent_dir: Path, project_name: str, framework: str, 
                          entry_file: Path, agent_var: str):
    """包装 Agent 目录到新项目"""
    import shutil
    
    project_path = Path(project_name)
    package_name = project_path.name.replace('-', '_')
    source_dir_name = from_agent_dir.name
    
    if project_path.exists():
        print_error(f"目录 '{project_name}' 已存在")
        raise SystemExit(1)
    
    print_title("包装 Agent 目录")
    print_kv("创建项目", project_name)
    print_kv("框架", framework)
    print_kv("包装目录", str(from_agent_dir))
    print_kv("入口文件", entry_file.name)
    print_kv("Agent 变量", agent_var)
    
    # 创建项目目录
    project_path.mkdir(parents=True)
    
    # 复制整个源目录到项目中（作为 package）
    dest_package_path = project_path / package_name
    ignore_names = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        ".idea",
        ".vscode",
        ".DS_Store",
    }

    def _ignore_copytree(_dir: str, names: list[str]):
        ignored = set()
        for name in names:
            if name in ignore_names or name.startswith(".venv"):
                ignored.add(name)
                continue
            if name.endswith((".pyc", ".pyo")):
                ignored.add(name)
        return ignored

    shutil.copytree(from_agent_dir, dest_package_path, ignore=_ignore_copytree)
    
    print_info(f"已复制 {sum(1 for _ in dest_package_path.rglob('*.py'))} 个 Python 文件")
    
    # 递归修复 .env 路径
    fixed_count = _fix_dotenv_paths_recursive(dest_package_path, depth=2)
    if fixed_count > 0:
        print_info(f"已自动修复 {fixed_count} 个文件的 .env 加载路径")
    
    # 修复嵌套目录导入路径
    # 查找源目录中的子目录（作为 Python 包）
    subdirs = [d.name for d in from_agent_dir.iterdir() 
               if d.is_dir() and not d.name.startswith('.') and not d.name.startswith('_')
               and (d / '__init__.py').exists()]
    
    if subdirs:
        # 修复入口文件中的导入
        dest_entry_file = dest_package_path / entry_file.relative_to(from_agent_dir)
        if _fix_nested_imports(dest_entry_file, subdirs):
            print_info(f"已修复嵌套目录导入: {', '.join(subdirs)}")
    
    
    # 检测全局配置
    from ksadk.configs.global_config import (
        global_config_exists,
        get_env_from_global_config,
    )
    
    global_env = {}
    if global_config_exists():
        global_env = get_env_from_global_config()
        if global_env:
            print_info("检测到全局配置，已自动填充凭证")
    
    # 生成 .env
    (project_path / ".env").write_text(_generate_env_content(global_env), encoding="utf-8-sig")
    
    # 生成 agentengine.yaml
    # entry_point 相对于项目根目录
    entry_relative = entry_file.relative_to(from_agent_dir)
    (project_path / "agentengine.yaml").write_text(f"""# AgentEngine 项目配置 (Wrapped Directory)
name: {package_name}
version: "1.0.0"

framework: {framework}
entry_point: {package_name}/{entry_relative}
agent_variable: {agent_var}
""", encoding="utf-8-sig")
    
    # 确保 __init__.py 正确导出 root_agent
    init_file = dest_package_path / "__init__.py"
    entry_module = ".".join(entry_relative.with_suffix("").parts)  # e.g. src.agentengine_adapter
    expected_export_line = f"from .{entry_module} import {agent_var} as root_agent"
    
    # 检查现有 __init__.py 是否已导出 root_agent
    init_has_export = False
    if init_file.exists():
        init_content = init_file.read_text(encoding='utf-8')
        if expected_export_line in init_content:
            init_has_export = True
    
    if not init_has_export:
        # 追加或创建导出语句
        export_code = f'''
# AgentEngine 导出 (自动添加)
{expected_export_line}
__all__ = ["root_agent"]
'''
        if init_file.exists():
            # 追加到现有文件
            with open(init_file, 'a', encoding='utf-8') as f:
                f.write(export_code)
            print_info("已修复 __init__.py 导出")
        else:
            init_file.write_text(f'''"""
{project_name} - Wrapped Agent
"""
{expected_export_line}
__all__ = ["root_agent"]
''', encoding="utf-8-sig")
    
    # 处理 requirements.txt
    source_requirements = from_agent_dir / "requirements.txt"
    dest_requirements = project_path / "requirements.txt"
    
    if source_requirements.exists():
        # 如果源目录有 requirements.txt，复制并补充必要的依赖
        import shutil as shutil_req
        shutil_req.copy(source_requirements, dest_requirements)
        print_info("已复制 requirements.txt")
        
        # 检查是否缺少必要依赖，追加
        existing_reqs = dest_requirements.read_text(encoding='utf-8').lower()
        missing = []
        if framework == "langgraph" and 'langgraph' not in existing_reqs:
            missing.append("langgraph")
        if framework == "deepagents":
            if 'deepagents' not in existing_reqs:
                missing.append("deepagents")
            if 'langgraph' not in existing_reqs:
                missing.append("langgraph")
        if 'python-dotenv' not in existing_reqs and 'dotenv' not in existing_reqs:
            missing.append("python-dotenv")
        
        if missing:
            with open(dest_requirements, 'a', encoding='utf-8') as f:
                f.write("\n# Added by agentengine\n")
                for pkg in missing:
                    f.write(f"{pkg}\n")
            print_info(f"已补充依赖: {', '.join(missing)}")
    else:
        # 自动根据 import 生成 requirements.txt
        reqs = _generate_requirements_from_imports(dest_package_path, framework)
        dest_requirements.write_text(reqs, encoding="utf-8")
        print_info(f"已自动生成 requirements.txt ({reqs.count(chr(10))} 个依赖)")
    
    # 生成 README.md
    (project_path / "README.md").write_text(f"""# {project_name}

Wrapped Agent project (from `{from_agent_dir.name}/`).

## Quick Start

```bash
cd {project_name}
agentengine run -i .
```
""", encoding="utf-8-sig")
    
    # === 运行时验证 ===
    print_rule("验证 Agent 加载")
    import subprocess
    import sys
    
    verify_code = f'''
import sys
sys.path.insert(0, ".")
try:
    from {package_name} import root_agent
    print("TYPE:" + type(root_agent).__name__)
except Exception as e:
    print("ERROR:" + str(e))
    sys.exit(1)
'''
    
    result = subprocess.run(
        [sys.executable, "-c", verify_code],
        cwd=str(project_path),
        capture_output=True,
        text=True,
        timeout=30
    )
    
    if result.returncode == 0:
        output = result.stdout.strip()
        if output.startswith("TYPE:"):
            agent_type = output.split("TYPE:")[1]
            print_success(f"Agent 加载成功! 类型: {agent_type}")
        else:
            print_success("Agent 加载成功!")
    else:
        error_msg = result.stderr or result.stdout
        print_warn(f"Agent 加载警告: {error_msg[:200]}")
        print_info("提示: 请检查依赖是否已安装，或代码是否有错误")
    
    print_success("包装完成")
    print_rule("快速开始")
    print_info(f"cd {project_name} && agentengine run -i .")


def _write_hermes_project_template(project_path: Path, project_name: str, package_name: str) -> None:
    """生成 Hermes container-first 项目骨架。"""
    repo_template_root = Path(__file__).resolve().parents[2] / "deploy" / "hermes"
    if repo_template_root.exists():
        replacements = {
            "{{PROJECT_NAME}}": project_name,
            "{{PACKAGE_NAME}}": package_name,
        }
        for source in repo_template_root.rglob("*"):
            relative = source.relative_to(repo_template_root)
            if source.is_dir():
                (project_path / relative).mkdir(parents=True, exist_ok=True)
                continue
            destination_relative = relative
            if source.name.endswith(".template"):
                destination_relative = relative.with_name(source.name[:-9])
                content = source.read_text(encoding="utf-8")
                for key, value in replacements.items():
                    content = content.replace(key, value)
                destination = project_path / destination_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8-sig")
                continue
            destination = project_path / destination_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        entrypoint = project_path / "entrypoint.sh"
        if entrypoint.exists():
            entrypoint.chmod(0o755)
        return

    runtime_dir = project_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    (project_path / "agentengine.yaml").write_text(f"""# AgentEngine Hermes 项目配置
name: {package_name}
version: "1.0.0"

framework: hermes
artifact_type: Container

ui_profile: hermes
ui_path: /

deploy:
  resources:
    cpu: "2"
    memory: "4Gi"
  scaling:
    min_replicas: 1
    max_replicas: 1
    concurrency: 1000
""", encoding="utf-8-sig")

    (project_path / "Dockerfile").write_text("""FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PORT=8080 \\
    API_SERVER_HOST=127.0.0.1 \\
    API_SERVER_PORT=8642 \\
    HERMES_DASHBOARD_HOST=127.0.0.1 \\
    HERMES_DASHBOARD_PORT=9119

WORKDIR /app

RUN apt-get update \\
    && apt-get install -y --no-install-recommends bash curl tini \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \\
    "fastapi>=0.100" \\
    "uvicorn[standard]>=0.23" \\
    "httpx>=0.24" \\
    "pyyaml>=6.0" \\
    "python-dotenv>=1.0" \\
    "hermes-agent==0.9.0"

COPY runtime ./runtime
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["tini", "--"]
CMD ["/app/entrypoint.sh"]
""", encoding="utf-8")

    (project_path / "entrypoint.sh").write_text("""#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/home/agent}"
export PORT="${PORT:-8080}"
export API_SERVER_HOST="${API_SERVER_HOST:-127.0.0.1}"
export API_SERVER_PORT="${API_SERVER_PORT:-8642}"
export HERMES_DASHBOARD_HOST="${HERMES_DASHBOARD_HOST:-127.0.0.1}"
export HERMES_DASHBOARD_PORT="${HERMES_DASHBOARD_PORT:-9119}"
export API_SERVER_ENABLED="${API_SERVER_ENABLED:-true}"

mkdir -p "${HOME}/.hermes"

cat > "${HOME}/.hermes/.env" <<EOF
OPENAI_API_KEY=${OPENAI_API_KEY:-}
OPENAI_BASE_URL=${OPENAI_BASE_URL:-}
OPENAI_MODEL_NAME=${OPENAI_MODEL_NAME:-}
API_SERVER_ENABLED=${API_SERVER_ENABLED}
API_SERVER_KEY=${API_SERVER_KEY:-}
API_SERVER_HOST=${API_SERVER_HOST}
API_SERVER_PORT=${API_SERVER_PORT}
EOF

cat > "${HOME}/.hermes/config.yaml" <<EOF
model:
  provider: custom
  default: "${OPENAI_MODEL_NAME:-}"
  base_url: "${OPENAI_BASE_URL:-}"
api_server:
  enabled: true
  host: "${API_SERVER_HOST}"
  port: ${API_SERVER_PORT}
EOF

hermes gateway run --replace &
HERMES_API_PID=$!

hermes dashboard --host "${HERMES_DASHBOARD_HOST}" --port "${HERMES_DASHBOARD_PORT}" --no-open &
HERMES_DASHBOARD_PID=$!

cleanup() {
  kill "${HERMES_API_PID}" "${HERMES_DASHBOARD_PID}" 2>/dev/null || true
}
trap cleanup EXIT

exec uvicorn runtime.app:app --host 0.0.0.0 --port "${PORT}"
""", encoding="utf-8")

    (runtime_dir / "__init__.py").write_text("", encoding="utf-8")
    (runtime_dir / "app.py").write_text("""from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pty
import select
import signal
import termios
from typing import Iterable

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from starlette.websockets import WebSocketState


TERMINAL_SUBPROTOCOL = "ks-terminal.v1"
SHELL_METACHARS = set("|&;<>()$`\\\\\\n\\r")
SINGLE_READONLY = {"status", "doctor", "version", "insights"}
NESTED_READONLY = {
    "sessions": {"list": (2, 2), "show": (3, 3), "export": (3, 3)},
    "config": {"show": (2, 2), "check": (2, 2), "path": (2, 2), "env-path": (2, 2)},
    "skills": {"list": (2, 2), "audit": (2, 2), "check": (2, 2)},
    "tools": {"list": (2, 2)},
    "cron": {"list": (2, 2), "status": (2, 2)},
    "gateway": {"status": (2, 2)},
}

app = FastAPI()


def _api_base() -> str:
    return f"http://{os.getenv('API_SERVER_HOST', '127.0.0.1')}:{os.getenv('API_SERVER_PORT', '8642')}"


def _dashboard_base() -> str:
    return f"http://{os.getenv('HERMES_DASHBOARD_HOST', '127.0.0.1')}:{os.getenv('HERMES_DASHBOARD_PORT', '9119')}"


def _validate_exec_argv(argv: Iterable[str]) -> list[str]:
    normalized = [str(item).strip() for item in argv]
    if not normalized:
        raise ValueError("missing argv")
    for item in normalized:
        if not item or item.startswith("-") or any(char in SHELL_METACHARS for char in item):
            raise ValueError(f"unsafe argv: {item}")
    if normalized[0] in SINGLE_READONLY:
        if len(normalized) != 1:
            raise ValueError("unsupported argv")
        return normalized
    nested = NESTED_READONLY.get(normalized[0])
    if not nested or len(normalized) < 2:
        raise ValueError("unsupported argv")
    bounds = nested.get(normalized[1])
    if not bounds:
        raise ValueError("unsupported argv")
    if not (bounds[0] <= len(normalized) <= bounds[1]):
        raise ValueError("unsupported argv")
    return normalized


async def _proxy_http(request: Request, base_url: str, path: str) -> Response:
    target = f"{base_url}/{path.lstrip('/')}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }
    async with httpx.AsyncClient(timeout=None) as client:
        upstream = await client.request(
            request.method,
            target,
            headers=headers,
            content=await request.body(),
        )
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in {"content-encoding", "transfer-encoding", "connection"}
    }
    return Response(upstream.content, status_code=upstream.status_code, headers=response_headers)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_api(path: str, request: Request) -> Response:
    return await _proxy_http(request, _api_base(), f"v1/{path}")


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    termios.tcsetwinsize(fd, (int(rows or 24), int(cols or 80)))


async def _pty_reader(ws: WebSocket, fd: int) -> None:
    loop = asyncio.get_running_loop()
    while True:
        await loop.run_in_executor(None, lambda: select.select([fd], [], [], None))
        data = os.read(fd, 4096)
        if not data:
            return
        await ws.send_bytes(data)


async def _wait_process(pid: int) -> int:
    loop = asyncio.get_running_loop()
    _, status = await loop.run_in_executor(None, os.waitpid, pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return status


@app.websocket("/_ksadk/terminal/ws")
async def terminal_ws(ws: WebSocket) -> None:
    if TERMINAL_SUBPROTOCOL not in (ws.headers.get("sec-websocket-protocol") or ""):
        await ws.close(code=4400, reason="missing ks-terminal.v1 subprotocol")
        return
    await ws.accept(subprotocol=TERMINAL_SUBPROTOCOL)
    pid = None
    fd = None
    receive_task = None
    reader_task = None
    wait_task = None
    try:
        first = await ws.receive_text()
        payload = json.loads(first)
        if payload.get("type") != "start":
            raise ValueError("first frame must be start")
        mode = payload.get("mode")
        argv = payload.get("argv") or []
        if mode == "tui":
            command = ["hermes", "chat"]
        elif mode == "exec":
            command = ["hermes", *_validate_exec_argv(argv)]
        else:
            raise ValueError("unsupported mode")

        pid, fd = pty.fork()
        if pid == 0:
            os.execvp(command[0], command)

        _set_winsize(fd, int(payload.get("rows") or 24), int(payload.get("cols") or 80))
        await ws.send_text(json.dumps({"type": "ready"}))
        reader_task = asyncio.create_task(_pty_reader(ws, fd))
        wait_task = asyncio.create_task(_wait_process(pid))
        receive_task = asyncio.create_task(ws.receive())

        while True:
            done, _pending = await asyncio.wait({wait_task, receive_task}, return_when=asyncio.FIRST_COMPLETED)
            if wait_task in done:
                if reader_task:
                    reader_task.cancel()
                if receive_task:
                    receive_task.cancel()
                code = wait_task.result()
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(json.dumps({"type": "exit", "code": code}))
                return

            message = receive_task.result()
            receive_task = asyncio.create_task(ws.receive())
            if message.get("bytes") is not None:
                os.write(fd, message["bytes"])
                continue
            text = message.get("text")
            if not text:
                continue
            control = json.loads(text)
            if control.get("type") == "resize":
                _set_winsize(fd, int(control.get("rows") or 24), int(control.get("cols") or 80))
            elif control.get("type") == "signal":
                sig = signal.SIGINT if control.get("signal") == "SIGINT" else signal.SIGTERM
                os.kill(pid, sig)
            elif control.get("type") == "stdin_eof":
                os.close(fd)
                fd = None
    except WebSocketDisconnect:
        return
    except Exception as exc:
        if ws.client_state == WebSocketState.CONNECTED:
            await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
            await ws.close()
    finally:
        for task in (receive_task, reader_task):
            if task and not task.done():
                task.cancel()
        if pid is not None and (wait_task is None or not wait_task.done()):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        if wait_task and not wait_task.done():
            wait_task.cancel()
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_dashboard(path: str, request: Request) -> Response:
    return await _proxy_http(request, _dashboard_base(), path)
""", encoding="utf-8")

    (project_path / "README.md").write_text(f"""# {project_name}

Hermes AgentEngine container-first 项目。

## 快速开始

```bash
cd {project_name}

# 1. 编辑 .env，填写模型配置
vim .env

# 2. 部署平台预置 Hermes runtime 镜像
agentengine hermes deploy

# 3. 打开 Hermes 管理 UI
agentengine hermes open

# 4. 进入 pod 内 Hermes 原生 TUI
agentengine invoke

# 5. 使用统一 hosted chat
agentengine hermes open --chat

# 6. 受限只读运维子命令
agentengine hermes exec <agent> -- status

# 7. Pairing 审批透传
agentengine hermes pairing <agent> -- list
```

## Runtime Contract

- `/` 反代到 Hermes dashboard 管理 UI
- `/chat` 由 AgentEngine hosted UI/router 处理
- `/v1/*` 反代到 Hermes OpenAI-compatible API server
- `/_ksadk/terminal/ws` 提供 `ks-terminal.v1` 原生 TUI/exec/pairing websocket
""", encoding="utf-8-sig")


@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.argument('project_name', required=False)
@click.option('--framework', '-f', type=click.Choice(['adk', 'langchain', 'langgraph', 'deepagents', 'openclaw', 'hermes']),
              default='langgraph', help='框架类型 (default: langgraph)')
@click.option('--from-agent', 'from_agent_path', type=click.Path(exists=True), 
              help='包装现有 Agent 文件或目录')
def create(project_name: str, framework: str, from_agent_path: str):
    """创建新的 Agent 项目
    
    PROJECT_NAME: 项目名称
    
    使用 --from-agent 可包装现有代码:
        agentengine init --from-agent ./my_agent.py      # 单文件
        agentengine init --from-agent ./my_agent/        # 目录
    """
    # === 包装模式 ===
    if from_agent_path:
        from_path = Path(from_agent_path)
        
        # === 目录模式 ===
        if from_path.is_dir():
            print_info(f"扫描目录: {from_path}")
            
            # 查找入口文件
            entry_result = _find_entry_file(from_path)
            if not entry_result:
                print_error("未找到有效的入口文件 (agent.py, main.py, __init__.py 或包含 Agent 定义的文件)")
                raise SystemExit(1)
            
            entry_file, detected_var = entry_result
            print_info(f"检测到入口: {entry_file.name}")
            print_info(f"检测到变量: {detected_var}")
            
            # 检测框架：优先读取已有 agentengine.yaml
            detected_framework = _load_framework_from_agentengine_yaml(from_path) or 'unknown'
            if detected_framework != 'unknown':
                print_info(f"从 agentengine.yaml 检测到框架: {detected_framework}")
            else:
                entry_content = entry_file.read_text(encoding='utf-8')
                detected_framework = _detect_framework(entry_content)
            
            # 如果入口文件未检测到框架，扫描整个目录
            if detected_framework == 'unknown':
                for py_file in _iter_python_files(from_path):
                    try:
                        content = py_file.read_text(encoding='utf-8')
                    except Exception:
                        continue
                    detected_framework = _detect_framework(content)
                    if detected_framework != 'unknown':
                        break
            
            if detected_framework == 'unknown':
                print_warn("无法自动检测框架，将使用默认 langgraph")
                detected_framework = 'langgraph'
            else:
                print_info(f"检测到框架: {detected_framework}")
            
            # 如果没有指定项目名，使用目录名
            if not project_name:
                project_name = from_path.name.replace('_', '-')
            
            _wrap_agent_directory(from_path, project_name, detected_framework, entry_file, detected_var)
            return
        
        # === 单文件模式 ===
        else:
            content = from_path.read_text(encoding='utf-8')
            
            # 自动检测框架
            detected_framework = _detect_framework(content)
            if detected_framework == 'unknown':
                print_warn("无法自动检测框架，将使用默认 langgraph")
                detected_framework = 'langgraph'
            else:
                print_info(f"检测到框架: {detected_framework}")
            
            # 自动检测 Agent 变量
            detected_var = _detect_agent_variable(content)
            if not detected_var:
                print_warn("无法自动检测 Agent 变量，将使用默认 root_agent")
                detected_var = 'root_agent'
            else:
                print_info(f"检测到变量: {detected_var}")
            
            # 如果没有指定项目名，使用文件名
            if not project_name:
                project_name = from_path.stem.replace('_', '-')
            
            _wrap_agent_file(from_path, project_name, detected_framework, detected_var)
            return
    
    # === 正常模板模式 ===
    # 如果没有提供项目名称，进入交互模式
    if not project_name:
        print_title("初始化新项目")
        
        project_name = questionary.text(
            "请输入项目名称:",
            style=custom_style
        ).ask()
        
        if not project_name:
            print_error("取消创建")
            raise SystemExit(0)
            
        framework = questionary.select(
            "请选择开发框架:",
            choices=['langgraph', 'langchain', 'deepagents', 'adk', 'openclaw', 'hermes'],
            default='langgraph',
            style=custom_style
        ).ask()
        
        if not framework:
            print_error("取消创建")
            raise SystemExit(0)
            
    project_path = Path(project_name)
    
    if project_path.exists():
        print_error(f"目录 '{project_name}' 已存在")
        raise SystemExit(1)
    
    print_kv("创建项目", project_name)
    print_kv("框架", framework)
    
    package_name = project_path.name.replace('-', '_')
    project_path.mkdir(parents=True)
    if framework not in {"openclaw", "hermes"}:
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
            print_info("检测到全局配置，已自动填充凭证")
    
    # .env - 生成配置文件
    # 如果有全局配置，使用全局配置的值；否则使用占位符
    # 如果有全局配置，使用全局配置的值；否则使用空字符串
    api_key = global_env.get("OPENAI_API_KEY", "")
    base_url = global_env.get("OPENAI_BASE_URL", "")
    model_name = global_env.get("OPENAI_MODEL_NAME", "")
    
    ks_ak = global_env.get("KSYUN_ACCESS_KEY", "")
    ks_sk = global_env.get("KSYUN_SECRET_KEY", "")
    ks_region = global_env.get("KSYUN_REGION", "cn-beijing-6")
    ks_account = global_env.get("KSYUN_ACCOUNT_ID", "")
    
    # 构建 .env 内容
    if framework == "openclaw":
        env_content = f"""# ======================
# OpenClaw 标准部署最小配置
# ======================
KSYUN_ACCESS_KEY={ks_ak}
KSYUN_SECRET_KEY={ks_sk}
KSYUN_REGION={ks_region}
"""
        if ks_account:
            env_content += f"KSYUN_ACCOUNT_ID={ks_account}\n"
        else:
            env_content += "# KSYUN_ACCOUNT_ID=your-account-id\n"

        env_content += f"\nOPENAI_API_KEY={api_key}\n"
        if base_url:
            env_content += f"OPENAI_BASE_URL={base_url}\n"
        else:
            env_content += "# OPENAI_BASE_URL=http://kspmas.ksyun.com/v1\n"

        if model_name:
            env_content += f"OPENAI_MODEL_NAME={model_name}\n"
        else:
            env_content += "# OPENAI_MODEL_NAME=glm-5.1\n"
    elif framework == "hermes":
        env_content = f"""# ======================
# Hermes 标准部署最小配置
# ======================
KSYUN_ACCESS_KEY={ks_ak}
KSYUN_SECRET_KEY={ks_sk}
KSYUN_REGION={ks_region}
"""
        if ks_account:
            env_content += f"KSYUN_ACCOUNT_ID={ks_account}\n"
        else:
            env_content += "# KSYUN_ACCOUNT_ID=your-account-id\n"

        env_content += f"""
OPENAI_API_KEY={api_key}
"""
        if base_url:
            env_content += f"OPENAI_BASE_URL={base_url}\n"
        else:
            env_content += "# OPENAI_BASE_URL=http://kspmas.ksyun.com/v1\n"

        if model_name:
            env_content += f"OPENAI_MODEL_NAME={model_name}\n"
        else:
            env_content += "# OPENAI_MODEL_NAME=glm-5.1\n"

        env_content += """
# Hermes runtime
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
HERMES_DASHBOARD_HOST=127.0.0.1
HERMES_DASHBOARD_PORT=9119
PORT=8080
# HERMES_CONTEXT_LENGTH=200000
# HERMES_FALLBACK_MODEL=kimi-k2.5
# HERMES_IMAGE=hub.kce.ksyun.com/agentengine-public/hermes-agent:2026.4.16
"""
    else:
        langfuse_public = global_env.get("LANGFUSE_PUBLIC_KEY", "")
        langfuse_secret = global_env.get("LANGFUSE_SECRET_KEY", "")
        langfuse_url = global_env.get("LANGFUSE_BASE_URL", "")

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
            env_content += "# OPENAI_MODEL_NAME=glm-5.1\n"

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

    if framework == "openclaw":
        print_success("项目创建成功")
        print_rule("快速开始")
        print_info("快速开始 (复制并执行):")
        print_info(f"cd {project_name} && agentengine openclaw deploy")
        print_info("部署前如需覆盖模型/网关参数，可先编辑 .env")
        return
    if framework == "hermes":
        _write_hermes_project_template(project_path, project_name, package_name)
        print_success("项目创建成功")
        print_rule("快速开始")
        print_info("快速开始 (复制并执行):")
        print_info(f"cd {project_name} && agentengine hermes deploy")
        print_info("部署前如需覆盖模型/运行时参数，可先编辑 .env")
        return
    
    # agentengine.yaml - Agent 配置
    (project_path / "agentengine.yaml").write_text(f"""# AgentEngine 项目配置
name: {package_name}
version: "1.0.0"

# 框架类型: adk, langchain, langgraph, deepagents
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
    if framework == "openclaw":
        readme = f"""# {project_name}

基于 AgentEngine 创建的 OpenClaw 项目。

## 快速开始

```bash
cd {project_name}

# 1. 编辑 .env（可选，覆盖模型/网关参数）
vim .env

# 2. 部署 OpenClaw（默认 trusted-proxy）
agentengine openclaw deploy

# 3. 打开 Dashboard
agentengine dashboard --share
```
"""
    else:
        readme = f"""# {project_name}

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
"""

    (project_path / "README.md").write_text(readme, encoding="utf-8-sig")

    # requirements.txt
    reqs = "requests_aws4auth\n"  # Minimum required for ksadk.common.auth
    if framework == "langchain":
        reqs += "langchain\nlangchain-openai\npython-dotenv\n"
    elif framework == "langgraph":
        reqs += "langchain\nlangchain-openai\nlanggraph\npython-dotenv\n"
    elif framework == "deepagents":
        reqs += "deepagents\nlangchain\nlangchain-openai\nlanggraph\npython-dotenv\n"
    elif framework == "adk":
        reqs += "google-adk\npython-dotenv\n"
    
    (project_path / "requirements.txt").write_text(reqs, encoding="utf-8")
    
    print_success("项目创建成功")
    print_rule("快速开始")
    
    # 检测操作系统，提供对应的组合命令
    import platform
    is_windows = platform.system() == "Windows"
    
    if framework == "openclaw":
        combined_cmd = f"cd {project_name} && agentengine openclaw deploy"
        run_cmd = f"cd {project_name} && agentengine dashboard --share"
    elif is_windows:
        # Windows: 使用 && 连接命令
        combined_cmd = f"cd {project_name} && agentengine config"
        run_cmd = f"cd {project_name} && agentengine run -i ."
    else:
        # Unix (macOS/Linux): 使用 && 连接命令
        combined_cmd = f"cd {project_name} && agentengine config"
        run_cmd = f"cd {project_name} && agentengine run -i ."
    
    print_info("快速开始 (复制并执行):")
    print_info(combined_cmd)
    print_info("或直接运行 (环境变量中需包含模型 API Key):")
    print_info(run_cmd)
