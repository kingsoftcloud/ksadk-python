"""AST-only, inspect-before-commit staging for uploaded Python tools."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
from pathlib import Path, PurePath
from typing import Any
from uuid import uuid4

from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace

_MAX_SOURCE_BYTES = 1024 * 1024
_TOKEN = re.compile(r"^pyti_[0-9a-f]{32}$")


class PythonToolInspector:
    """Stage Python source and expose declarations without importing it."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.root = workspace.resolve(".agentkit/catalog/python-tool-inspections")
        self.root.mkdir(parents=True, exist_ok=True)

    def inspect(self, content: bytes, *, filename: str) -> dict[str, Any]:
        normalized_filename = str(filename or "").strip()
        if (
            not normalized_filename
            or PurePath(normalized_filename).name != normalized_filename
            or not normalized_filename.endswith(".py")
        ):
            raise StudioError(
                "PYTHON_TOOL_FILENAME_INVALID",
                "Python Tool 必须是单个 .py 文件",
                status_code=422,
                field="file",
            )
        if not content:
            raise StudioError(
                "PYTHON_TOOL_EMPTY",
                "Python Tool 文件不能为空",
                status_code=422,
                field="file",
            )
        if len(content) > _MAX_SOURCE_BYTES:
            raise StudioError(
                "PYTHON_TOOL_TOO_LARGE",
                "Python Tool 源码不能超过 1 MiB",
                status_code=413,
                field="file",
            )
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StudioError(
                "PYTHON_TOOL_ENCODING_INVALID",
                "Python Tool 源码必须使用 UTF-8 编码",
                status_code=422,
                field="file",
            ) from exc
        try:
            tree = ast.parse(source, filename=normalized_filename)
        except SyntaxError as exc:
            raise StudioError(
                "PYTHON_TOOL_SYNTAX_INVALID",
                f"Python 语法错误：第 {exc.lineno or 1} 行",
                status_code=422,
                field="file",
                details={"line": exc.lineno, "offset": exc.offset},
            ) from exc

        callables = [
            self._callable(node)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")
        ]
        if not callables:
            raise StudioError(
                "PYTHON_TOOL_CALLABLE_REQUIRED",
                "文件中没有可导入的顶层公开函数",
                status_code=422,
                field="file",
            )

        token = f"pyti_{uuid4().hex}"
        directory = self.root / token
        directory.mkdir(parents=True, exist_ok=False)
        digest = hashlib.sha256(content).hexdigest()
        try:
            self.workspace.atomic_write_text(directory / "tool.py", source)
            record = {
                "format": "agentkit.python-tool-inspection/v1",
                "inspectionToken": token,
                "filename": normalized_filename,
                "sha256": digest,
                "size": len(content),
                "callables": callables,
            }
            self.workspace.atomic_write_text(
                directory / "inspection.json",
                json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return dict(record)

    def load(self, token: str) -> dict[str, Any]:
        directory = self._directory(token)
        record_path = directory / "inspection.json"
        source_path = directory / "tool.py"
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            content = source_path.read_bytes()
        except (OSError, ValueError) as exc:
            raise StudioError(
                "PYTHON_TOOL_INSPECTION_NOT_FOUND",
                "Python Tool 检查不存在或已失效",
                status_code=404,
            ) from exc
        actual = hashlib.sha256(content).hexdigest()
        if actual != str(record.get("sha256") or ""):
            raise StudioError(
                "PYTHON_TOOL_INSPECTION_CHANGED",
                "Python Tool 暂存内容在确认前发生变化，请重新检查",
                status_code=409,
            )
        record["sourcePath"] = self.workspace.relative(source_path)
        return record

    def consume(self, token: str) -> None:
        shutil.rmtree(self._directory(token), ignore_errors=True)

    def _directory(self, token: str) -> Path:
        if not _TOKEN.fullmatch(str(token or "")):
            raise StudioError(
                "PYTHON_TOOL_INSPECTION_NOT_FOUND",
                "Python Tool 检查不存在或已失效",
                status_code=404,
            )
        directory = (self.root / token).resolve()
        try:
            directory.relative_to(self.root.resolve())
        except ValueError as exc:
            raise StudioError(
                "PYTHON_TOOL_INSPECTION_NOT_FOUND",
                "Python Tool 检查不存在或已失效",
                status_code=404,
            ) from exc
        return directory

    @staticmethod
    def _callable(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
        positional = [*node.args.posonlyargs, *node.args.args]
        required_count = max(0, len(positional) - len(node.args.defaults))
        required = [argument.arg for argument in positional[:required_count]]
        required.extend(
            argument.arg
            for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults)
            if default is None
        )
        parameters = [argument.arg for argument in positional]
        if node.args.vararg is not None:
            parameters.append(f"*{node.args.vararg.arg}")
        parameters.extend(argument.arg for argument in node.args.kwonlyargs)
        if node.args.kwarg is not None:
            parameters.append(f"**{node.args.kwarg.arg}")
        return {
            "name": node.name,
            "async": isinstance(node, ast.AsyncFunctionDef),
            "description": ast.get_docstring(node) or "",
            "parameters": parameters,
            "required": required,
            "line": node.lineno,
        }


__all__ = ["PythonToolInspector"]
