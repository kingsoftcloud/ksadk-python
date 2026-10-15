"""Static, side-effect-free validation for code-first A2A exposure plans."""

from __future__ import annotations

import ast
from pathlib import Path

_SKIP_DIRS = frozenset({".agentengine", ".git", ".venv", "venv", "__pycache__"})


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _string_value(node: ast.AST, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _assignment(statement: ast.stmt) -> tuple[str, ast.AST] | None:
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
        if isinstance(target, ast.Name):
            return target.id, statement.value
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        if statement.value is not None:
            return statement.target.id, statement.value
    return None


def _spec_from_call(call: ast.Call, constants: dict[str, str]) -> dict[str, str] | None:
    if _call_name(call.func) != "RemoteAgentSpec":
        return None
    values: dict[str, str] = {}
    for keyword in call.keywords:
        if keyword.arg in {"local_name", "binding_id", "target_agent_id", "exposure"}:
            value = _string_value(keyword.value, constants)
            if value is not None:
                values[keyword.arg] = value
    if "target_agent_id" not in values:
        return None
    values.setdefault("local_name", "<unknown>")
    values.setdefault("binding_id", "<unknown>")
    values.setdefault("exposure", "auto")
    return values


def _resolve_plan_entries(
    expression: ast.AST,
    *,
    constants: dict[str, str],
    specs: dict[str, dict[str, str]],
) -> list[dict[str, str]] | None:
    if not isinstance(expression, (ast.List, ast.Tuple)):
        return None
    resolved: list[dict[str, str]] = []
    for element in expression.elts:
        if isinstance(element, ast.Name) and element.id in specs:
            resolved.append(specs[element.id])
            continue
        if isinstance(element, ast.Call):
            spec = _spec_from_call(element, constants)
            if spec is not None:
                resolved.append(spec)
                continue
        return None
    return resolved


def _top_level_calls(statement: ast.stmt) -> tuple[ast.Call, ...]:
    value: ast.AST | None = None
    if isinstance(statement, ast.Expr):
        value = statement.value
    else:
        assignment = _assignment(statement)
        if assignment is not None:
            value = assignment[1]
    return (value,) if isinstance(value, ast.Call) else ()


def _validate_file(path: Path, project_dir: Path) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return

    constants: dict[str, str] = {}
    for statement in tree.body:
        assignment = _assignment(statement)
        if assignment is None:
            continue
        name, value = assignment
        resolved = _string_value(value, constants)
        if resolved is not None:
            constants[name] = resolved

    specs: dict[str, dict[str, str]] = {}
    for statement in tree.body:
        assignment = _assignment(statement)
        if assignment is None:
            continue
        name, value = assignment
        if isinstance(value, ast.Call):
            spec = _spec_from_call(value, constants)
            if spec is not None:
                specs[name] = spec

    for statement in tree.body:
        for call in _top_level_calls(statement):
            if _call_name(call.func) != "compile_exposure_plan" or not call.args:
                continue
            entries = _resolve_plan_entries(call.args[0], constants=constants, specs=specs)
            if entries is None:
                continue
            by_target: dict[str, dict[str, str]] = {}
            for entry in entries:
                target = entry["target_agent_id"]
                previous = by_target.get(target)
                if previous is None:
                    by_target[target] = entry
                    continue
                relative = path.relative_to(project_dir).as_posix()
                raise ValueError(
                    "A2A_EXPOSURE_CONFLICT: "
                    f"{relative}:{call.lineno} target {target!r} is declared by "
                    f"{previous['local_name']!r}/{previous['exposure']!r} and "
                    f"{entry['local_name']!r}/{entry['exposure']!r}"
                )


def validate_a2a_exposure_sources(project_dir: Path) -> None:
    """Reject statically resolvable duplicate targets without importing agent code."""
    project_dir = Path(project_dir).resolve()
    for path in sorted(project_dir.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.relative_to(project_dir).parts):
            continue
        _validate_file(path, project_dir)


__all__ = ["validate_a2a_exposure_sources"]
