import subprocess
from pathlib import Path


def test_node_bridge_protocol_suite():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["node", "--test", str(root / "tests/fixtures/resource_runtime/node_bridge.test.mjs")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
