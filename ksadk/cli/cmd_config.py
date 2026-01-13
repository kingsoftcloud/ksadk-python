"""
agentengin config - 交互式配置向导
"""

import click
from pathlib import Path
import yaml


@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.option('--output', '-o', default='agentengin.yaml', help='输出配置文件名')
def config(output: str):
    """通过交互式向导配置 agentengin.yaml 文件
    
    \b
    示例:
        agentengin config
        agentengin config -o custom.yaml
    """
    click.secho("🔧 AgentEngine 配置向导", fg='blue', bold=True)
    click.echo("─" * 50)
    click.echo("")
    
    config_data = {}
    
    # 基础信息
    config_data['name'] = click.prompt("请输入 Agent 名称", default="my-agent")
    config_data['description'] = click.prompt("请输入 Agent 描述", default="")
    
    # 框架选择
    frameworks = ['langgraph', 'langchain', 'adk']
    click.echo("\n可用框架: " + ", ".join(frameworks))
    config_data['framework'] = click.prompt(
        "请选择框架", 
        type=click.Choice(frameworks),
        default='langgraph'
    )
    
    # 入口配置
    config_data['entry_point'] = click.prompt("入口文件", default="agent.py")
    config_data['agent_variable'] = click.prompt("Agent 变量名", default="root_agent")
    
    # 资源规格
    click.echo("\n📊 资源配置")
    resource_presets = {
        '1c2g': {'cpu': '1', 'memory': '2Gi'},
        '2c4g': {'cpu': '2', 'memory': '4Gi'},
        '4c8g': {'cpu': '4', 'memory': '8Gi'},
        '8c16g': {'cpu': '8', 'memory': '16Gi'},
    }
    resource_choice = click.prompt(
        "请选择资源规格",
        type=click.Choice(list(resource_presets.keys())),
        default='2c4g'
    )
    config_data['resources'] = resource_presets[resource_choice]
    
    # 扩缩容配置
    click.echo("\n⚖️ 扩缩容配置")
    config_data['scaling'] = {
        'min_replicas': click.prompt("最小实例数", type=int, default=1),
        'max_replicas': click.prompt("最大实例数", type=int, default=10),
        'concurrency': click.prompt("单实例并发数", type=int, default=10),
    }
    
    # 网络配置
    click.echo("\n🌐 网络配置")
    access_types = ['public', 'private']
    config_data['network'] = {
        'access_type': click.prompt(
            "访问类型",
            type=click.Choice(access_types),
            default='public'
        ),
        'enable_https': click.confirm("启用 HTTPS", default=True),
    }
    
    # 区域配置
    regions = ['cn-north-1', 'cn-east-1', 'cn-south-1']
    config_data['region'] = click.prompt(
        "部署区域",
        type=click.Choice(regions),
        default='cn-north-1'
    )
    
    # 可观测性
    click.echo("\n📊 可观测性配置")
    config_data['tracing'] = {
        'enabled': click.confirm("启用链路追踪", default=True),
        'exporter': click.prompt(
            "Exporter 类型",
            type=click.Choice(['langfuse', 'otlp', 'inmemory']),
            default='langfuse'
        ) if click.confirm("启用链路追踪", default=True) else 'inmemory'
    }
    
    # 写入配置文件
    output_path = Path(output)
    
    # 格式化输出
    config_content = f"""# AgentEngine 配置文件
# 生成时间: {click.get_current_context().info_name}

version: "1.0"

# 基础信息
name: {config_data['name']}
description: "{config_data['description']}"
framework: {config_data['framework']}

# 入口配置
entry_point: {config_data['entry_point']}
agent_variable: {config_data['agent_variable']}

# 资源规格
resources:
  cpu: "{config_data['resources']['cpu']}"
  memory: "{config_data['resources']['memory']}"

# 扩缩容
scaling:
  min_replicas: {config_data['scaling']['min_replicas']}
  max_replicas: {config_data['scaling']['max_replicas']}
  concurrency: {config_data['scaling']['concurrency']}

# 网络配置
network:
  access_type: {config_data['network']['access_type']}
  enable_https: {str(config_data['network']['enable_https']).lower()}

# 区域
region: {config_data['region']}

# 可观测性
tracing:
  enabled: {str(config_data['tracing']['enabled']).lower()}
  exporter: {config_data['tracing']['exporter']}
"""
    
    with open(output_path, 'w') as f:
        f.write(config_content)
    
    click.echo("")
    click.secho(f"✅ 配置文件已生成: {output_path}", fg='green')
    click.echo("\n下一步:")
    click.echo(f"  agentengin build .     # 构建镜像")
    click.echo(f"  agentengin deploy .    # 部署到云端")
    click.echo(f"  agentengin launch .    # 一键构建+部署")
