"""
AgentEngine CLI - 命令行工具入口

使用方式:
    agentengine init myapp    # 初始化项目
    agentengine run .         # 本地运行
    agentengine web .         # 启动 Web UI
    agentengine build .       # 构建镜像
    agentengine deploy .      # 部署到云端
    agentengine launch .      # 一键构建+部署
    agentengine status        # 查看状态
    agentengine invoke        # 与 Agent 交互
    agentengine destroy       # 销毁实例

别名: ksadk (向后兼容)
"""

import click
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
        formatter.write(click.style("LangGraph", fg="yellow"))
        formatter.write(click.style(" / ", fg="white"))
        formatter.write(click.style("LangChain", fg="yellow"))
        formatter.write(click.style(" / ", fg="white"))
        formatter.write(click.style("Google ADK", fg="yellow"))
        formatter.write(click.style(" 的本地运行与云端部署\n\n", fg="white"))

        # 本地开发
        formatter.write(click.style("  📦 本地开发:\n\n", fg="green", bold=True))
        formatter.write(click.style("      agentengine run .     ", fg="cyan"))
        formatter.write(click.style("运行 API Server\n\n", fg="white"))
        formatter.write(click.style("      agentengine web .     ", fg="cyan"))
        formatter.write(click.style("启动 Web UI\n\n", fg="white"))

        # 云端部署
        formatter.write(click.style("  🚀 云端部署:\n\n", fg="blue", bold=True))
        formatter.write(click.style("      agentengine init      ", fg="cyan"))
        formatter.write(click.style("初始化项目\n\n", fg="white"))
        formatter.write(click.style("      agentengine build     ", fg="cyan"))
        formatter.write(click.style("构建镜像\n\n", fg="white"))
        formatter.write(click.style("      agentengine deploy    ", fg="cyan"))
        formatter.write(click.style("部署到云端\n\n", fg="white"))
        formatter.write(click.style("      agentengine launch    ", fg="cyan"))
        formatter.write(click.style("一键构建+部署\n\n", fg="white"))
        formatter.write(click.style("      agentengine status    ", fg="cyan"))
        formatter.write(click.style("查看状态\n\n", fg="white"))
        formatter.write(click.style("      agentengine invoke    ", fg="cyan"))
        formatter.write(click.style("与 Agent 交互\n\n", fg="white"))
        formatter.write(click.style("      agentengine destroy   ", fg="cyan"))
        formatter.write(click.style("销毁实例\n\n", fg="white"))

        # 自定义 Options 格式化
        self.format_options_colored(ctx, formatter)

        # 自定义 Commands 格式化
        self.format_commands_colored(ctx, formatter)

    def format_options_colored(self, ctx, formatter):
        """自定义选项格式化"""
        formatter.write(click.style("  ⚙️  选项:\n\n", fg="yellow", bold=True))
        formatter.write(click.style("      --version      ", fg="cyan"))
        formatter.write(click.style("显示版本号\n\n", fg="white"))
        formatter.write(click.style("      -h, --help     ", fg="cyan"))
        formatter.write(click.style("显示帮助信息\n", fg="white"))

    def format_commands(self, ctx, formatter):
        """覆盖默认的 Commands 格式化，防止重复输出"""
        # 什么也不做，Commands 已在 format_help 中自定义输出
        pass

    def format_commands_colored(self, ctx, formatter):
        """自定义命令列表格式，更简洁美观"""
        # 定义简短描述映射
        short_help_map = {
            "build": "构建 Docker 镜像",
            "config": "配置项目",
            "deploy": "部署到云端",
            "destroy": "销毁实例",
            "init": "创建新项目",
            "invoke": "交互测试",
            "launch": "一键构建+部署",
            "mcp": "MCP Server 管理",
            "model": "切换默认模型",
            "run": "运行 Agent",
            "status": "查看状态",
            "web": "启动 Web UI",
        }

        commands = []
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            if cmd is None:
                continue
            commands.append((subcommand, cmd))

        if not commands:
            return

        formatter.write(click.style("\n  📋 可用命令:\n\n", fg="magenta", bold=True))

        # 计算最大命令名长度用于对齐
        max_len = max(len(cmd[0]) for cmd in commands)

        for subcommand, cmd in commands:
            # 使用预定义的简短描述
            help_text = short_help_map.get(subcommand, "")
            if not help_text and cmd.help:
                # 只取 docstring 第一行
                help_text = cmd.help.split("\n")[0].strip()

            # 格式化输出
            padding = " " * (max_len - len(subcommand) + 2)
            formatter.write(click.style(f"      {subcommand}", fg="cyan"))
            formatter.write(click.style(f"{padding}{help_text}\n\n", fg="white"))


# 添加 -h 短选项支持
CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


@click.group(cls=ColoredHelpGroup, context_settings=CONTEXT_SETTINGS)
@click.version_option(version=VERSION, prog_name="AgentEngine")
def cli():
    """AgentEngine CLI"""
    pass


# 延迟导入子命令，避免循环依赖
def _register_commands():
    from ksadk.cli.cmd_run import run
    from ksadk.cli.cmd_deploy import deploy
    from ksadk.cli.cmd_web import web
    from ksadk.cli.cmd_create import create

    # 注册现有命令
    cli.add_command(run)
    cli.add_command(deploy)
    cli.add_command(web)

    # init 作为主命令 (PRD 规范)
    cli.add_command(create, name="init")

    # 注册新命令 (如果存在)
    try:
        from ksadk.cli.cmd_config import config

        cli.add_command(config)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_model import model

        cli.add_command(model)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_build import build

        cli.add_command(build)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_launch import launch

        cli.add_command(launch)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_status import status

        cli.add_command(status)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_invoke import invoke

        cli.add_command(invoke)
    except ImportError:
        pass

    try:
        from ksadk.cli.cmd_destroy import destroy

        cli.add_command(destroy)
    except ImportError:
        pass

    # MCP 命令组
    try:
        from ksadk.cli.cmd_mcp import mcp

        cli.add_command(mcp)
    except ImportError:
        pass

    # Completion 命令组
    try:
        from ksadk.cli.cmd_completion import completion

        cli.add_command(completion)
    except ImportError:
        pass


def main():
    # 全局加载 .env 文件
    try:
        from dotenv import load_dotenv, find_dotenv

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

    _register_commands()
    cli()


if __name__ == "__main__":
    main()
