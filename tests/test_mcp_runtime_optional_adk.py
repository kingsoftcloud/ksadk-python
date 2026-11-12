"""A LangGraph Code artifact does not install the optional ADK framework."""

import subprocess
import sys
import textwrap
from pathlib import Path


def test_oauth_helper_and_local_runtime_import_without_google_adk():
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import importlib.abc
            import sys

            class NoAdk(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "google.adk" or fullname.startswith("google.adk."):
                        raise ModuleNotFoundError("ADK deliberately absent", name=fullname)

            sys.meta_path.insert(0, NoAdk())
            from ksadk.mcp_runtime import MCPServerConfig, build_connection_params
            from ksadk.mcp_runtime.oauth import call_mcp_tool, create_mcp_agent
            from ksadk.mcp_runtime.oauth.local.runtime import LocalOAuthRuntime
            assert callable(call_mcp_tool)
            assert callable(create_mcp_agent)
            assert "ksadk.mcp_runtime.oauth._agent" not in sys.modules
            assert LocalOAuthRuntime is not None
            assert not any(name.startswith("google.adk") for name in sys.modules)
            try:
                build_connection_params(MCPServerConfig("static", "https://mcp.example/mcp"))
            except ImportError as error:
                assert "google-adk" in str(error)
            else:
                raise AssertionError("ADK connection still requires ADK installation")
        """)],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
