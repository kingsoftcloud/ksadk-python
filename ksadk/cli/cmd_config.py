"""
agentengine config - 交互式配置向导
"""

import click
import os
import yaml
from pathlib import Path
from dotenv import dotenv_values
import questionary
from questionary import Style

# 自定义样式，确保选中项高亮可见
custom_style = Style([
    ('qmark', 'fg:#5f819d bold'),      # 略深一点的蓝青色
    ('question', 'bold'),               
    ('answer', 'fg:#69f0ae bold'),      # 浅绿色 (替代深红)
    ('pointer', 'fg:#fbc02d bold'),     # 略深的金色/暗黄色
    ('highlighted', 'fg:#fbc02d bold'), # 同上
    ('selected', 'fg:#69f0ae'),         # 浅绿色
    ('separator', 'fg:#69f0ae'),        # 浅绿色
    ('instruction', ''),                
    ('text', ''),                       
    ('disabled', 'fg:#858585 italic')   
])


def _load_env_file(path: Path) -> dict:
    """Safely load .env file if exists"""
    if path.exists():
        return dotenv_values(path)
    return {}


def _update_env_file(path: Path, updates: dict):
    """Update .env file preserving existing keys/comments where possible"""
    lines = []
    if path.exists():
        content = path.read_text()
        lines = content.splitlines()

    # Track which keys we've updated
    updated_keys = set()
    new_lines = []

    for line in lines:
        stripped = line.strip()
        # Skip comments and empty lines
        if not stripped or stripped.startswith('#'):
            new_lines.append(line)
            continue
        
        # Parse key=value
        if '=' in stripped:
            key = stripped.split('=', 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated_keys.add(key)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    # Append new keys
    added_new = False
    for key, value in updates.items():
        if key not in updated_keys and value:
            # Add a newline before new keys if the file wasn't empty and didn't end with one
            if not added_new and lines and lines[-1].strip():
                new_lines.append("") 
            new_lines.append(f"{key}={value}")
            added_new = True

    path.write_text("\n".join(new_lines) + "\n")


@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.option('--output', '-o', default='agentengine.yaml', help='输出配置文件名')
def config(output: str):
    """通过交互式向导配置 agentengine.yaml 和 .env 文件
    
    支持:
    1. 配置 Agent 基础信息 (名称、框架等)
    2. 配置 模型服务 (API Key, Base URL)
    3. 配置 云厂商凭证 (KSYUN AK/SK)
    
    命令是幂等的，再次运行会读取现有配置作为默认值。
    """
    click.secho("🔧 AgentEngine 全局配置向导", fg='blue', bold=True)
    click.echo("─" * 50)
    
    output_path = Path(output)
    env_path = Path(".env")
    
    # === 1. 加载现有配置 ===
    existing_config = {}
    if output_path.exists():
        try:
            with open(output_path, 'r') as f:
                existing_config = yaml.safe_load(f) or {}
            click.echo(f"ℹ️  检测到现有配置文件: {output_path}")
        except Exception:
            pass

    existing_env = _load_env_file(env_path)
    if existing_env:
        click.echo(f"ℹ️  检测到现有环境变量: {env_path}")

    click.echo("")
    
    # Helper to clean code and handle Ctrl+C
    def _ask_or_exit(question):
        result = question.ask()
        if result is None:
            click.echo("\n❌取消配置")
            raise SystemExit(0)
        return result

    new_config = {}
    new_env = {}

    # === 2. 基础配置 (agentengine.yaml) ===
    click.secho("📝 基础配置", fg='yellow', bold=True)
    
    # 智能默认值: 优先读文件 -> 其次用目录名 -> 最后 my-agent
    default_name = existing_config.get('name')
    if not default_name:
        default_name = Path.cwd().name
    
    new_config['name'] = _ask_or_exit(questionary.text(
        "Agent 名称:", 
        default=default_name,
        style=custom_style
    ))
    
    new_config['description'] = _ask_or_exit(questionary.text(
        "Agent 描述:", 
        default=existing_config.get('description', ''),
        style=custom_style
    ))
    
    frameworks = ['langgraph', 'langchain', 'adk']
    new_config['framework'] = _ask_or_exit(questionary.select(
        "选择开发框架:",
        choices=frameworks,
        default=existing_config.get('framework', 'langgraph'),
        style=custom_style
    ))
    
    click.echo("")

    # === 3. 模型配置 (.env) ===
    click.secho("🤖 模型配置", fg='yellow', bold=True)
    click.echo("配置用于推理的大模型服务 (OpenAI 兼容接口)")
    
    new_env['OPENAI_API_KEY'] = _ask_or_exit(questionary.password(
        "API Key (OPENAI_API_KEY):",
        default=existing_env.get('OPENAI_API_KEY', ''),
        style=custom_style
    ))
    
    new_env['OPENAI_BASE_URL'] = _ask_or_exit(questionary.text(
        "Base URL (OPENAI_BASE_URL) [选填,默认使用金山云星流平台URL]:",
        default=existing_env.get('OPENAI_BASE_URL', ''),
        style=custom_style
    ))
    
    new_env['MODEL_NAME'] = _ask_or_exit(questionary.text(
        "模型名称 (MODEL_NAME) [选填,默认使用金山云星流平台deepseek-v3.2]:",
        default=existing_env.get('MODEL_NAME', ''),
        style=custom_style
    ))
    
    click.echo("")

    # === 4. 云厂商配置 (.env) ===
    click.secho("☁️  金山云配置 (可选)", fg='yellow', bold=True)
    click.echo("用于 agentengine deploy 部署到云端环境")
    
    should_config_ksyun = _ask_or_exit(questionary.confirm(
        "是否配置金山云凭证?",
        default=bool(existing_env.get('KSYUN_ACCESS_KEY')),
        style=custom_style
    ))

    if should_config_ksyun:
        new_env['KSYUN_ACCESS_KEY'] = _ask_or_exit(questionary.password(
            "Access Key (AK):",
            default=existing_env.get('KSYUN_ACCESS_KEY', ''),
            style=custom_style
        ))
        
        new_env['KSYUN_SECRET_KEY'] = _ask_or_exit(questionary.password(
            "Secret Key (SK):",
            default=existing_env.get('KSYUN_SECRET_KEY', ''),
            style=custom_style
        ))
        
        new_env['KSYUN_ACCOUNT_ID'] = _ask_or_exit(questionary.text(
            "Account ID (账户ID):",
            default=existing_env.get('KSYUN_ACCOUNT_ID', ''),
            style=custom_style
        ))
        
        
        
        # 内部默认区域逻辑
        default_region = existing_env.get('KSYUN_REGION')
        if not default_region:
            default_region = existing_config.get('region', 'cn-beijing-6')
            
        # 标准区域列表
        standard_regions = ['cn-beijing-6', 'cn-guangzhou-1']
        CUSTOM_OPTION = "⚙️ Custom (手动输入，金山云自定义区域请参考文档确认是否支持)"
        
        choices = standard_regions.copy()
        
        # Idempotency: 如果现有值是自定义的（比如 pre-online），加入列表作为默认选项，防止报错且方便确认
        if default_region and default_region not in standard_regions:
            choices.append(default_region)
            
        choices.append(CUSTOM_OPTION)
            
        selected_region = _ask_or_exit(questionary.select(
            "默认区域 (Region):",
            choices=choices,
            default=default_region if default_region in choices else standard_regions[0],
            style=custom_style
        ))
        
        # 如果选择了自定义，或者是点击了之前保留的自定义值，这里逻辑是这样的：
        # 1. 如果选了标准值 -> 直接用
        # 2. 如果选了 CUSTOM_OPTION -> 弹框让用户输
        # 3. 如果选了列表里已有的自定义值 (如 pre-online) -> 直接用
        
        if selected_region == CUSTOM_OPTION:
             selected_region = _ask_or_exit(questionary.text(
                "请输入区域 Code (如 cn-shanghai-2):",
                default=default_region if default_region not in standard_regions else "",
                style=custom_style
             ))
        
        new_env['KSYUN_REGION'] = selected_region
        
        # 同步回 agentengine.yaml 的 region 字段，保持一致
        new_config['region'] = new_env['KSYUN_REGION']
    else:
        # 如果不配置，保留原值或设为默认
        new_config['region'] = existing_config.get('region', 'cn-beijing-6')

    click.echo("")
    
    # === 5. 写入文件 ===
    
    # 5.1 构造完整的 YAML (保留原有的其他配置)
    final_config = existing_config.copy()
    final_config.update(new_config)
    
    # 确保结构完整 (补全 config 命令中未询问但必须的字段，如果不存在的话)
    if 'entry_point' not in final_config:
        final_config['entry_point'] = f"{new_config['name'].replace('-', '_')}/agent.py"
    if 'agent_variable' not in final_config:
        final_config['agent_variable'] = "root_agent"
    if 'version' not in final_config:
        final_config['version'] = "1.0.0"

    # 5.2 写入 agentengine.yaml
    with open(output_path, 'w') as f:
        # 简单的字典转 YAML 可能丢失注释，但这是预期行为
        # 为了更好的体验，我们手动排版几个关键字段，其他用 dump
        
        f.write(f"# AgentEngine Project Config\n")
        f.write(f"name: {final_config['name']}\n")
        f.write(f"version: \"{final_config.get('version', '1.0.0')}\"\n\n")
        
        f.write(f"# Framework\n")
        f.write(f"framework: {final_config['framework']}\n")
        f.write(f"entry_point: {final_config['entry_point']}\n")
        f.write(f"agent_variable: {final_config['agent_variable']}\n\n")
        
        f.write(f"# Deployment\n")
        f.write(f"region: {final_config.get('region', 'cn-beijing-6')}\n")
        
        # 处理其他复杂对象如 resources, scaling 等，如果存在
        remaining = {k: v for k, v in final_config.items() if k not in [
            'name', 'version', 'framework', 'entry_point', 'agent_variable', 'region'
        ]}
        if remaining:
            f.write("\n# Advanced Settings\n")
            yaml.dump(remaining, f, default_flow_style=False)

    # 5.3 写入 .env
    _update_env_file(env_path, new_env)
    
    click.secho(f"✅ 配置完成!", fg='green')
    click.echo(f"   配置文件: {output_path}")
    click.echo(f"   环境凭证: {env_path}")
