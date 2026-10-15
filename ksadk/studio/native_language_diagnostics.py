"""Activation-scoped language services for authorized builtin workspace writes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path, PurePosixPath

from ksadk.harness.diagnostic_adapters.contracts import DiagnosticDocument, LanguageServerProfile
from ksadk.harness.diagnostic_adapters.native_lsp import NativeLanguageDiagnostics
from ksadk.harness.native.adapters import StrictToolExecutor, ToolBinding
from ksadk.harness.native.model import ModelTextContent
from ksadk.harness.native.ports import ToolDefinition, ToolResult
from ksadk.harness.workspace_adapters.native_directory import DirectoryWorkspacePort
from ksadk.studio.native_workspace_diagnostics import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    _read_file,
    feedback_profile_digest,
)


def _issues(values: list[dict]) -> list[dict]:
    result = []
    for value in values[:100]:
        if not isinstance(value, dict) or value.get("severity", 1) not in {1, 2}:
            continue
        start = value.get("range", {}).get("start", {})
        line, column, message = start.get("line"), start.get("character"), value.get("message")
        if type(line) is not int or line < 0 or type(column) is not int or column < 0:
            continue
        if not isinstance(message, str) or not message:
            continue
        code = value.get("code")
        result.append(
            {
                "severity": "error" if value.get("severity", 1) == 1 else "warning",
                "line": line + 1,
                "column": column + 1,
                "message": message[:2000],
                **({"code": str(code)[:128]} if type(code) in {str, int} else {}),
            }
        )
        if len(result) == 20:
            break
    return result


class StudioNativeLanguageDiagnostics:
    def __init__(self, *, root: Path, profiles: tuple[LanguageServerProfile, ...]):
        self.root = root.resolve()
        self.profiles = tuple(
            LanguageServerProfile.model_validate_json(p.model_dump_json()) for p in profiles
        )
        self.clients = tuple(
            NativeLanguageDiagnostics(root=self.root, profile=p) for p in self.profiles
        )
        self.serial = asyncio.Lock()
        self.workspace = DirectoryWorkspacePort(self.root)
        self.inventories = {}
        payload = {
            "version": "workspace-language-diagnostics/v1",
            "baseline": feedback_profile_digest(),
            "push_debounce_ms": 150,
            "workspace_sync": {
                "provider": "directory-workspace/v1",
                "max_entries": self.workspace.max_entries,
                "max_hashed_bytes": self.workspace.max_hashed_bytes,
                "changed_sources": "synchronize_before_query",
                "other_changes": "invalidate_overlay",
                "unavailable": "not_run",
            },
            "profiles": [p.model_dump(mode="json") for p in self.profiles],
            "limits": {
                "files": MAX_FILES,
                "file_bytes": MAX_FILE_BYTES,
                "total_bytes": MAX_TOTAL_BYTES,
                "issues": 20,
                "related_files": 5,
            },
        }
        self.profile_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            ).hexdigest()
        )

    def tool_surface(self, *, scope, authorize):
        definition = ToolDefinition(
            name="workspace_diagnostics",
            description=(
                "Observe configured language-server diagnostics for existing workspace files. "
                "Use relative paths to inspect types and related-file errors after changes. "
                "Diagnostics do not run the program or establish task acceptance. "
                "An empty issue list is only this provider's current observation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_FILES,
                        "items": {"type": "string", "minLength": 1, "maxLength": 512},
                    }
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
            read_only=True,
            progress_observation=True,
            feedback_profile_digest=self.profile_digest,
        )

        async def invoke(request):
            block = await self.observe(root=self.root, paths=tuple(request.call.arguments["paths"]))
            return ToolResult(status="ok", content=json.loads(block.text))

        return definition, StrictToolExecutor(
            scope=scope,
            tools=(ToolBinding(definition, invoke),),
            authorize=authorize,
        )

    def _document(self, path: str, remaining: int) -> DiagnosticDocument | None:
        relative = PurePosixPath(path)
        if (
            not path
            or len(path) > 512
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in path
            or "\x00" in path
        ):
            return None
        try:
            data = _read_file(self.root, relative, min(remaining, MAX_FILE_BYTES))
            if data is None:
                return None
            return DiagnosticDocument(
                path=path,
                text=data.decode("utf-8"),
                content_digest="sha256:" + hashlib.sha256(data).hexdigest(),
            )
        except (OSError, ValueError, RuntimeError):
            return None

    async def observe(self, *, root: Path, paths: tuple[str, ...]) -> ModelTextContent:
        async with self.serial:
            return await self._observe(root=root, paths=paths)

    def _synchronization(self, profile, previous, entries):
        if previous is None:
            return (), False
        current = {entry.path: entry for entry in entries}
        changed = [
            path
            for path in sorted(current.keys() | previous.keys())
            if current.get(path) != previous.get(path)
        ]
        documents, remaining = [], MAX_TOTAL_BYTES
        for path in changed:
            entry = current.get(path)
            if (
                entry is None
                or entry.kind != "file"
                or Path(path).suffix not in profile.languages
                or len(documents) == MAX_FILES
            ):
                # Deleted documents and project/configuration changes can leave
                # stale overlays or settings; start from current disk instead.
                return (), True
            document = self._document(path, remaining)
            if document is None or document.content_digest != entry.content_digest:
                return (), True
            remaining -= len(document.text.encode())
            documents.append(document)
        return tuple(documents), False

    async def _observe(self, *, root: Path, paths: tuple[str, ...]) -> ModelTextContent:
        reports = []
        deadline = asyncio.get_running_loop().time() + 20.0
        same_root = root.resolve() == self.root
        captured, entries = (
            await self.workspace.capture_inventory(operation_key="language-diagnostics")
            if same_root
            else (None, ())
        )
        usable = captured is not None and captured.status == "captured"
        for index, (profile, client) in enumerate(zip(self.profiles, self.clients, strict=True)):
            documents, missing, remaining = [], [], MAX_TOTAL_BYTES
            for path in paths[:MAX_FILES]:
                if Path(path).suffix not in profile.languages:
                    continue
                document = self._document(path, remaining) if same_root else None
                if document is None:
                    missing.append(
                        {
                            "path": path,
                            "status": "not_run",
                            "code": "diagnostic_document_unavailable",
                        }
                    )
                else:
                    remaining -= len(document.text.encode())
                    documents.append(document)
            if usable:
                synchronization, invalidate = self._synchronization(
                    profile, self.inventories.get(index), entries
                )
                observations = await client.observe(
                    tuple(documents),
                    synchronization_documents=synchronization,
                    invalidate_project=invalidate,
                    timeout_seconds=max(0, deadline - asyncio.get_running_loop().time()),
                )
            else:
                if same_root:
                    await client.invalidate()
                    self.inventories.pop(index, None)
                observations = [
                    {
                        "path": document.path,
                        "status": "not_run",
                        "code": "diagnostic_workspace_unavailable",
                    }
                    for document in documents
                ]
            files = []
            for observation in observations:
                if observation["status"] != "observed":
                    files.append(observation)
                    continue
                current = self._document(observation["path"], MAX_FILE_BYTES)
                if current is None or current.content_digest != observation["content_digest"]:
                    files.append(
                        {
                            "path": observation["path"],
                            "status": "not_run",
                            "code": "diagnostic_document_changed",
                        }
                    )
                    continue
                related = []
                for path, values in observation.pop("related", {}).items():
                    issues = _issues(values)
                    document = self._document(path, remaining) if issues else None
                    if document is not None:
                        remaining -= len(document.text.encode())
                        related.append(
                            {
                                "path": path,
                                "content_digest": document.content_digest,
                                "status": "observed",
                                "issues": issues,
                            }
                        )
                    if len(related) == 5:
                        break
                if "issues" in observation:
                    observation["issues"] = _issues(observation["issues"])
                files.append({**observation, "related_files": related})
            reports.append(
                {
                    "server_id": profile.server_id,
                    "profile_digest": profile.digest,
                    "files": files + missing,
                }
            )
        workspace = {
            "status": "unavailable",
            "reason": captured.reason if captured is not None else "workspace_root_mismatch",
        }
        if usable:
            after, _entries = await self.workspace.capture_inventory(
                operation_key="language-diagnostics-after"
            )
            if after.status == "captured" and after.revision == captured.revision:
                workspace = {"status": "captured", "revision": captured.revision}
                for index in range(len(self.clients)):
                    self.inventories[index] = {entry.path: entry for entry in entries}
            else:
                workspace = {"status": "unavailable", "reason": "workspace_changed_during_query"}
                for report, client in zip(reports, self.clients, strict=True):
                    report["files"] = [
                        {
                            "path": observation["path"],
                            "status": "not_run",
                            "code": "diagnostic_workspace_changed",
                        }
                        if observation["status"] == "observed"
                        else observation
                        for observation in report["files"]
                    ]
                    await client.invalidate()
                self.inventories.clear()
        value = {
            "version": "workspace-language-diagnostics/v1",
            "scope": "configured_language_servers",
            "acceptance": "not_evaluated",
            "profile_digest": self.profile_digest,
            "workspace": workspace,
            "providers": reports,
            "unsupported_files": [
                {"path": path, "status": "not_run", "code": "unsupported_file_type"}
                for path in paths[:MAX_FILES]
                if not any(Path(path).suffix in profile.languages for profile in self.profiles)
            ],
            "omitted_file_count": max(0, len(paths) - MAX_FILES),
        }
        return ModelTextContent(text=json.dumps(value, ensure_ascii=False))

    async def close(self):
        async with self.serial:
            for client in self.clients:
                await client.close()
            self.inventories.clear()
