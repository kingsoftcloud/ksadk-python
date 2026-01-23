"""
agentengine completion - Shell 自动补全安装

支持 Bash 和 Zsh 两种 Shell。
"""

import os
import sys
import click
from pathlib import Path


@click.group()
def completion():
    """安装 Shell 自动补全"""
    pass


@completion.command("bash")
def completion_bash():
    """输出 Bash 补全脚本"""
    script = '''
_agentengine_completion() {
    local IFS=$'\\n'
    COMPREPLY=( $( env COMP_WORDS="${COMP_WORDS[*]}" \\
                   COMP_CWORD=$COMP_CWORD \\
                   _AGENTENGINE_COMPLETE=bash_complete $1 ) )
    return 0
}

complete -o default -F _agentengine_completion agentengine
'''
    click.echo(script.strip())


@completion.command("zsh")
def completion_zsh():
    """输出 Zsh 补全脚本"""
    script = '''
#compdef agentengine

_agentengine() {
    local -a completions
    local -a completions_with_descriptions
    local -a response
    response=("${(@f)$( env COMP_WORDS="${words[*]}" \\
                        COMP_CWORD=$((CURRENT-1)) \\
                        _AGENTENGINE_COMPLETE=zsh_complete agentengine )}")

    for key descr in ${(kv)response}; do
        if [[ "$descr" == "_" ]]; then
            completions+=("$key")
        else
            completions_with_descriptions+=("$key":"$descr")
        fi
    done

    if [ -n "$completions_with_descriptions" ]; then
        _describe -V unsorted completions_with_descriptions -U
    fi

    if [ -n "$completions" ]; then
        compadd -U -V unsorted -a completions
    fi
}

compdef _agentengine agentengine
'''
    click.echo(script.strip())


@completion.command("install")
@click.option("--shell", type=click.Choice(["bash", "zsh", "auto"]), default="auto", 
              help="指定 Shell 类型")
def completion_install(shell: str):
    """自动安装补全脚本到 Shell 配置文件"""
    
    # 自动检测 Shell
    if shell == "auto":
        current_shell = os.environ.get("SHELL", "")
        if "zsh" in current_shell:
            shell = "zsh"
        elif "bash" in current_shell:
            shell = "bash"
        else:
            click.secho(f"⚠️  无法自动检测 Shell 类型: {current_shell}", fg="yellow")
            click.echo("请使用 --shell=bash 或 --shell=zsh 指定")
            return
    
    home = Path.home()
    
    if shell == "zsh":
        rc_file = home / ".zshrc"
        completion_file = home / ".agentengine-complete.zsh"
        completion_cmd = '_AGENTENGINE_COMPLETE=zsh_source agentengine'
        init_line = "autoload -Uz compinit && compinit"
    else:  # bash
        rc_file = home / ".bashrc"
        completion_file = home / ".agentengine-complete.bash"
        completion_cmd = '_AGENTENGINE_COMPLETE=bash_source agentengine'
        init_line = None
    
    click.echo(f"🔧 正在为 {click.style(shell, fg='cyan', bold=True)} 安装补全脚本...")
    
    # 生成补全脚本
    try:
        import subprocess
        env = os.environ.copy()
        env["_AGENTENGINE_COMPLETE"] = f"{shell}_source"
        
        result = subprocess.run(
            [sys.executable, "-m", "ksadk.cli"],
            env=env,
            capture_output=True,
            text=True
        )
        
        completion_script = result.stdout
        
        if not completion_script.strip():
            click.secho("❌ 生成补全脚本失败", fg="red")
            click.echo("\n💡 请尝试手动安装:")
            click.echo(f"   {completion_cmd} > {completion_file}")
            click.echo(f"   echo 'source {completion_file}' >> {rc_file}")
            return
        
        # 写入补全脚本文件
        with open(completion_file, "w") as f:
            f.write(completion_script)
        
        click.echo(f"   ✅ 补全脚本已保存到: {completion_file}")
        
    except Exception as e:
        click.secho(f"❌ 生成补全脚本失败: {e}", fg="red")
        click.echo("\n💡 请尝试手动安装:")
        click.echo(f"   {completion_cmd} > {completion_file}")
        click.echo(f"   echo 'source {completion_file}' >> {rc_file}")
        return
    
    # 检查并更新 rc 文件
    source_line = f"source {completion_file}"
    
    rc_content = ""
    if rc_file.exists():
        with open(rc_file, "r") as f:
            rc_content = f.read()
    
    lines_to_add = []
    
    # 对于 zsh，需要确保 compinit 已初始化
    if shell == "zsh" and init_line and init_line not in rc_content:
        lines_to_add.append(f"\n# zsh 补全系统初始化\n{init_line}")
        click.echo(f"   ✅ 添加 compinit 初始化")
    
    # 添加 source 行
    if source_line not in rc_content:
        lines_to_add.append(f"\n# AgentEngine CLI 自动补全\n{source_line}")
        click.echo(f"   ✅ 添加补全脚本加载")
    else:
        click.echo(f"   ℹ️  补全配置已存在，跳过")
    
    if lines_to_add:
        with open(rc_file, "a") as f:
            for line in lines_to_add:
                f.write(line + "\n")
    
    click.echo("")
    click.secho("🎉 安装完成！", fg="green", bold=True)
    click.echo("")
    click.echo("请运行以下命令使其生效:")
    click.echo(click.style(f"   source {rc_file}", fg="cyan"))
    click.echo("")
    click.echo("之后输入 " + click.style("agentengine ", fg="yellow") + "并按 " + 
               click.style("Tab", fg="cyan", bold=True) + " 键即可自动补全")
