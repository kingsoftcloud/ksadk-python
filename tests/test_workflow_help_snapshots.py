from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ksadk.cli.cmd_build import build
from ksadk.cli.cmd_deploy import deploy
from ksadk.cli.cmd_launch import launch


SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "workflow_help_snapshots.txt"


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
    return text.rstrip() + "\n"


def test_workflow_help_snapshots_match():
    runner = CliRunner()
    snapshots = load_section_snapshots(SNAPSHOT_FILE)
    commands = {
        "build_help": (build, ["--help"]),
        "deploy_help": (deploy, ["--help"]),
        "launch_help": (launch, ["--help"]),
    }

    for section_name, (command, argv) in commands.items():
        result = runner.invoke(command, argv)
        assert result.exit_code == 0, result.output
        assert _normalize_help(result.output) == snapshots[section_name]
