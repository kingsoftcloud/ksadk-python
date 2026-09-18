"""Read-only links to documents referenced by a run in its isolated tool workspace.

These are current workspace files, not immutable ArtifactStore versions. Never
resolve model-supplied absolute paths or traverse outside the owning agent root.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from urllib.parse import quote

from ksadk.studio.errors import StudioError, not_found

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_REFERENCE = re.compile(r"(?<![\w/])([\w\u4e00-\u9fff][\w\u4e00-\u9fff .()/\-]*\.md)\b", re.I)


def _run_studio(studio, run_id: str):
    """Pin document reads to the run owner, not the currently selected workspace."""
    resolve = getattr(studio, "runtime_for_run", None)
    if callable(resolve):
        owner = resolve(run_id)
        if owner is None:
            raise not_found("run", run_id)
        return owner
    return studio


def document_root(studio, run) -> Path:
    """Harness 内置文件工具在 Studio 工作区下的受控落盘根。

    ``assemble_python_tools`` 会将 builtin workspace 能力限定在
    ``.harness-tools/workspace``；文档链接必须与真实写入根一致，
    不能扫描整个代码库来猜测文件。
    """
    studio = _run_studio(studio, run.id)
    return studio.workspace.root / ".harness-tools" / "workspace"


def referenced_documents(studio, run) -> list[dict]:
    if run.runtime_type != "harness":
        return []
    root = document_root(studio, run)
    if root.resolve() != root:
        return []
    # Prefer quoted Markdown/code references; unquoted filenames are supported
    # too. Existence is verified before advertising a link.
    candidates = re.findall(r"`([^`\n]+\.md)`", run.output, re.I)
    candidates += _REFERENCE.findall(run.output)
    result = []
    seen = set()
    for name in candidates:
        name = name.strip()
        if name in seen or "\\" in name:
            continue
        relative = Path(name)
        if relative.is_absolute() or any(part in {"..", "."} for part in relative.parts):
            continue
        path = root / relative
        # Reject symlinks even when they point back into this workspace.
        if any(
            (root / Path(*relative.parts[:i])).is_symlink()
            for i in range(1, len(relative.parts) + 1)
        ):
            continue
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            continue
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            continue
        seen.add(name)
        result.append(
            {
                "path": name,
                "name": relative.name,
                "href": (
                    f"/api/v1/runs/{quote(run.id, safe='')}/documents/content"
                    f"?path={quote(name, safe='')}"
                ),
            }
        )
    return result


def read_document(studio, run_id: str, name: str) -> tuple[Path, str]:
    studio = _run_studio(studio, run_id)
    run = studio.event_store.get(run_id)
    if not any(item["path"] == name for item in referenced_documents(studio, run)):
        raise not_found("document", name)
    path = document_root(studio, run) / name
    # Open every component relative to a held directory fd. A concurrently
    # changed symlink must not turn a document request into an arbitrary read.
    descriptors = []
    try:
        current = os.open(studio.workspace.root, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(current)
        parts = path.relative_to(studio.workspace.root).parts
        for part in parts[:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        with os.fdopen(file_fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise not_found("document", name)
            content = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(content) > MAX_DOCUMENT_BYTES:
            raise StudioError("DOCUMENT_TOO_LARGE", "文档超过预览上限", status_code=413)
        return path, content.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise not_found("document", name) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def link_run_documents(studio, run, text: str) -> str:
    for item in referenced_documents(studio, run):
        label = item["name"].replace("[", "\\[").replace("]", "\\]")
        link = f"[{label}]({item['href']})"
        quoted = f"`{item['path']}`"
        if quoted in text:
            text = text.replace(quoted, link)
        elif item["href"] not in text:
            text += f"\n\n打开文档：{link}"
    return text
