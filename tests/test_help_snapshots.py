from __future__ import annotations

import re
from pathlib import Path

from click.testing import CliRunner
from click.utils import strip_ansi
from rich.cells import cell_len

import ksadk.cli as cli_module
from ksadk.cli import ROOT_HELP_COMMANDS, SHORT_HELP_MAP, _register_commands, cli

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "help_snapshots.txt"


def load_section_snapshots(path: Path) -> dict[str, str]:
    sections: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("=== ") and line.endswith(" ==="):
            if current_name is not None:
                sections[current_name] = "\n".join(current_lines).rstrip() + "\n"
            current_name = line[4:-4]
            current_lines = []
            continue
        current_lines.append(line)

    if current_name is not None:
        sections[current_name] = "\n".join(current_lines).rstrip() + "\n"

    return sections


def _normalize_help(text: str) -> str:
    text = re.sub(r"v\d+\.\d+\.\d+(?:[-+][^\s]+)?", "vX.Y.Z", text)
    return text.rstrip() + "\n"


def test_help_snapshots_match_canonical_cli_surface():
    _register_commands()
    runner = CliRunner()
    snapshots = load_section_snapshots(SNAPSHOT_FILE)

    commands = {
        "root_help": ["--help"],
        "a2a_help": ["a2a", "--help"],
        "a2a_serve_help": ["a2a", "serve", "--help"],
        "a2a_card_help": ["a2a", "card", "--help"],
        "agent_help": ["agent", "--help"],
        "dashboard_help": ["dashboard", "--help"],
        "dashboard_open_help": ["dashboard", "open", "--help"],
        "hermes_help": ["hermes", "--help"],
        "mcp_help": ["mcp", "--help"],
        "mcp_build_help": ["mcp", "build", "--help"],
        "openclaw_help": ["openclaw", "--help"],
        "version_help": ["version", "--help"],
        "config_help": ["config", "--help"],
        "config_wizard_help": ["config", "wizard", "--help"],
        "config_show_help": ["config", "show", "--help"],
        "config_set_help": ["config", "set", "--help"],
        "config_model_help": ["config", "model", "--help"],
        "completion_help": ["completion", "--help"],
        "model_alias_help": ["model", "--help"],
        "status_alias_help": ["status", "--help"],
    }

    for name, argv in commands.items():
        result = runner.invoke(cli, argv)
        assert result.exit_code == 0, result.output
        assert _normalize_help(result.output) == snapshots[name]


def test_colored_root_help_command_columns_align_with_unicode_icons(monkeypatch):
    _register_commands()
    monkeypatch.setattr(cli_module, "should_render_banner", lambda: True)

    result = CliRunner().invoke(cli, ["--help"], color=True)

    assert result.exit_code == 0, result.output

    command_lines: list[str] = []
    in_commands = False
    for line in strip_ansi(result.output).splitlines():
        if "可用命令:" in line:
            in_commands = True
            continue
        if in_commands and line.startswith("      ") and line.strip():
            command_lines.append(line)

    assert len(command_lines) == len(ROOT_HELP_COMMANDS)

    command_offsets: set[int] = set()
    description_offsets: set[int] = set()
    for line in command_lines:
        command_name = next(
            name for name in ROOT_HELP_COMMANDS if re.search(rf"\b{re.escape(name)}\b", line)
        )
        description = SHORT_HELP_MAP[command_name]

        command_offsets.add(cell_len(line[: line.index(command_name)]))
        description_offsets.add(cell_len(line[: line.index(description)]))

    assert command_offsets == {10}
    assert description_offsets == {34}
