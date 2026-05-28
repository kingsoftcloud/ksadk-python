"""
agentengine completion - Shell 自动补全安装

支持 Bash 和 Zsh 两种 Shell。
"""

import os
import re
import sys
import click
from pathlib import Path
from ksadk.cli.error_utils import ensure_json_output_supported, print_exception
from ksadk.cli.resource_common import CONTEXT_SETTINGS
from ksadk.cli.ui import (
    print_error,
    print_info,
    print_kv,
    print_success,
    print_title,
    print_warn,
)


@click.group("completion", context_settings=CONTEXT_SETTINGS)
def completion():
    """Shell 补全管理。"""
    pass


def _normalize_source_line(path: Path) -> str:
    return f'source "{path}"'


def _detect_shell(shell: str) -> str | None:
    if shell != "auto":
        return shell

    current_shell = os.environ.get("SHELL", "").lower()
    if "zsh" in current_shell:
        return "zsh"
    if "bash" in current_shell:
        return "bash"

    # Git Bash / MSYS2 / WSL 常见场景下，SHELL 可能为空或不可靠，兜底按 bash 处理。
    if os.environ.get("MSYSTEM") or os.environ.get("WSL_DISTRO_NAME"):
        return "bash"

    return None


def _resolve_bash_rc_file(home: Path) -> Path:
    if os.environ.get("MSYSTEM") or os.environ.get("WSL_DISTRO_NAME"):
        candidates = [home / ".bashrc", home / ".bash_profile", home / ".profile"]
    elif sys.platform == "darwin":
        candidates = [home / ".bash_profile", home / ".bashrc", home / ".profile"]
    else:
        candidates = [home / ".bashrc", home / ".bash_profile", home / ".profile"]

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _has_zsh_compinit(rc_content: str) -> bool:
    return re.search(r"(?m)^\s*(?:autoload\s+-Uz\s+compinit(?:\s*&&\s*compinit)?|compinit)\s*$", rc_content) is not None


def _rewrite_zsh_rc(rc_content: str, completion_file: Path, init_line: str) -> tuple[str, bool]:
    source_line = _normalize_source_line(completion_file)
    escaped_path = re.escape(str(completion_file))

    # 清理旧的 AgentEngine 自动补全配置，避免重复 source / eval。
    updated = re.sub(
        r'(?ms)^\s*if command -v agentengine >/dev/null 2>&1; then\s*\n\s*eval "\$\(_AGENTENGINE_COMPLETE=zsh_source agentengine\)"\s*\n\s*fi\s*\n?',
        "",
        rc_content,
    )
    updated = re.sub(
        r'(?m)^\s*eval "\$\(_AGENTENGINE_COMPLETE=zsh_source agentengine\)"\s*$\n?',
        "",
        updated,
    )
    updated = re.sub(
        rf'(?m)^\s*source\s+["\']?{escaped_path}["\']?\s*$\n?',
        "",
        updated,
    )
    updated = re.sub(
        r'(?m)^\s*# AgentEngine CLI 自动补全\s*$\n?',
        "",
        updated,
    )
    updated = updated.rstrip()

    blocks: list[str] = []
    added_compinit = False
    if not _has_zsh_compinit(updated):
        blocks.append(f"# zsh 补全系统初始化\n{init_line}")
        added_compinit = True
    blocks.append(f"# AgentEngine CLI 自动补全\n{source_line}")

    if updated:
        updated = updated + "\n\n" + "\n\n".join(blocks) + "\n"
    else:
        updated = "\n\n".join(blocks) + "\n"
    return updated, added_compinit


def _rewrite_bash_rc(rc_content: str, completion_file: Path) -> str:
    source_line = _normalize_source_line(completion_file)
    escaped_path = re.escape(str(completion_file))
    updated = re.sub(
        rf'(?m)^\s*source\s+["\']?{escaped_path}["\']?\s*$\n?',
        "",
        rc_content,
    )
    updated = re.sub(
        r'(?m)^\s*if command -v agentengine >/dev/null 2>&1; then\s*$\n?'
        r'^\s*eval "\$\(_AGENTENGINE_COMPLETE=bash_source agentengine\)"\s*$\n?'
        r'^\s*fi\s*$\n?',
        "",
        updated,
    )
    updated = re.sub(
        r'(?m)^\s*eval "\$\(_AGENTENGINE_COMPLETE=bash_source agentengine\)"\s*$\n?',
        "",
        updated,
    )
    updated = re.sub(
        r'(?m)^\s*# AgentEngine CLI 自动补全\s*$\n?',
        "",
        updated,
    )
    updated = updated.rstrip()
    block = f"# AgentEngine CLI 自动补全\n{source_line}\n"
    return updated + "\n\n" + block if updated else block


