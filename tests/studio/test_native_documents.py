from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from ksadk.studio import native_documents
from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import RunRecord, RunStatus
from ksadk.studio.errors import StudioError
from ksadk.studio.native_documents import (
    NativeDocumentActionRequest,
    document_metadata,
    is_local_desktop_request,
    perform_document_action,
)
from ksadk.studio.run_documents import document_root
from ksadk.studio.service import StudioService


@pytest.fixture
def document(tmp_path, monkeypatch):
    run = RunRecord(
        id="native-run",
        build_id="build-a",
        agent_id="agent-a",
        session_id="session-a",
        trace_id="trace-a",
        input="生成文档",
        output="已保存：`报告.md`。",
        runtime_type="harness",
        status=RunStatus.COMPLETED,
    )
    studio = SimpleNamespace(
        workspace=SimpleNamespace(root=tmp_path), event_store=SimpleNamespace(get=lambda _: run)
    )
    root = document_root(studio, run)
    root.mkdir(parents=True)
    (root / "报告.md").write_text("# 报告\n\n测试内容", encoding="utf-8")
    app_root = tmp_path / "Applications"
    (app_root / "Visual Studio Code.app").mkdir(parents=True)
    (app_root / "TextEdit.app").mkdir()
    (app_root / "Untrusted App.app").mkdir()
    monkeypatch.setattr(native_documents.sys, "platform", "darwin")
    monkeypatch.setattr(native_documents, "_desktop_launcher", lambda: "/usr/bin/open")
    monkeypatch.setattr(native_documents, "_application_roots", lambda: (app_root,))
    launcher = Mock()
    monkeypatch.setattr(native_documents, "_launch", launcher)
    return studio, run, root, app_root, launcher


def test_metadata_advertises_only_installed_allowlisted_apps(document):
    studio, run, root, _, launcher = document
    metadata = document_metadata(studio, run.id, "报告.md", local=True)
    assert metadata["path"] == str(root / "报告.md")
    assert metadata["relativePath"] == "报告.md"
    assert metadata["revealLabel"] == "在 Finder 中显示"
    assert metadata["capabilities"] == {"open": True, "reveal": True, "openWith": True}
    assert metadata["applications"] == [
        {"id": "vscode", "name": "Visual Studio Code"},
        {"id": "textedit", "name": "TextEdit"},
    ]
    launcher.assert_not_called()


@pytest.mark.parametrize(
    "action,app_id,expected",
    [("open", None, []), ("reveal", None, ["-R"]), ("open", "vscode", ["-a"])],
)
def test_native_actions_use_verified_original_file(document, action, app_id, expected):
    studio, run, root, app_root, launcher = document
    payload = NativeDocumentActionRequest(path="报告.md", action=action, applicationId=app_id)
    response = perform_document_action(studio, run.id, payload, local=True)
    args = ["/usr/bin/open", *expected]
    if app_id:
        args.append(str(app_root / "Visual Studio Code.app"))
    launcher.assert_called_once_with([*args, str(root / "报告.md")])
    assert response["status"] == ("opened" if action == "open" else "revealed")


def test_filename_is_one_argument_not_a_shell_command(document):
    studio, run, root, _, launcher = document
    name = "报告 ; $(touch never).md"
    run.output = f"`{name}`"
    (root / name).write_text("# 内容", encoding="utf-8")
    perform_document_action(
        studio, run.id, NativeDocumentActionRequest(path=name, action="open"), local=True
    )
    launcher.assert_called_once_with(["/usr/bin/open", str(root / name)])


@pytest.mark.parametrize("name", ["../outside.md", "/tmp/outside.md", "private.md", "linked.md"])
def test_native_actions_cannot_open_unverified_files(document, name):
    studio, run, root, _, launcher = document
    (root / "private.md").write_text("private")
    (root / "linked.md").symlink_to(root / "private.md")
    if name != "private.md":
        run.output += f" `{name}`"
    with pytest.raises(StudioError) as raised:
        perform_document_action(
            studio, run.id, NativeDocumentActionRequest(path=name, action="open"), local=True
        )
    assert raised.value.status_code == 404
    launcher.assert_not_called()


@pytest.mark.parametrize(
    "action,app_id", [("open", "/bin/sh"), ("open", "typora"), ("reveal", "vscode")]
)
def test_rejects_arbitrary_unavailable_or_inapplicable_application(document, action, app_id):
    studio, run, _, _, launcher = document
    with pytest.raises(StudioError) as raised:
        perform_document_action(
            studio,
            run.id,
            NativeDocumentActionRequest(path="报告.md", action=action, applicationId=app_id),
            local=True,
        )
    assert raised.value.code == "DOCUMENT_APPLICATION_UNAVAILABLE"
    launcher.assert_not_called()


def test_remote_clients_cannot_see_absolute_paths_or_launch(document):
    studio, run, _, _, launcher = document
    metadata = document_metadata(studio, run.id, "报告.md", local=False)
    assert metadata["path"] is None
    assert not any(metadata["capabilities"].values())
    assert metadata["applications"] == []
    with pytest.raises(StudioError) as raised:
        perform_document_action(
            studio, run.id, NativeDocumentActionRequest(path="报告.md", action="open"), local=False
        )
    assert raised.value.code == "NATIVE_DOCUMENT_LOCAL_ONLY"
    launcher.assert_not_called()


