"""Synchronize actual documents and return bounded, current diagnostic observations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import unquote, urlparse

from .contracts import DiagnosticDocument, LanguageServerProfile
from .lsp_rpc import LspConnection, LspFailure


class NativeLanguageDiagnostics:
    def __init__(self, *, root: Path, profile: LanguageServerProfile):
        self.root = root.resolve()
        self.profile = LanguageServerProfile.model_validate_json(profile.model_dump_json())
        self.connection: LspConnection | None = None
        self.capabilities: dict = {}
        self.documents: dict[str, tuple[int, str]] = {}
        self.published: dict[str, tuple[int | None, list[dict]]] = {}
        self.published_at: dict[str, float] = {}
        self.published_generation: dict[str, int] = {}
        self.project_generation = 0
        self.serial = asyncio.Lock()
        self.changed = asyncio.Event()
        self.pull = False
        self.pull_identifier: str | None = None
        self.closed = False

    def _path(self, uri: object) -> str | None:
        if not isinstance(uri, str):
            return None
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            return None
        target = Path(unquote(parsed.path))
        try:
            relative = target.relative_to(self.root)
            if ".." in relative.parts or target.resolve() != target or not target.is_file():
                return None
            return relative.as_posix()
        except (ValueError, OSError):
            return None

    def _notification(self, method: str, params: dict) -> None:
        if method == "textDocument/publishDiagnostics":
            path = self._path(params.get("uri"))
            issues = params.get("diagnostics")
            if (
                path is not None
                and isinstance(issues, list)
                and (path in self.published or len(self.published) < 64)
            ):
                version = params.get("version")
                self.published[path] = (version if type(version) is int else None, issues[:100])
                self.published_at[path] = asyncio.get_running_loop().time()
                self.published_generation[path] = self.project_generation
                self.changed.set()
        elif method == "client/registerCapability":
            for item in params.get("registrations", []):
                if item.get("method") == "textDocument/diagnostic":
                    self.pull = True
                    self.pull_identifier = item.get("registerOptions", {}).get("identifier")
                    self.changed.set()
        elif method == "host/connectionLost":
            self.changed.set()

    async def _connect(self) -> LspConnection:
        if self.closed:
            raise LspFailure("language_server_closed")
        if self.connection is None:
            self.connection = LspConnection(self.profile, self.root, self._notification)
            self.capabilities = await self.connection.start()
            self.pull = self.capabilities.get("diagnosticProvider") not in (None, False)
            options = self.capabilities.get("diagnosticProvider")
            self.pull_identifier = options.get("identifier") if isinstance(options, dict) else None
        return self.connection

    async def _sync(self, document: DiagnosticDocument, connection: LspConnection) -> int:
        uri = (self.root / document.path).as_uri()
        previous = self.documents.get(document.path)
        if previous is not None and previous[1] == document.text:
            # OpenCode retains push observations for no-op touches. Reopening
            # instead emits a didClose clearing notification which can arrive
            # before the server has produced the next semantic batch.
            return previous[0]
        version = previous[0] + 1 if previous else 1
        self.project_generation += 1
        self.changed.clear()
        await connection.send(
            "workspace/didChangeWatchedFiles",
            {
                "changes": [{"uri": uri, "type": 2 if previous else 1}],
            },
        )
        if previous is None:
            await connection.send(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "version": version,
                        "languageId": self.profile.languages[Path(document.path).suffix],
                        "text": document.text,
                    }
                },
            )
        else:
            sync = self.capabilities.get("textDocumentSync", 1)
            kind = sync.get("change", 1) if isinstance(sync, dict) else sync
            change: dict = {"text": document.text}
            if kind == 2:
                lines = previous[1].split("\n")
                change["range"] = {
                    "start": {"line": 0, "character": 0},
                    "end": {
                        "line": len(lines) - 1,
                        "character": len(lines[-1].encode("utf-16-le")) // 2,
                    },
                }
            await connection.send(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [change],
                },
            )
        self.documents[document.path] = (version, document.text)
        await connection.send("textDocument/didSave", {"textDocument": {"uri": uri}})
        return version

    async def _issues(self, document: DiagnosticDocument, version: int, connection: LspConnection):
        while True:
            if self.pull:
                report = await connection.request(
                    "textDocument/diagnostic",
                    {
                        "textDocument": {"uri": (self.root / document.path).as_uri()},
                        **({"identifier": self.pull_identifier} if self.pull_identifier else {}),
                    },
                )
                if isinstance(report, dict) and isinstance(report.get("items"), list):
                    related = {}
                    for uri, other in report.get("relatedDocuments", {}).items():
                        path = self._path(uri)
                        if (
                            path
                            and isinstance(other, dict)
                            and isinstance(other.get("items"), list)
                        ):
                            related[path] = other["items"][:100]
                    return report["items"][:100], related, "pull"
                raise LspFailure("language_server_report_unavailable")
            current = self.published.get(document.path)
            if (
                current is not None
                and current[0] in {None, version}
                and self.published_generation.get(document.path) == self.project_generation
            ):
                # OpenCode waits 150ms after the most recent matching push.
                # TypeScript can publish an empty syntax batch before semantic
                # errors; the first notification is not a complete observation.
                settle = self.published_at[document.path] + 0.150
                remaining = settle - asyncio.get_running_loop().time()
                if remaining > 0:
                    self.changed.clear()
                    try:
                        await asyncio.wait_for(self.changed.wait(), remaining)
                    except TimeoutError:
                        pass
                    continue
                related = {
                    p: items
                    for p, (_, items) in self.published.items()
                    if p != document.path
                    and self.published_generation.get(p) == self.project_generation
                }
                return current[1], related, "push"
            self.changed.clear()
            await self.changed.wait()
            if (
                self.connection is None
                or self.connection.failed
                or self.connection.process.returncode is not None
            ):
                raise LspFailure()

    async def observe(
        self,
        documents: tuple[DiagnosticDocument, ...],
        *,
        timeout_seconds: float = 20.0,
        synchronization_documents: tuple[DiagnosticDocument, ...] = (),
        invalidate_project: bool = False,
    ) -> list[dict]:
        async with self.serial:
            results = []
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            if invalidate_project:
                await self._reset()
            if synchronization_documents:
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise LspFailure("language_server_budget_exhausted")
                    async with asyncio.timeout(min(self.profile.timeout_seconds, remaining)):
                        connection = await self._connect()
                        for document in synchronization_documents:
                            await self._sync(document, connection)
                except asyncio.CancelledError:
                    await self._reset()
                    raise
                except Exception as exc:
                    await self._reset()
                    code = (
                        "language_server_timeout"
                        if isinstance(exc, TimeoutError)
                        else exc.code
                        if isinstance(exc, LspFailure)
                        else "language_server_unavailable"
                    )
                    return [
                        {
                            "path": document.path,
                            "content_digest": document.content_digest,
                            "status": "not_run",
                            "code": code,
                        }
                        for document in documents
                    ]
            for document in documents:
                base = {"path": document.path, "content_digest": document.content_digest}
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise LspFailure("language_server_budget_exhausted")
                    async with asyncio.timeout(min(self.profile.timeout_seconds, remaining)):
                        connection = await self._connect()
                        version = await self._sync(document, connection)
                        issues, related, delivery = await self._issues(
                            document, version, connection
                        )
                    results.append(
                        {
                            **base,
                            "status": "observed",
                            "document_version": version,
                            "project_generation": self.project_generation,
                            "delivery": delivery,
                            "issues": issues,
                            "related": related,
                        }
                    )
                except asyncio.CancelledError:
                    await self._reset()
                    raise
                except Exception as exc:
                    await self._reset()
                    code = (
                        "language_server_timeout"
                        if isinstance(exc, TimeoutError)
                        else exc.code
                        if isinstance(exc, LspFailure)
                        else "language_server_unavailable"
                    )
                    results.append({**base, "status": "not_run", "code": code})
            return results

    async def invalidate(self):
        """Discard stale overlays when the host cannot reconcile project inputs."""
        async with self.serial:
            await self._reset()

    async def _reset(self):
        if self.connection is not None:
            await self.connection.close()
        self.connection = None
        self.documents.clear()
        self.published.clear()
        self.published_at.clear()
        self.published_generation.clear()
        self.project_generation = 0

    async def close(self):
        self.closed = True
        await self._reset()