@completion.command("bash", context_settings=CONTEXT_SETTINGS)
def completion_bash():
    """输出 Bash 补全脚本"""
    script = '''
_agentengine_completion() {
    local IFS=$'\\n'
    local line
    local out
    COMPREPLY=()

    out="$( env COMP_WORDS="${COMP_WORDS[*]}" \\
              COMP_CWORD=$COMP_CWORD \\
              _AGENTENGINE_COMPLETE=bash_complete $1 )"

    for line in $out; do
        # Click completion entries are typed tuples, e.g.:
        #   plain,run
        #   file,/path/to/file
        # Keep only the value portion for shell candidates.
        if [[ "$line" == *,* ]]; then
            line="${line#*,}"
        fi
        COMPREPLY+=("$line")
    done
    return 0
}

complete -o default -F _agentengine_completion agentengine
'''
    click.echo(script.strip())


@completion.command("zsh", context_settings=CONTEXT_SETTINGS)
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


@completion.command("install", context_settings=CONTEXT_SETTINGS)
@click.option("--shell", type=click.Choice(["bash", "zsh", "auto"]), default="auto", 
              help="指定 Shell 类型")
def completion_install(shell: str):
    """自动安装补全脚本到 Shell 配置文件"""
    ensure_json_output_supported(
        "agentengine completion install",
        suggestion="请直接使用 `agentengine completion bash` 或 `agentengine completion zsh` 获取脚本内容。",
    )
    print_title("安装自动补全")

    # 自动检测 Shell
    resolved_shell = _detect_shell(shell)
    if resolved_shell is None:
        current_shell = os.environ.get("SHELL", "")
        print_warn(f"无法自动检测 Shell 类型: {current_shell}")
        print_info("请使用 --shell=bash 或 --shell=zsh 指定")
        return
    shell = resolved_shell
    
    home = Path.home()
    
    if shell == "zsh":
        rc_file = home / ".zshrc"
        completion_file = home / ".agentengine-complete.zsh"
        completion_cmd = '_AGENTENGINE_COMPLETE=zsh_source agentengine'
        init_line = "autoload -Uz compinit && compinit"
    else:  # bash
        rc_file = _resolve_bash_rc_file(home)
        completion_file = home / ".agentengine-complete.bash"
        completion_cmd = '_AGENTENGINE_COMPLETE=bash_source agentengine'
        init_line = None
    
    print_kv("目标 Shell", shell, value_style="#58a6ff")
    print_kv("配置文件", rc_file, value_style="#58a6ff")
    print_info("正在安装补全脚本...")
    
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
            print_error("生成补全脚本失败")
            print_info("请尝试手动安装:")
            print_info(f"{completion_cmd} > {completion_file}")
            print_info(f'echo \'source "{completion_file}"\' >> {rc_file}')
            return
        
        # 写入补全脚本文件
        with open(completion_file, "w") as f:
            f.write(completion_script)
        
        print_success(f"补全脚本已保存到: {completion_file}")
        
    except Exception as e:
        print_exception("生成补全脚本失败", e)
        print_info("请尝试手动安装:")
        print_info(f"{completion_cmd} > {completion_file}")
        print_info(f'echo \'source "{completion_file}"\' >> {rc_file}')
        return
    
    # 检查并更新 rc 文件
    rc_content = ""
    if rc_file.exists():
        with open(rc_file, "r") as f:
            rc_content = f.read()

    if shell == "zsh":
        updated_rc, added_compinit = _rewrite_zsh_rc(rc_content, completion_file, init_line)
        if added_compinit:
            print_success("已添加 compinit 初始化")
        print_success("已更新补全脚本加载顺序")
    else:
        updated_rc = _rewrite_bash_rc(rc_content, completion_file)
        print_success("已更新补全脚本加载")

    if updated_rc != rc_content:
        with open(rc_file, "w") as f:
            f.write(updated_rc)
    else:
        print_info("补全配置已存在，跳过")
    
    print_success("安装完成")
    print_info("请运行以下命令使其生效:")
    print_kv("命令", f"source {rc_file}", value_style="#58a6ff")
    print_info("之后输入 `agentengine ` 并按 `Tab` 键即可自动补全")
