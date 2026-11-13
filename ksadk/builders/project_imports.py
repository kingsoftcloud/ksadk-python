"""Static import detection for build-time optional runtime dependencies."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path


def project_imports_any(files: Iterable[Path], module_names: set[str]) -> bool:
    """Inspect sources without importing user code; ignore unreadable/invalid files."""
    def matches(import_name: str) -> bool:
        return any(
            import_name == name or import_name.startswith(f"{name}.")
            for name in module_names
        )

    for py_file in files:
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(matches(alias.name) for alias in node.names):
                    return True
            elif isinstance(node, ast.ImportFrom) and node.module:
                if matches(node.module):
                    return True
                if (
                    node.module == "ksadk"
                    and "ksadk.tunnel" in module_names
                    and any(alias.name == "tunnel" for alias in node.names)
                ):
                    return True
    return False
