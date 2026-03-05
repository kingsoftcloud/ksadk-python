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
from ksadk.cli.ui import (
    print_error,
    print_info,
    print_kv,
    print_rule,
    print_success,
    print_title,
    print_warn,
)

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
        # 使用 utf-8-sig 自动处理 BOM，确保 Windows 兼容性
        return dotenv_values(path, encoding="utf-8-sig")
    return {}


def _update_env_file(path: Path, updates: dict):
    """Update .env file preserving existing keys/comments where possible"""
    lines = []
    if path.exists():
        # 使用 utf-8-sig 自动处理 BOM，确保 Windows 兼容性
        content = path.read_text(encoding="utf-8-sig")
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

    # 使用 utf-8-sig 编码 (带 BOM)，确保 Windows 程序正确识别为 UTF-8
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8-sig")


def _handle_set_command(set_items: tuple, output_path: Path, env_path: Path, is_global: bool):
    """处理 --set 命令逻辑"""
    updates_yaml = {}
    updates_env = {}
    
    for item in set_items:
        if "=" not in item:
            print_warn(f"无效格式忽略: {item} (应为 key=value)")
            continue
            
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        
        # 键值映射逻辑
        # 1. 环境变量 (OPENAI_*, KSYUN_*)
        if key.startswith("OPENAI_") or key.startswith("KSYUN_"):
            updates_env[key] = value
            # 特殊联动: KSYUN_REGION -> region
            if key == "KSYUN_REGION":
                updates_yaml["region"] = value
                
        # 2. YAML 配置 (region 特殊处理联动)
        elif key == "region":
            updates_yaml["region"] = value
            updates_env["KSYUN_REGION"] = value
            
        # 3. 其他默认视为 YAML 配置
        else:
            updates_yaml[key] = value

    # 更新本地 .env
    if updates_env:
        _update_env_file(env_path, updates_env)
        print_success(f"更新环境变量 ({env_path}): {', '.join(updates_env.keys())}")
        
    # 更新本地 agentengine.yaml
    if updates_yaml:
        current_yaml = {}
        if output_path.exists():
            try:
                with open(output_path, 'r', encoding='utf-8-sig') as f:
                    current_yaml = yaml.safe_load(f) or {}
            except Exception:
                pass
        
        current_yaml.update(updates_yaml)
        
        # 简单回写 (注意：这会丢失原文件的注释，但为了 --set 功能这是权衡)
        # 如果只想更新特定字段而不重写文件结构，需要更复杂的解析器
        with open(output_path, 'w', encoding='utf-8-sig') as f:
            yaml.dump(current_yaml, f, default_flow_style=False, allow_unicode=True)
            
        print_success(f"更新项目配置 ({output_path}): {', '.join(updates_yaml.keys())}")

    # 处理全局配置
    if is_global:
        from ksadk.configs.global_config import (
            save_global_config,
            build_global_config_from_env,
            load_global_config,
            get_global_config_path
        )
        
        # 加载现有全局配置用于合并 (因为 build_from_env 是覆盖式构建)
        # 这里简化逻辑：我们只更新本次 set 涉及的环境变量
        # 但 build_global_config_from_env 需要完整的 env 字典才能构建出完整结构？
        # 不，它会构建一个新的结构。我们需要合并到旧结构中。
        
        # 更好策略: 加载旧全局 -> 扁平化为 Env -> 更新 Env -> 重新构建 -> 保存
        # 或者直接利用 config 模块的分组逻辑 (需要 config 模块支持 update)
        
        # 简易实现：
        # 1. 获取当前全局配置的 env 视图
        from ksadk.configs.global_config import get_env_from_global_config
        
        current_global_env = get_env_from_global_config()
        # 2. 合并本次更新
        current_global_env.update(updates_env)
        # 3. 重新构建并保存
        new_global_config = build_global_config_from_env(current_global_env)
        
        if save_global_config(new_global_config):
            print_success(f"更新全局配置 ({get_global_config_path()})")
        else:
            print_warn("保存全局配置失败")

