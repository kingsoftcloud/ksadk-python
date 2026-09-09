import hashlib
import io
import stat
import zipfile
from types import SimpleNamespace

import pytest

from ksadk.sandbox.backends.e2b import E2BSandboxSession
from ksadk.sandbox.backends.local_process import LocalProcessSandboxSession
from ksadk.skills.runtime import artifact_delivery as delivery


def test_binary_artifact_survives_source_removal(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    original = root / "binary.bin"
    data = bytes(range(256))
    original.write_bytes(data)
    bundle = tmp_path / "artifacts.zip"
    receipt = delivery.export_artifacts([str(original)], root, bundle)
    original.unlink()
    output = delivery.import_artifacts(bundle.read_bytes(), receipt, parent=tmp_path)
    from pathlib import Path

    assert Path(output[0]).read_bytes() == data
    assert stat.S_IMODE(Path(output[0]).stat().st_mode) == 0o600


@pytest.mark.parametrize("case", ["outside", "link", "parent-link"])
def test_export_rejects_outside_files_and_links(tmp_path, case):
    root = tmp_path / "source"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    if case == "outside":
        path = outside
    elif case == "link":
        path = root / "link"
        path.symlink_to(outside)
    else:
        (root / "linked").symlink_to(tmp_path)
        path = root / "linked" / "outside.txt"
    bundle = tmp_path / "artifacts.zip"
    with pytest.raises(delivery.ArtifactDeliveryError):
        delivery.export_artifacts([str(path)], root, bundle)
    assert not bundle.exists()


def zip_receipt(name, *, link=False):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        info = zipfile.ZipInfo(name)
        if link:
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "content")
    content = stream.getvalue()
    return content, delivery.ArtifactBundle(
        sha256=hashlib.sha256(content).hexdigest(), size=len(content), file_count=1
    )


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/drive", "nested\\path"])
def test_import_rejects_paths_and_removes_partial_directory(tmp_path, name):
    content, receipt = zip_receipt(name)
    with pytest.raises(delivery.ArtifactDeliveryError):
        delivery.import_artifacts(content, receipt, parent=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_import_rejects_link_entry(tmp_path):
    content, receipt = zip_receipt("link", link=True)
    with pytest.raises(delivery.ArtifactDeliveryError):
        delivery.import_artifacts(content, receipt, parent=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_import_requires_expected_digest(tmp_path):
    content, receipt = zip_receipt("file")
    with pytest.raises(delivery.ArtifactDeliveryError, match="integrity"):
        delivery.import_artifacts(content + b"modified", receipt, parent=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_export_does_not_remove_existing_destination(tmp_path):
    destination = tmp_path / "existing.zip"
    destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        delivery.export_artifacts([], tmp_path, destination)
    assert destination.read_bytes() == b"keep"


def test_artifact_size_limits_apply_at_both_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "MAX_FILE_BYTES", 3)
    source = tmp_path / "file"
    source.write_bytes(b"large")
    with pytest.raises(delivery.ArtifactDeliveryError):
        delivery.export_artifacts([str(source)], tmp_path, tmp_path / "out.zip")
    content, receipt = zip_receipt("file")
    with pytest.raises(delivery.ArtifactDeliveryError):
        delivery.import_artifacts(content, receipt, parent=tmp_path)
    assert not list(tmp_path.glob("ksadk-skill-artifacts-*"))


@pytest.mark.parametrize("limit", [2, 8])
def test_e2b_binary_stream_closes_on_success_and_limit(limit):
    events = []

    def chunks():
        try:
            yield b"\x00\xff"
            yield b"abc"
            events.append("completed")
        finally:
            events.append("closed")

    def read(path, **kwargs):
        assert kwargs == {"format": "stream", "request_timeout": 30}
        return chunks()

    session = E2BSandboxSession(SimpleNamespace(files=SimpleNamespace(read=read)))
    if limit == 2:
        with pytest.raises(ValueError, match="limit"):
            session.read_file_bytes("/fixture", max_bytes=limit)
        assert events == ["closed"]
    else:
        assert session.read_file_bytes("/fixture", max_bytes=limit) == b"\x00\xffabc"
        assert events == ["completed", "closed"]


def test_local_sandbox_binary_read_is_bounded(tmp_path):
    session = LocalProcessSandboxSession(session_id="fixture", workspace_root=tmp_path)
    session.write_file("binary", b"\x00\xffdata")
    assert session.read_file_bytes("binary", max_bytes=6) == b"\x00\xffdata"
    with pytest.raises(ValueError, match="limit"):
        session.read_file_bytes("binary", max_bytes=2)
