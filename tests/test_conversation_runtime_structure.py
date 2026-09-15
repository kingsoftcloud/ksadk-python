from __future__ import annotations

import ast
from pathlib import Path

import ksadk.conversations as conversations
from ksadk.conversations import runtime

CONVERSATIONS_DIR = Path(runtime.__file__).resolve().parent
RUNTIME_MODULES = sorted(CONVERSATIONS_DIR.glob("runtime*.py"))
RUNTIME_PUBLIC_EXPORTS = (
    "CompactionPlan",
    "PreparedConversationTurn",
    "append_context_checkpoint_event",
    "append_conversation_event",
    "append_reasoning_event",
    "append_run_checkpoint_event",
    "append_run_resume_event",
    "append_run_status_event",
    "build_chat_completions_payload",
    "build_compaction_sse_event",
    "build_responses_payload",
    "build_run_input",
    "compact_conversation_history",
    "ensure_conversation_session",
    "extract_responses_resume_input",
    "invoke_conversation_once",
    "preview_auto_compaction",
    "prime_session_metadata_for_user_turn",
    "stream_conversation_turn",
    "stream_responses_conversation_turn",
)


def _runtime_import_graph() -> dict[str, set[str]]:
    module_names = {path.stem for path in RUNTIME_MODULES}
    graph = {name: set() for name in module_names}
    for path in RUNTIME_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            imported = node.module.rsplit(".", 1)[-1]
            if imported in module_names:
                graph[path.stem].add(imported)
    return graph


def test_runtime_facade_and_modules_stay_within_structure_limits() -> None:
    line_counts = {
        path.name: len(path.read_text(encoding="utf-8").splitlines()) for path in RUNTIME_MODULES
    }

    assert line_counts["runtime.py"] <= 500
    assert all(count < 1000 for count in line_counts.values()), line_counts


def test_runtime_module_import_graph_is_acyclic() -> None:
    graph = _runtime_import_graph()
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, path: tuple[str, ...]) -> None:
        if module in visiting:
            raise AssertionError("runtime import cycle: " + " -> ".join((*path, module)))
        if module in visited:
            return
        visiting.add(module)
        for dependency in sorted(graph[module]):
            visit(dependency, (*path, module))
        visiting.remove(module)
        visited.add(module)

    for module in sorted(graph):
        visit(module, ())


def test_conversations_public_exports_remain_on_runtime_facade() -> None:
    for name in RUNTIME_PUBLIC_EXPORTS:
        assert name in conversations.__all__
        assert getattr(conversations, name) is getattr(runtime, name)
