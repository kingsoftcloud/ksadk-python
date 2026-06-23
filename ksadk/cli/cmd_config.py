import os
from pathlib import Path
import re
import click
from dotenv import dotenv_values
import questionary
from questionary import Style
import yaml

from ksadk.cli.error_utils import (
    abort_with_cli_error,
    cancelled_error,
    ensure_json_output_supported,
    usage_error,
)
from ksadk.cli.resource_common import CONTEXT_SETTINGS, build_result_envelope, build_status_envelope
from ksadk.cli.ui import (
    emit_json,
    is_color_disabled,
    is_json_output,
    is_stdout_tty,
    output_option as cli_output_option,
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


def _questionary_style():
    return None if is_color_disabled() else custom_style


def _ensure_interactive_command(command: str, *, hints: list[str]) -> None:
    if is_stdout_tty():
        return
    abort_with_cli_error(
        usage_error(
            f"`{command}` 需要交互式终端 (TTY)。",
            hints=hints,
        ),
        argv=command.split()[1:],
    )


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


def _load_yaml_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _default_project_config_path() -> Path:
    canonical = Path("agentengine.yaml")
    if canonical.exists():
        return canonical
    legacy = Path("ksadk.yaml")
    if legacy.exists():
        return legacy
    return canonical


def _stringify_value(value) -> str:
    if isinstance(value, (dict, list, tuple)):
        return yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip()
    return str(value)


_ENV_VAR_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _is_env_assignment_key(key: str) -> bool:
    return bool(_ENV_VAR_KEY_PATTERN.fullmatch(key))


def _parse_set_items(set_items: tuple) -> tuple[dict, dict, list[str]]:
    """Parse KEY=VALUE assignments into project/env updates."""
    updates_yaml = {}
    updates_env = {}
    invalid_items: list[str] = []

    for item in set_items:
        if "=" not in item:
            invalid_items.append(str(item))
            continue

        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()

        if _is_env_assignment_key(key):
            updates_env[key] = value
            if key == "KSYUN_REGION":
                updates_yaml["region"] = value
        elif key == "region":
            updates_yaml["region"] = value
            updates_env["KSYUN_REGION"] = value
        else:
            updates_yaml[key] = value
    return updates_yaml, updates_env, invalid_items


def _apply_set_command(set_items: tuple, output_path: Path, env_path: Path, is_global: bool) -> dict:
    """Apply non-interactive config mutations and return a structured summary."""
    updates_yaml, updates_env, invalid_items = _parse_set_items(set_items)
    if not updates_yaml and not updates_env and invalid_items:
        raise usage_error(
            "至少提供一个有效的 KEY=VALUE 配置项。",
            hints=["示例: `agentengine config set region=cn-beijing-6 OPENAI_MODEL_NAME=glm-5.2`"],
        )

    result = {
        "project_config_path": str(output_path),
        "project_env_path": str(env_path),
        "updated_project_keys": [],
        "updated_env_keys": [],
        "invalid_items": invalid_items,
        "global_updated": False,
        "global_config_path": None,
    }

    if updates_env:
        _update_env_file(env_path, updates_env)
        result["updated_env_keys"] = sorted(updates_env.keys())

    if updates_yaml:
        current_yaml = _load_yaml_file(output_path)
        current_yaml.update(updates_yaml)

        with open(output_path, "w", encoding="utf-8-sig") as f:
            yaml.dump(current_yaml, f, default_flow_style=False, allow_unicode=True)
        result["updated_project_keys"] = sorted(updates_yaml.keys())

    if is_global:
        from ksadk.configs.global_config import (
            build_global_config_from_env,
            get_global_config_path
        )

        from ksadk.configs.global_config import get_env_from_global_config, save_global_config

        current_global_env = get_env_from_global_config()
        current_global_env.update(updates_env)
        if "region" in updates_yaml and "KSYUN_REGION" not in current_global_env:
            current_global_env["KSYUN_REGION"] = updates_yaml["region"]
        new_global_config = build_global_config_from_env(current_global_env)
        if save_global_config(new_global_config):
            result["global_updated"] = True
            result["global_config_path"] = str(get_global_config_path())
    return result


def _collect_config_snapshot(output_path: Path, env_path: Path) -> dict:
    from ksadk.configs.global_config import get_env_from_global_config, get_global_config_path, load_global_config

    project_config = _load_yaml_file(output_path)
    project_env = {k: v for k, v in _load_env_file(env_path).items() if v not in (None, "")}
    global_config = load_global_config()
    global_env = get_env_from_global_config()
    effective_env = dict(global_env)
    effective_env.update(project_env)
    return {
        "project_config_path": str(output_path),
        "project_env_path": str(env_path),
        "global_config_path": str(get_global_config_path()),
        "project_config": project_config,
        "project_env": project_env,
        "global_config": global_config,
        "global_env": global_env,
        "effective_env": effective_env,
    }


def _render_config_snapshot(snapshot: dict) -> None:
    emit_json(build_status_envelope(resource="config", item=snapshot))


def _render_config_snapshot_pretty(snapshot: dict) -> None:
    print_title("配置概览")
    print_kv("项目配置文件", snapshot["project_config_path"])
    print_kv("环境文件", snapshot["project_env_path"])
    print_kv("全局配置文件", snapshot["global_config_path"])

    print_rule("项目配置")
    if snapshot["project_config"]:
        for key, value in snapshot["project_config"].items():
            print_kv(str(key), _stringify_value(value))
    else:
        print_info("未检测到项目配置。")

    print_rule("项目环境变量")
    if snapshot["project_env"]:
        for key, value in snapshot["project_env"].items():
            print_kv(str(key), _stringify_value(value))
    else:
        print_info("未检测到项目 .env 配置。")

    print_rule("全局配置")
    if snapshot["global_env"]:
        for key, value in snapshot["global_env"].items():
            print_kv(str(key), _stringify_value(value))
    else:
        print_info("未检测到全局配置。")

    print_rule("生效环境变量")
    if snapshot["effective_env"]:
        for key, value in snapshot["effective_env"].items():
            print_kv(str(key), _stringify_value(value))
    else:
        print_info("当前没有可用的生效环境变量。")


def _render_config_set_result(result: dict) -> None:
    emit_json(
        build_result_envelope(
            resource="config",
            action="set",
            result=result,
            hints=[],
        )
    )


def _render_config_set_result_pretty(result: dict) -> None:
    print_success("配置已更新")
    print_kv("项目配置文件", result["project_config_path"])
    print_kv("环境文件", result["project_env_path"])
    if result["updated_project_keys"]:
        print_kv("更新的项目键", ", ".join(result["updated_project_keys"]))
    if result["updated_env_keys"]:
        print_kv("更新的环境变量", ", ".join(result["updated_env_keys"]))
    if result["invalid_items"]:
        print_warn(f"已忽略无效配置项: {', '.join(result['invalid_items'])}")
    if result["global_updated"]:
        print_kv("全局配置", result["global_config_path"] or "~/.agentengine/settings.json")


def _run_config_set_command(*, set_items: tuple, output_path: Path, env_path: Path, is_global: bool) -> dict:
    if not set_items:
        raise usage_error(
            "请至少提供一个 KEY=VALUE 配置项。",
            hints=["示例: `agentengine config set region=cn-beijing-6 OPENAI_MODEL_NAME=glm-5.2`"],
        )
    return _apply_set_command(set_items, output_path, env_path, is_global)

def run_config_wizard(config_file: str | None, set_items: tuple, is_global: bool):
    """通过交互式向导配置 agentengine.yaml 和 .env 文件
    
    支持:
    1. 配置 Agent 基础信息 (名称、框架等)
    2. 配置 模型服务 (API Key, Base URL)
    3. 配置 云厂商凭证 (KSYUN AK/SK)
    
    参数:
        --set: 非交互式设置配置项 (如 --set name=MyAgent --set KSYUN_REGION=cn-beijing-6)
        --global: 强制更新全局配置 (~/.agentengine/settings.json)
    """
    output_path = Path(config_file) if config_file else _default_project_config_path()
    env_path = Path(".env")

    # === 0. 处理 --set 非交互模式 ===
    if set_items:
        print_warn("`agentengine config wizard --set ...` 是兼容入口，推荐改用 `agentengine config set KEY=VALUE ...`。")
        result = _run_config_set_command(
            set_items=set_items,
            output_path=output_path,
            env_path=env_path,
            is_global=is_global,
        )
        if is_json_output():
            _render_config_set_result(result)
        else:
            _render_config_set_result_pretty(result)
        return

    ensure_json_output_supported(
        "agentengine config wizard",
        suggestion="请改用 `agentengine config show --output json` 或 `agentengine config set KEY=VALUE`。",
    )
    _ensure_interactive_command(
        "agentengine config wizard",
        hints=[
            "查看当前配置请使用 `agentengine config show`。",
            "非交互修改请使用 `agentengine config set KEY=VALUE ...`。",
        ],
    )
    print_title("AgentEngine 配置向导")

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
            raise cancelled_error("取消配置")
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
        style=_questionary_style()
    ))
    
    new_config['description'] = _ask_or_exit(questionary.text(
        "Agent 描述:", 
        default=existing_config.get('description', ''),
        style=_questionary_style()
    ))
    
    frameworks = ['langgraph', 'langchain', 'deepagents', 'adk', 'openclaw']
    default_framework = existing_config.get('framework', 'langgraph')
    if default_framework and default_framework not in frameworks:
        frameworks.append(default_framework)
    new_config['framework'] = _ask_or_exit(questionary.select(
        "选择开发框架:",
        choices=frameworks,
        default=default_framework,
        style=_questionary_style()
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
        style=_questionary_style()
    ))
    
    new_env['OPENAI_BASE_URL'] = _ask_or_exit(questionary.text(
        "Base URL (OPENAI_BASE_URL) [选填,默认使用金山云星流平台URL]:",
        default=existing_env.get('OPENAI_BASE_URL', ''),
        style=_questionary_style()
    ))
    
    new_env['OPENAI_MODEL_NAME'] = _ask_or_exit(questionary.text(
        "模型名称 (OPENAI_MODEL_NAME) [选填,默认使用金山云星流平台glm-5.2]:",
        default=existing_env.get('OPENAI_MODEL_NAME', ''),
        style=_questionary_style()
    ))
    
    print_rule()

    # === 4. 云厂商配置 (.env) ===
    print_rule("金山云配置 (可选)")
    print_info("用于 agentengine deploy 部署到云端环境")
    
    should_config_ksyun = _ask_or_exit(questionary.confirm(
        "是否配置金山云凭证?",
        default=bool(existing_env.get('KSYUN_ACCESS_KEY')),
        style=_questionary_style()
    ))

    if should_config_ksyun:
        new_env['KSYUN_ACCESS_KEY'] = _ask_or_exit(questionary.password(
            "Access Key (AK):",
            default=existing_env.get('KSYUN_ACCESS_KEY', ''),
            style=_questionary_style()
        ))
        
        new_env['KSYUN_SECRET_KEY'] = _ask_or_exit(questionary.password(
            "Secret Key (SK):",
            default=existing_env.get('KSYUN_SECRET_KEY', ''),
            style=_questionary_style()
        ))
        
        new_env['KSYUN_ACCOUNT_ID'] = _ask_or_exit(questionary.text(
            "Account ID (账户ID):",
            default=existing_env.get('KSYUN_ACCOUNT_ID', ''),
            style=_questionary_style()
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
            style=_questionary_style()
        ))
        
        # 如果选择了自定义，或者是点击了之前保留的自定义值，这里逻辑是这样的：
        # 1. 如果选了标准值 -> 直接用
        # 2. 如果选了 CUSTOM_OPTION -> 弹框让用户输
        # 3. 如果选了列表里已有的自定义值 (如 pre-online) -> 直接用
        
        if selected_region == CUSTOM_OPTION:
             selected_region = _ask_or_exit(questionary.text(
                "请输入区域 Code (如 cn-shanghai-2):",
                default=default_region if default_region not in standard_regions else "",
                style=_questionary_style()
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
        style=_questionary_style()
    ))

    if should_config_registry:
        new_env['KCR_USERNAME'] = _ask_or_exit(questionary.text(
            "KCR 用户名 (企业版请填写访问凭证用户名):",
            default=existing_env.get('KCR_USERNAME', ''),
            style=_questionary_style()
        ))

        new_env['KCR_PASSWORD'] = _ask_or_exit(questionary.password(
            "KCR 密码或 Token:",
            default=existing_env.get('KCR_PASSWORD', ''),
            style=_questionary_style()
        ))

        default_registry = existing_env.get('KCR_REGISTRY', '')
        custom_registry = _ask_or_exit(questionary.text(
            "镜像仓库地址 [选填,如: agenthzzqy-vpc.ksyunkcr.com/testagent-pub]:",
            default=default_registry,
            style=_questionary_style()
        ))
        
        if custom_registry:
            new_env['KCR_REGISTRY'] = custom_registry

        print_info("提示:")
        print_info("个人版 KCR 可留空 KCR_USERNAME，运行时使用 KSYUN_ACCOUNT_ID 作为用户名兜底")
        print_info("企业版 KCR 和第三方镜像仓库必须配置 KCR_USERNAME + KCR_PASSWORD")
        print_info("KCR 访问凭证获取: https://kcr.console.ksyun.com/ → 访问凭证")

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
            style=_questionary_style()
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


