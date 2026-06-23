"""
AgentEngine CLI - 命令行工具入口

使用方式:
    agentengine init myapp     # 初始化项目
    agentengine run            # 本地运行
    agentengine web            # 本地调试 UI (Invoke)
    agentengine build          # 构建镜像
    agentengine deploy         # 部署到云端
    agentengine launch         # 一键构建+部署
    agentengine agent list     # 列出已部署 Agent
    agentengine agent status   # 查看单个 Agent 状态
    agentengine agent invoke   # 与 Agent 交互
    agentengine agent delete   # 删除实例

别名: ksadk (向后兼容)
"""

import os
import sys

import click
from rich.cells import cell_len

from ksadk.cli.dry_run import dry_run_option
from ksadk.cli.error_utils import (
    CLIError,
    cli_error_from_exception,
    emit_cli_error,
    is_debug_mode_enabled,
)
from ksadk.cli.global_options import ensure_global_cli_options
from ksadk.cli.resource_common import CONTEXT_SETTINGS
from ksadk.cli.ui import no_color_option, output_option, should_render_banner
from ksadk.version import VERSION


def _gradient_line(text: str, colors: list) -> str:
    """为文本添加渐变颜色效果"""
    if not text.strip():
        return text + "\n"
    result = ""
    visible_chars = [c for c in text if c.strip()]
    if not visible_chars:
        return text + "\n"
    step = max(1, len(visible_chars) // len(colors))
    color_idx = 0
    char_count = 0
    for c in text:
        if c.strip():
            result += click.style(c, fg=colors[min(color_idx, len(colors) - 1)], bold=True)
            char_count += 1
            if char_count >= step and color_idx < len(colors) - 1:
                color_idx += 1
                char_count = 0
        else:
            result += c
    return result + "\n"


# 金山云品牌渐变色: 红 -> 橙 -> 黄
BRAND_COLORS = [(255, 87, 51), (255, 140, 0), (255, 193, 7), (255, 215, 0)]
ROOT_HELP_COMMANDS = {
    "a2a",
    "agent",
    "build",
    "completion",
    "dashboard",
    "deploy",
    "files",
    "init",
    "hermes",
    "launch",
    "mcp",
    "openclaw",
    "run",
    "version",
    "web",
}
ROOT_HELP_TOOL_COMMANDS = {
    "config",
}
SHORT_HELP_MAP = {
    "a2a": "暴露 A2A 服务与 Agent Card",
    "agent": "Agent 资源管理",
    "build": "构建部署制品",
    "completion": "Shell 补全管理",
    "dashboard": "打开云端 Agent Dashboard",
    "deploy": "部署到云端",
    "files": "管理 workspace 文件",
    "hermes": "Hermes Agent 资源管理",
    "init": "创建新项目",
    "launch": "一键构建+部署",
    "mcp": "MCP 资源管理",
    "openclaw": "OpenClaw 资源管理",
    "run": "运行 Agent",
    "version": "Agent 版本管理",
    "web": "本地调试 Agent Invoke UI",
    "config": "项目配置与模型设置",
}


def _terminal_cell_width(text: str) -> int:
    return cell_len(click.unstyle(text))


def _pad_to_terminal_cells(text: str, width: int) -> str:
    return text + " " * max(width - _terminal_cell_width(text), 0)


COLORED_HELP_LABEL_WIDTH = 28


def _write_colored_help_row(
    formatter: click.HelpFormatter,
    label: str,
    description: str,
    *,
    end: str = "\n\n",
) -> None:
    label_cell = _pad_to_terminal_cells(label, COLORED_HELP_LABEL_WIDTH)
    formatter.write(click.style(f"      {label_cell}", fg="cyan"))
    formatter.write(click.style(f"{description}{end}", fg="white"))


# ASCII 艺术字 Banner
BANNER = r"""
    _                    _   _____            _
   / \   __ _  ___ _ __ | |_| ____|_ __   __ _(_)_ __   ___
  / _ \ / _` |/ _ \ '_ \| __|  _| | '_ \ / _` | | '_ \ / _ \
 / ___ \ (_| |  __/ | | | |_| |___| | | | (_| | | | | |  __/
/_/   \_\__, |\___|_| |_|\__|_____|_| |_|\__, |_|_| |_|\___|
        |___/                            |___/
"""


class ColoredHelpGroup(click.Group):
    """带颜色帮助文本的命令组"""

    def format_help(self, ctx, formatter):
        """覆盖默认帮助格式，添加颜色"""
        if not should_render_banner():
            return self.format_help_plain(ctx, formatter)

        # 渐变 Banner
        formatter.write("\n")
        for line in BANNER.strip().split("\n"):
            formatter.write(_gradient_line("  " + line, BRAND_COLORS))

        # 副标题 + 版本号
        formatter.write(click.style("\n  金山云", fg="red", bold=True))
        formatter.write(click.style(" AgentEngine", fg="yellow", bold=True))
        formatter.write(click.style(" v" + VERSION, fg="bright_black"))
        formatter.write(click.style(" - AI Agent 开发与部署平台\n\n", fg="white"))

        # 描述
        formatter.write(click.style("  支持 ", fg="white"))
        formatter.write(click.style("Hermes", fg="yellow"))
        formatter.write(click.style(" / ", fg="white"))
        formatter.write(click.style("OpenClaw", fg="yellow"))
        formatter.write(click.style(" / ", fg="white"))
        formatter.write(click.style("DeepAgents", fg="yellow"))
        formatter.write(click.style(" / ", fg="white"))
        formatter.write(click.style("LangGraph", fg="yellow"))
        formatter.write(click.style(" / ", fg="white"))
        formatter.write(click.style("LangChain", fg="yellow"))
        formatter.write(click.style(" / ", fg="white"))
        formatter.write(click.style("Google ADK", fg="yellow"))
        formatter.write(click.style(" 的本地运行与云端部署\n\n", fg="white"))

        # 本地开发
        formatter.write(click.style("  📦  本地开发:\n\n", fg="green", bold=True))
        _write_colored_help_row(formatter, "agentengine init", "初始化项目")
        _write_colored_help_row(formatter, "agentengine run", "运行 API Server")
        _write_colored_help_row(formatter, "agentengine web", "本地调试 Agent Invoke UI")

        # 云端部署
        formatter.write(click.style("  🚀  云端部署:\n\n", fg="blue", bold=True))
        _write_colored_help_row(formatter, "agentengine build", "构建部署制品")
        _write_colored_help_row(formatter, "agentengine deploy", "部署到云端")
        _write_colored_help_row(formatter, "agentengine launch", "一键构建+部署")
        _write_colored_help_row(formatter, "agentengine agent", "Agent 资源管理")
        _write_colored_help_row(formatter, "agentengine dashboard", "打开云端 Agent Dashboard")
        _write_colored_help_row(formatter, "agentengine hermes", "Hermes Agent 资源管理")
        _write_colored_help_row(formatter, "agentengine openclaw", "OpenClaw 资源管理")

        # 配置与工具
        formatter.write(click.style("  🧰  配置:\n\n", fg="yellow", bold=True))
        _write_colored_help_row(formatter, "agentengine config", "项目配置向导与模型配置")

        # 自定义 Options 格式化
        self.format_options_colored(ctx, formatter)

        # 自定义 Commands 格式化
        self.format_commands_colored(ctx, formatter)

    def format_options_colored(self, ctx, formatter):
        """自定义选项格式化"""
        formatter.write(click.style("  ⚙️  选项:\n\n", fg="yellow", bold=True))
        _write_colored_help_row(formatter, "--output", "输出格式（pretty/json）")
        _write_colored_help_row(formatter, "--no-color", "禁用颜色输出")
        _write_colored_help_row(formatter, "--version", "显示版本号")
        _write_colored_help_row(formatter, "-h, --help", "显示帮助信息", end="\n")

    def format_commands(self, ctx, formatter):
        """覆盖默认的 Commands 格式化，防止重复输出"""
        # 什么也不做，Commands 已在 format_help 中自定义输出
        pass

    def format_help_plain(self, ctx, formatter):
        formatter.write_usage(ctx.command_path, "[OPTIONS] COMMAND [ARGS]...")
        formatter.write_paragraph()
        formatter.write_text("AgentEngine CLI")
        formatter.write_text(
            "支持 Hermes / OpenClaw / DeepAgents / LangGraph / LangChain / Google ADK 的本地运行与云端部署。"
        )

        formatter.write_paragraph()
        formatter.write_text("工作流命令:")
        for name in sorted(ROOT_HELP_COMMANDS):
            formatter.write_text(f"  agentengine {name:<10} {SHORT_HELP_MAP.get(name, '')}")

        formatter.write_paragraph()
        formatter.write_text("配置:")
        for name in sorted(ROOT_HELP_TOOL_COMMANDS):
            formatter.write_text(f"  agentengine {name:<10} {SHORT_HELP_MAP.get(name, '')}")

        formatter.write_paragraph()
        formatter.write_text("全局选项:")
        formatter.write_text("  --output       输出格式（pretty/json）")
        formatter.write_text("  --no-color     禁用颜色输出")
        formatter.write_text("  --dry-run      全局 Dry Run（仅打印请求，不执行）")
        formatter.write_text("  --version      显示版本号")
        formatter.write_text("  -h, --help     显示帮助信息")

        formatter.write_paragraph()
        formatter.write_text("使用 `agentengine <command> --help` 查看子命令帮助。")

    def format_commands_colored(self, ctx, formatter):
        """自定义命令列表格式，更简洁美观"""
        icon_map = {
            "a2a": "↔️",
            "agent": "🤖",
            "build": "🔨",
            "completion": "⌨️",
            "dashboard": "🖥️",
            "deploy": "🚀",
            "files": "📄",
            "hermes": "⌁",
            "init": "📁",
            "launch": "✨",
            "mcp": "🔌",
            "openclaw": "🦞",
            "run": "▶️",
            "version": "🏷️",
            "web": "🌐",
        }

        commands = []
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            if cmd is None:
                continue
            if getattr(cmd, "hidden", False):
                continue
            if subcommand not in ROOT_HELP_COMMANDS:
                continue
            commands.append((subcommand, cmd))

        if not commands:
            return

        formatter.write(click.style("\n  📋  可用命令:\n\n", fg="magenta", bold=True))

        # Emoji、变体选择符和 CJK 字符的 Python len 与终端 cell 宽度不同。
        # 这里按 cell 宽度补齐，保证命令列和说明列在真实终端里同列。
        icon_width = max(_terminal_cell_width(icon) for icon in icon_map.values())
        max_cmd_width = max(max(_terminal_cell_width(name) for name, _ in commands), 16)

        for subcommand, cmd in commands:
            # 使用预定义的简短描述
            help_text = SHORT_HELP_MAP.get(subcommand, "")
            if not help_text and cmd.help:
                # 只取 docstring 第一行
                help_text = cmd.help.split("\n")[0].strip()

            # 格式化输出
            icon = icon_map.get(subcommand, "•")
            icon_cell = _pad_to_terminal_cells(icon, icon_width)
            name_cell = _pad_to_terminal_cells(subcommand, max_cmd_width)
            formatter.write(click.style(f"      {icon_cell}  ", fg="cyan"))
            formatter.write(click.style(f"{name_cell}        ", fg="cyan"))
            formatter.write(click.style(f"{help_text}\n\n", fg="white"))

@click.group(cls=ColoredHelpGroup, context_settings=CONTEXT_SETTINGS)
@click.version_option(version=VERSION, prog_name="AgentEngine")
@no_color_option(hidden=False)
@output_option()
@dry_run_option("全局 Dry Run（仅打印请求，不执行）", expose_value=False)
def cli(output_mode: str | None):
    """AgentEngine CLI"""
    _ = output_mode


# 延迟导入子命令，避免循环依赖
def _add_command_once(group: click.Group, command, *, name: str | None = None):
    """仅在未注册时添加命令，避免重复导入时抛错。"""
    command_name = name or command.name
    if command_name and command_name not in group.commands:
        ensure_global_cli_options(command)
        group.add_command(command, name=name)


def _register_commands():
    from ksadk.cli.cmd_create import create
    from ksadk.cli.cmd_deploy import deploy
    from ksadk.cli.cmd_run import run
    from ksadk.cli.cmd_web import web

    # 注册现有命令
    _add_command_once(cli, run)
    _add_command_once(cli, deploy)
    _add_command_once(cli, web)

    # init 作为主命令 (PRD 规范)
    _add_command_once(cli, create, name="init")

    # 注册新命令 (如果存在)
    try:
        from ksadk.cli.cmd_a2a import a2a

        _add_command_once(cli, a2a)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_files import files

        _add_command_once(cli, files)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_config import config

        _add_command_once(cli, config)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_model import model

        _add_command_once(cli, model)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_build import build

        _add_command_once(cli, build)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_launch import launch

        _add_command_once(cli, launch)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_agent import agent

        _add_command_once(cli, agent)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_status import status

        _add_command_once(cli, status)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_invoke import invoke

        _add_command_once(cli, invoke)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_dashboard import dashboard

        _add_command_once(cli, dashboard)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_destroy import delete, destroy

        _add_command_once(cli, delete)
        _add_command_once(cli, destroy)
    except ImportError:
        pass

    # MCP 命令组
    try:
        from ksadk.cli.cmd_mcp import mcp

        _add_command_once(cli, mcp)
    except ImportError:
        pass

    # Completion 命令组
    try:
        from ksadk.cli.cmd_completion import completion

        _add_command_once(cli, completion)
    except ImportError:
        pass

    # Version 命令组
    try:
        from ksadk.cli.cmd_version import version

        _add_command_once(cli, version)
    except ImportError:
        pass

    # OpenClaw 命令组
    try:
        from ksadk.cli.cmd_openclaw import openclaw

        _add_command_once(cli, openclaw)
    except ImportError:
        pass

    # Hermes 命令组
    try:
        from ksadk.cli.cmd_hermes import hermes

        _add_command_once(cli, hermes)
    except ImportError:
        pass

def main():
    # 全局加载 .env 文件
    try:
        from dotenv import find_dotenv, load_dotenv

        # 使用 find_dotenv(usecwd=True) 确保从当前工作目录开始查找 .env 文件
        # 如果当前目录不存在（已被删除），跳过 .env 加载
        try:
            dotenv_path = find_dotenv(usecwd=True)
        except (FileNotFoundError, OSError):
            # 当前工作目录不存在，跳过 .env 加载
            dotenv_path = None

        if dotenv_path:
            # 编码尝试顺序: utf-8-sig (带BOM) -> utf-8 -> 系统默认编码
            encodings_to_try = ["utf-8-sig", "utf-8"]
            loaded = False

            for enc in encodings_to_try:
                try:
                    load_dotenv(dotenv_path, override=False, encoding=enc)
                    loaded = True
                    break
                except UnicodeDecodeError:
                    continue

            if not loaded:
                # 回退到系统默认编码 (Windows 上通常是 GBK/cp936)
                import locale
                fallback_encoding = locale.getpreferredencoding(False)
                try:
                    load_dotenv(dotenv_path, override=False, encoding=fallback_encoding)
                    click.echo(
                        click.style(
                            f"⚠️  警告: .env 文件使用 {fallback_encoding} 编码，建议转换为 UTF-8\n",
                            fg="yellow"
                        ),
                        err=True
                    )
                except UnicodeDecodeError:
                    click.echo(
                        click.style(
                            f"❌ 错误: .env 文件编码无法识别，请确保使用 UTF-8 编码保存\n"
                            f"   文件路径: {dotenv_path}\n",
                            fg="red"
                        ),
                        err=True
                    )
    except ImportError:
        pass

    # 全局配置回退: .env 未设置的变量从 ~/.agentengine/settings.json 补充
    try:
        from ksadk.configs.global_config import get_env_from_global_config
        global_env = get_env_from_global_config()
        injected_keys = []
        for key, value in global_env.items():
            if not os.environ.get(key):
                os.environ[key] = value
                injected_keys.append(key)
        if injected_keys:
            os.environ["KSADK_GLOBAL_CONFIG_ENV_KEYS"] = ",".join(sorted(injected_keys))
    except Exception:
        pass

    _register_commands()
    if len(sys.argv) <= 1:
        cli.main(args=["--help"], prog_name="agentengine", standalone_mode=False)
        raise SystemExit(0)
    try:
        cli.main(prog_name="agentengine", standalone_mode=False)
    except click.exceptions.Exit as e:
        raise SystemExit(e.exit_code) from None
    except Exception as e:
        if is_debug_mode_enabled():
            raise
        cli_error = cli_error_from_exception(e, show_help=True)
        if isinstance(e, CLIError):
            cli_error = e
        emit_cli_error(cli_error)
        raise SystemExit(cli_error.exit_code) from None


if __name__ == "__main__":
    main()
