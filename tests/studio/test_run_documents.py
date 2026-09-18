from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import RunRecord, RunStatus
from ksadk.studio.run_documents import (
    MAX_DOCUMENT_BYTES,
    document_root,
    link_run_documents,
    read_document,
    referenced_documents,
)
from ksadk.studio.service import StudioService
from ksadk.studio.shared_web import StudioSharedWebBridge


def setup_run(tmp_path: Path, *, agent_id="agent-a", run_id="run-a"):
    studio = StudioService(tmp_path)
    run = RunRecord(
        id=run_id,
        build_id="build-a",
        agent_id=agent_id,
        session_id="session-a",
        trace_id="trace-a",
        input="生成报告",
        output="已保存：`对比报告.md`。",
        runtime_type="harness",
        status=RunStatus.COMPLETED,
    )
    studio.event_store.create(run)
    root = document_root(studio, run)
    root.mkdir(parents=True, exist_ok=True)
    (root / "对比报告.md").write_text(
        "# 对比报告\n\n[官方来源](https://example.com/docs)", encoding="utf-8"
    )
    return studio, run, root


def test_links_only_reference_existing_documents_in_owner_workspace(tmp_path):
    studio, run, root = setup_run(tmp_path)
    assert root == tmp_path / ".harness-tools" / "workspace"
    links = referenced_documents(studio, run)
    assert len(links) == 1
    assert links[0]["path"] == "对比报告.md"
    assert "[对比报告.md](/api/v1/runs/run-a/documents/content?path=" in link_run_documents(
        studio, run, run.output
    )
    (root / "对比报告.md").unlink()
    assert referenced_documents(studio, run) == []


def test_workspace_switch_keeps_document_bound_to_original_run_owner(tmp_path):
    original, record, root = setup_run(tmp_path / "original", run_id="run-original")
    active, _, other_root = setup_run(tmp_path / "active", run_id="run-active")
    (other_root / "对比报告.md").write_text("wrong workspace", encoding="utf-8")
    manager = SimpleNamespace(
        workspace=active.workspace,
        event_store=active.event_store,
        runtime_for_run=lambda run_id: original if run_id == record.id else None,
    )
    path, content = read_document(manager, record.id, "对比报告.md")
    assert path == root / "对比报告.md"
    assert "[官方来源]" in content
    assert "wrong workspace" not in content
    assert referenced_documents(manager, record)[0]["href"].startswith(
        "/api/v1/runs/run-original/documents/content?"
    )
    from ksadk.studio.errors import StudioError

    with pytest.raises(StudioError):
        read_document(manager, "missing-run", "对比报告.md")


def test_preview_download_and_missing_file(tmp_path):
    studio, run, _ = setup_run(tmp_path)
    href = referenced_documents(studio, run)[0]["href"]
    with TestClient(create_studio_app(tmp_path, service=studio, security_enabled=False)) as client:
        preview = client.get(href + "&preview=true")
        assert preview.status_code == 200
        assert preview.json()["name"] == "对比报告.md"
        assert "[官方来源]" in preview.json()["content"]
        download = client.get(href)
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["x-content-type-options"] == "nosniff"
        assert (
            client.get(
                "/api/v1/runs/run-a/documents/content", params={"path": "missing.md"}
            ).status_code
            == 404
        )


@pytest.mark.parametrize(
    "name", ["../outside.md", "/tmp/outside.md", "link.md", "private.md", "huge.md"]
)
def test_rejects_traversal_symlinks_unreferenced_and_oversize(tmp_path, name):
    studio, run, root = setup_run(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    (root / "link.md").symlink_to(outside)
    (root / "private.md").write_text("not referenced")
    (root / "huge.md").write_text("x" * (MAX_DOCUMENT_BYTES + 1))
    if name != "private.md":
        run.output += f" `{name}`"
        studio.event_store.save(run)
    with TestClient(create_studio_app(tmp_path, service=studio, security_enabled=False)) as client:
        assert (
            client.get("/api/v1/runs/run-a/documents/content", params={"path": name}).status_code
            == 404
        )


def test_requires_studio_session(tmp_path):
    studio, run, _ = setup_run(tmp_path)
    href = referenced_documents(studio, run)[0]["href"]
    with TestClient(
        create_studio_app(tmp_path, service=studio, session_token="test-session-token")
    ) as client:
        assert client.get(href).status_code in {401, 403}


def test_progress_projection_and_linked_final_do_not_mutate_trace(tmp_path):
    studio, run, _ = setup_run(tmp_path)
    bridge = StudioSharedWebBridge(studio)
    event = {
        "runtimeEvent": {
            "run_id": "native-run",
            "item_kind": "message",
            "source": {"metadata": {"native_event_type": "run.progress"}},
        }
    }
    assert (
        bridge._presentation_event_content(event, run)["runtimeEvent"]["item_kind"] == "reasoning"
    )
    assert event["runtimeEvent"]["item_kind"] == "message"
    assert (
        "[对比报告.md]"
        in bridge._response_payload(run, model="test")["output"][0]["content"][0]["text"]
    )