@click.group("config", context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.option("--set", "-s", "set_items", multiple=True, hidden=True, help="(兼容) 设置配置项 key=value")
@click.option("--global", "is_global", is_flag=True, default=False, hidden=True, help="(兼容) 强制更新全局配置")
@cli_output_option(hidden=True)
@click.pass_context
def config(ctx: click.Context, set_items: tuple, is_global: bool, output_mode: str | None):
    """配置命令组。

    直接运行 `agentengine config` 会进入向导。
    标准子命令为 `wizard` / `show` / `set` / `model`。
    """
    _ = output_mode
    if ctx.invoked_subcommand is not None:
        return

    if set_items:
        print_warn("`agentengine config --set ...` 是兼容入口，推荐改用 `agentengine config set KEY=VALUE ...`。")
        result = _run_config_set_command(
            set_items=set_items,
            output_path=_default_project_config_path(),
            env_path=Path(".env"),
            is_global=is_global,
        )
        if is_json_output():
            _render_config_set_result(result)
        else:
            _render_config_set_result_pretty(result)
        return

    ensure_json_output_supported(
        "agentengine config",
        suggestion="请改用 `agentengine config show --output json` 或 `agentengine config set KEY=VALUE`。",
    )
    run_config_wizard(config_file=None, set_items=(), is_global=is_global)


@config.command("wizard", context_settings=CONTEXT_SETTINGS)
@click.option("--file", "config_file", default=None, help="配置文件路径（默认自动复用 agentengine.yaml/ksadk.yaml）")
@click.option('--set', '-s', 'set_items', multiple=True, help='设置配置项 key=value')
@click.option('--global', 'is_global', is_flag=True, default=False, help='强制更新全局配置')
@cli_output_option(hidden=True)
def config_wizard(config_file: str | None, set_items: tuple, is_global: bool, output_mode: str | None):
    """通过交互式向导配置项目。"""
    _ = output_mode
    run_config_wizard(config_file=config_file, set_items=set_items, is_global=is_global)


@config.command("show", context_settings=CONTEXT_SETTINGS)
@cli_output_option()
def config_show(output_mode: str | None):
    """查看项目配置、全局配置与当前生效环境变量。"""
    _ = output_mode
    snapshot = _collect_config_snapshot(_default_project_config_path(), Path(".env"))
    if is_json_output():
        _render_config_snapshot(snapshot)
    else:
        _render_config_snapshot_pretty(snapshot)


@config.command("set", context_settings=CONTEXT_SETTINGS)
@click.argument("set_items", nargs=-1)
@click.option("--global", "is_global", is_flag=True, default=False, help="同时更新全局配置")
@cli_output_option()
def config_set(set_items: tuple, is_global: bool, output_mode: str | None):
    """非交互式设置配置项。

    \b
    示例:
      agentengine config set region=cn-beijing-6
      agentengine config set OPENAI_MODEL_NAME=glm-5.2 OPENAI_BASE_URL=https://example.com/v1
      agentengine config set KSYUN_REGION=cn-beijing-6 --global
    """
    _ = output_mode
    result = _run_config_set_command(
        set_items=set_items,
        output_path=_default_project_config_path(),
        env_path=Path(".env"),
        is_global=is_global,
    )
    if is_json_output():
        _render_config_set_result(result)
    else:
        _render_config_set_result_pretty(result)


@config.command("model", context_settings=CONTEXT_SETTINGS)
@click.option("--multi", is_flag=True, help="交互式多选模型，并按当前框架写入模型 allowlist")
@click.option(
    "--env",
    "env_models",
    default=None,
    help="按模型列表生成环境变量，逗号分隔；首个模型作为默认模型，不写入 .env",
)
@click.option(
    "--framework",
    type=click.Choice(["auto", "openclaw", "hermes", "generic"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="allowlist 变量选择策略；auto 会读取当前目录框架",
)
def config_model(multi: bool, env_models: str | None, framework: str):
    """切换默认模型。"""
    if not env_models:
        ensure_json_output_supported(
            "agentengine config model",
            suggestion="请改用 `agentengine config show --output json` 查看当前配置。",
        )
    from ksadk.cli.cmd_model import run_model_command

    run_model_command(multi=multi, env_models=env_models, framework=framework)