@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.option('--output', '-o', default='agentengine.yaml', help='输出配置文件名')
@click.option('--set', '-s', 'set_items', multiple=True, help='设置配置项 key=value')
@click.option('--global', 'is_global', is_flag=True, default=False, help='强制更新全局配置')
def config(output: str, set_items: tuple, is_global: bool):
    """通过交互式向导配置 agentengine.yaml 和 .env 文件
    
    支持:
    1. 配置 Agent 基础信息 (名称、框架等)
    2. 配置 模型服务 (API Key, Base URL)
    3. 配置 云厂商凭证 (KSYUN AK/SK)
    
    参数:
        --set: 非交互式设置配置项 (如 --set name=MyAgent --set KSYUN_REGION=cn-beijing-6)
        --global: 强制更新全局配置 (~/.agentengine/settings.json)
    """
    print_title("AgentEngine 配置向导")
    
    output_path = Path(output)
    env_path = Path(".env")
    
    # === 0. 处理 --set 非交互模式 ===
    if set_items:
        _handle_set_command(set_items, output_path, env_path, is_global)
        return

    # === 1. 加载现有配置 ===
    existing_config = {}
    if output_path.exists():
        try:
            # 使用 utf-8-sig 自动处理 BOM，确保 Windows 兼容性
            with open(output_path, 'r', encoding='utf-8-sig') as f:
                existing_config = yaml.safe_load(f) or {}
            print_info(f"检测到现有配置文件: {output_path}")
        except Exception:
            pass

    existing_env = _load_env_file(env_path)
    if existing_env:
        print_info(f"检测到现有环境变量: {env_path}")

    print_rule()
    
    # Helper to clean code and handle Ctrl+C
    def _ask_or_exit(question):
        result = question.ask()
        if result is None:
            print_error("取消配置")
            raise SystemExit(0)
        return result

    new_config = {}
    new_env = {}

    # === 2. 基础配置 (agentengine.yaml) ===
    print_rule("基础配置")
    
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
    
    frameworks = ['langgraph', 'langchain', 'adk', 'openclaw']
    new_config['framework'] = _ask_or_exit(questionary.select(
        "选择开发框架:",
        choices=frameworks,
        default=existing_config.get('framework', 'langgraph'),
        style=custom_style
    ))
    
    print_rule()

    # === 3. 模型配置 (.env) ===
    print_rule("模型配置")
    print_info("配置用于推理的大模型服务 (OpenAI 兼容接口)")
    
    # 向后兼容: 如果是从旧版模板生成的，可能包含 'your-api-key-here' 占位符，视为空
    default_api_key = existing_env.get('OPENAI_API_KEY', '')
    if default_api_key == "your-api-key-here":
        default_api_key = ""

    new_env['OPENAI_API_KEY'] = _ask_or_exit(questionary.password(
        "API Key (OPENAI_API_KEY):",
        default=default_api_key,
        style=custom_style
    ))
    
    new_env['OPENAI_BASE_URL'] = _ask_or_exit(questionary.text(
        "Base URL (OPENAI_BASE_URL) [选填,默认使用金山云星流平台URL]:",
        default=existing_env.get('OPENAI_BASE_URL', ''),
        style=custom_style
    ))
    
    new_env['OPENAI_MODEL_NAME'] = _ask_or_exit(questionary.text(
        "模型名称 (OPENAI_MODEL_NAME) [选填,默认使用金山云星流平台deepseek-v3.2]:",
        default=existing_env.get('OPENAI_MODEL_NAME', ''),
        style=custom_style
    ))
    
    print_rule()

    # === 4. 云厂商配置 (.env) ===
    print_rule("金山云配置 (可选)")
    print_info("用于 agentengine deploy 部署到云端环境")
    
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

    print_rule()

    # === 4.5 容器镜像仓库认证 (仅 container 模式需要) ===
    print_rule("容器镜像部署 (可选)")
    print_info("如果计划使用 container 模式 (agentengine build -m container)，需要配置镜像仓库认证")
    
    should_config_registry = _ask_or_exit(questionary.confirm(
        "是否使用 container 模式部署?",
        default=bool(existing_env.get('KCR_USERNAME')),
        style=custom_style
    ))

    if should_config_registry:
        # 密码 (必填)
        new_env['KCR_PASSWORD'] = _ask_or_exit(questionary.password(
            "KCR 临时密码:",
            default=existing_env.get('KCR_PASSWORD', ''),
            style=custom_style
        ))
        
        # 仓库地址 (选填，默认使用企业版 KCR)
        default_registry = existing_env.get('KCR_REGISTRY', '')
        auto_registry = "hub.kce.ksyun.com/agentengine"
        
        custom_registry = _ask_or_exit(questionary.text(
            f"镜像仓库地址 [选填,默认: {auto_registry}]:",
            default=default_registry,
            style=custom_style
        ))
        
        if custom_registry:
            new_env['KCR_REGISTRY'] = custom_registry
        # 不填则不写入，运行时自动根据 KSYUN_REGION 生成
        
        print_info("提示:")
        print_info("用户名自动使用 KSYUN_ACCOUNT_ID (无需配置)")
        print_info("KCR 临时密码获取: https://kcr.console.ksyun.com/ → 访问凭证")

    print_rule()
    
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
    # 使用 utf-8-sig 编码 (带 BOM)，确保 Windows 程序正确识别为 UTF-8
    with open(output_path, 'w', encoding='utf-8-sig') as f:
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
    
    print_success("配置完成")
    print_kv("配置文件", str(output_path))
    print_kv("环境凭证", str(env_path))
    
    # 5.4 处理全局配置保存逻辑
    print_rule()
    from ksadk.configs.global_config import (
        save_global_config,
        build_global_config_from_env,
        get_global_config_path,
        global_config_exists,
    )

    should_save_global = False
    
    # 情况1: 用户显式指定 --global -> 总是保存 (或确认后保存)
    if is_global:
        should_save_global = True
        
    # 情况2: 全局配置不存在 -> 首次运行，提示保存
    elif not global_config_exists():
        should_save_global = _ask_or_exit(questionary.confirm(
            "是否保存到全局配置 (后续新项目可自动复用)?",
            default=True,
            style=custom_style
        ))
        
    # 情况3: 全局配置已存在 且 未指定 --global -> 静默跳过，不打扰用户
    else:
        should_save_global = False

    if should_save_global:
        global_config = build_global_config_from_env(new_env)
        if save_global_config(global_config):
            print_success(f"已保存到全局配置: {get_global_config_path()}")
        else:
            print_warn("保存全局配置失败")