def test_headless_environment_has_download_fallback(document, monkeypatch):
    studio, run, _, _, launcher = document
    monkeypatch.setattr(native_documents, "_desktop_launcher", lambda: None)
    metadata = document_metadata(studio, run.id, "报告.md", local=True)
    assert not any(metadata["capabilities"].values())
    with pytest.raises(StudioError) as raised:
        perform_document_action(
            studio, run.id, NativeDocumentActionRequest(path="报告.md", action="open"), local=True
        )
    assert raised.value.code == "NATIVE_DOCUMENT_UNAVAILABLE"
    launcher.assert_not_called()


def test_launcher_failure_is_not_reported_as_success_or_echoed(document):
    studio, run, _, _, launcher = document
    launcher.side_effect = OSError("sensitive local details")
    with pytest.raises(StudioError) as raised:
        perform_document_action(
            studio, run.id, NativeDocumentActionRequest(path="报告.md", action="open"), local=True
        )
    assert raised.value.code == "DOCUMENT_OPEN_FAILED"
    assert "sensitive" not in str(raised.value)


@pytest.mark.parametrize("action", ["open", "reveal"])
def test_linux_desktop_opens_document_or_owning_folder(document, monkeypatch, action):
    studio, run, root, _, launcher = document
    monkeypatch.setattr(native_documents.sys, "platform", "linux")
    monkeypatch.setattr(native_documents, "_desktop_launcher", lambda: "/usr/bin/xdg-open")
    perform_document_action(
        studio, run.id, NativeDocumentActionRequest(path="报告.md", action=action), local=True
    )
    target = root / "报告.md" if action == "open" else root
    launcher.assert_called_once_with(["/usr/bin/xdg-open", str(target)])


def test_windows_native_support_is_not_advertised_without_secure_reader(monkeypatch):
    monkeypatch.setattr(native_documents.sys, "platform", "win32")
    assert native_documents._desktop_launcher() is None


def test_linux_detection_requires_desktop_environment(monkeypatch):
    monkeypatch.setattr(native_documents.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(native_documents.shutil, "which", lambda _: "/usr/bin/xdg-open")
    assert native_documents._desktop_launcher() is None
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert native_documents._desktop_launcher() == "/usr/bin/xdg-open"


def test_process_launcher_is_bounded_without_shell_or_private_stderr(monkeypatch):
    launch = Mock()
    monkeypatch.setattr(native_documents.subprocess, "run", launch)
    native_documents._launch(["/usr/bin/open", "/workspace/report.md"])
    args, kwargs = launch.call_args
    assert args == (["/usr/bin/open", "/workspace/report.md"],)
    assert kwargs.get("shell", False) is False
    assert kwargs["timeout"] == 10
    assert kwargs["check"] is True
    assert kwargs["stderr"] == native_documents.subprocess.DEVNULL


@pytest.mark.parametrize(
    "client,host,headers,expected",
    [
        ("127.0.0.1", "127.0.0.1:8085", [], True),
        ("::1", "localhost:8085", [("origin", "http://localhost:8085")], True),
        ("192.0.2.10", "localhost:8085", [], False),
        ("127.0.0.1", "remote.example:8085", [], False),
        ("127.0.0.1", "localhost:8085", [("x-forwarded-for", "192.0.2.10")], False),
        ("127.0.0.1", "localhost:8085", [("forwarded", "for=192.0.2.10")], False),
        ("127.0.0.1", "localhost:8085", [("origin", "http://localhost:9090")], False),
    ],
)
def test_local_desktop_requires_peer_and_origin_not_just_host(client, host, headers, expected):
    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "path": "/api/v1/runs/native-run/documents/actions",
            "headers": [(b"host", host.encode()), *((k.encode(), v.encode()) for k, v in headers)],
            "client": (client, 55000),
        }
    )
    assert is_local_desktop_request(request) is expected


def test_api_native_actions_require_session_csrf_and_valid_body(document):
    stub, run, _, _, launcher = document
    studio = StudioService(stub.workspace.root)
    studio.event_store.create(run)
    app = create_studio_app(
        stub.workspace.root, service=studio, session_token="test-session", csrf_token="test-csrf"
    )
    # Do not start unrelated model/cloud/scheduler services for a file-action route test.
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 55000))
    url = f"/api/v1/runs/{run.id}/documents/actions"
    data = {"path": "报告.md", "action": "open"}
    try:
        assert client.post(url, json=data).status_code == 401
        client.headers["X-AgentKit-Session"] = "test-session"
        assert client.post(url, json=data).status_code == 403
        client.headers["X-CSRF-Token"] = "test-csrf"
        assert client.post(url, json={**data, "command": "sh"}).status_code == 422
        assert client.post(url, json={**data, "action": "execute"}).status_code == 422
        launcher.assert_not_called()
        result = client.post(url, json=data)
        assert result.status_code == 200
        assert result.json()["status"] == "opened"
        launcher.assert_called_once()
        metadata = client.get(
            f"/api/v1/runs/{run.id}/documents/metadata", params={"path": "报告.md"}
        )
        assert metadata.status_code == 200
        assert metadata.json()["capabilities"]["open"] is True
        client.headers["X-Forwarded-For"] = "192.0.2.10"
        remote_metadata = client.get(
            f"/api/v1/runs/{run.id}/documents/metadata", params={"path": "报告.md"}
        )
        assert remote_metadata.json()["path"] is None
        assert client.post(url, json=data).status_code == 403
        launcher.assert_called_once()
    finally:
        client.close()
