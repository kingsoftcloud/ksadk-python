"""Detect optional runtime dependencies without importing user code."""

import ast
from pathlib import Path


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def imports_any(path: Path, module_names: set[str]) -> bool:
    tree = _parse(path)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        else:
            continue
        if any(n == target or n.startswith(target + ".") for n in names for target in module_names):
            return True
    return False


def uses_tunnel(path: Path) -> bool:
    tree = _parse(path)
    if tree is None:
        return False

    factories = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [(a.name, a.asname or a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [(f"{node.module}.{a.name}", a.asname or a.name) for a in node.names]
        else:
            continue
        for name, local in names:
            if name == "ksadk.tunnel" or name.startswith("ksadk.tunnel."):
                return True
            if name in {"ksadk.Agent", "ksadk.agent.Agent"}:
                factories.add(local)
            elif name in {"ksadk", "ksadk.agent"}:
                factories.add(f"{local}.Agent")
                if name == "ksadk":
                    factories.add(f"{local}.agent.Agent")

    if not factories:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, (ast.Name, ast.Attribute)):
            continue
        if ast.unparse(node.func) not in factories:
            continue
        for kw in node.keywords:
            if kw.arg is None:  # **options may enable the tunnel at runtime.
                return True
            if kw.arg == "enable_tunnel":
                if not isinstance(kw.value, ast.Constant) or kw.value.value is not False:
                    return True
    return False
