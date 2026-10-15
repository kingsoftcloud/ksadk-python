"""Pure A2A contracts must be usable without optional execution backends."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_studio_import_does_not_require_optional_database_or_agent_frameworks():
    root = Path(__file__).resolve().parents[2]
    script = """
import importlib.abc
import sys
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sqlalchemy', 'langgraph', 'langchain',
                                     'langchain_core', 'langchain_openai', 'deepagents'}:
            raise ModuleNotFoundError('optional backend unexpectedly loaded', name=fullname)
sys.meta_path.insert(0, BlockOptional())
from ksadk.a2a.ids import require_a2a_resource_id
from ksadk.studio.api import create_studio_app
assert callable(create_studio_app)
assert 'ksadk.a2a.context_store' not in sys.modules
assert 'ksadk.a2a.langgraph' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={**os.environ, "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_public_exports_preserve_identity_and_attribute_errors():
    import ksadk.a2a as package
    from ksadk.a2a.invocation_types import A2AInvocationRequest

    assert package.A2AInvocationRequest is A2AInvocationRequest
    assert package.A2AInvocationRequest is package.A2AInvocationRequest
    assert set(package.__all__) <= set(dir(package))
    try:
        package.no_such_public_export
    except AttributeError as exc:
        assert "no_such_public_export" in str(exc)
    else:
        raise AssertionError("unknown exports must raise AttributeError")
