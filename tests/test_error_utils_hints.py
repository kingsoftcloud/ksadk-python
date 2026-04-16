from pathlib import Path

from ksadk.api.client import AgentEngineAPIError
from ksadk.cli.error_utils import explain_exception


SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "error_hint_snapshots.txt"


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


def test_dashboard_not_found_hint_points_to_canonical_open():
    err = Exception("Server API Error (Code: 404): Agent not found")

    _, hints = explain_exception(err, argv=["dashboard"])

    assert any("agentengine agent list" in hint for hint in hints)
    assert any("agentengine dashboard open --agent" in hint for hint in hints)


def test_dashboard_list_hint_points_to_share_list():
    err = Exception("Server API Error (Code: 404): Agent not found")

    _, hints = explain_exception(err, argv=["dashboard", "list"])

    assert any("dashboard list" in hint for hint in hints)
    assert any("dashboard share list" in hint for hint in hints)


def test_error_hint_snapshots_match_canonical_hints():
    snapshots = load_section_snapshots(SNAPSHOT_FILE)
    cases = {
        "dashboard_not_found": (
            Exception("Server API Error (Code: 404): Agent not found"),
            ["dashboard"],
        ),
        "dashboard_list_not_found": (
            Exception("Server API Error (Code: 404): Agent not found"),
            ["dashboard", "list"],
        ),
        "dashboard_share_not_found": (
            Exception("Server API Error (Code: 404): Agent not found"),
            ["dashboard", "share", "list"],
        ),
        "mcp_not_found": (
            Exception("Server API Error (Code: 404): MCP not found"),
            ["mcp", "status"],
        ),
        "openclaw_not_found": (
            Exception("Server API Error (Code: 404): OpenClaw not found"),
            ["openclaw", "status"],
        ),
        "version_not_found": (
            Exception("Server API Error (Code: 404): Version not found"),
            ["version", "list"],
        ),
        "auth_failed": (
            Exception("Server API Error (Code: 401): unauthorized"),
            ["mcp", "status"],
        ),
        "missing_aksk": (
            AgentEngineAPIError(
                400,
                "Access Key is Missing",
                details={
                    "http_status": 400,
                    "remote_error_code": "MissingAccesskey",
                    "remote_error_message": "Access Key is Missing",
                    "request_id": "req-missing-ak",
                },
            ),
            ["hermes", "status"],
        ),
        "invalid_aksk": (
            AgentEngineAPIError(
                403,
                "The Access Key Id you provided does not exist",
                details={
                    "http_status": 403,
                    "remote_error_code": "InvalidAccessKey",
                    "remote_error_message": "The Access Key Id you provided does not exist",
                    "request_id": "req-invalid-ak",
                },
            ),
            ["agent", "status"],
        ),
        "missing_runtime_permission": (
            AgentEngineAPIError(
                403,
                "当前账号没有 KsyunAgentEngineDefaultRole 权限",
                details={
                    "http_status": 403,
                    "remote_error_code": "AccessDenied",
                    "remote_error_message": "当前账号没有 KsyunAgentEngineDefaultRole 权限",
                    "request_id": "req-no-role",
                },
            ),
            ["openclaw", "status"],
        ),
    }

    for name, (err, argv) in cases.items():
        summary, hints = explain_exception(err, argv=argv)
        actual = "\n".join([summary, *[f"- {hint}" for hint in hints]]).rstrip() + "\n"
        assert actual == snapshots[name]


def test_missing_aksk_hint_points_to_credential_and_permission_docs():
    err = AgentEngineAPIError(
        400,
        "Access Key is Missing",
        details={
            "http_status": 400,
            "remote_error_code": "MissingAccesskey",
            "remote_error_message": "Access Key is Missing",
        },
    )

    summary, hints = explain_exception(err, argv=["hermes", "status"])

    assert "AK/SK" in summary
    assert any("KSYUN_ACCESS_KEY" in hint for hint in hints)
    assert any("agentEngineRuntime" in hint for hint in hints)
    assert any("/permission/authorize" in hint for hint in hints)
    assert any("/pro/iam/" in hint for hint in hints)
