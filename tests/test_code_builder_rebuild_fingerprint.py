import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

from ksadk.builders.code_builder import CodeBuilder


class _FakeType:
    value = "langgraph"
    name = "LANGGRAPH"


class _FakeFrameworkDetector:
    def __init__(self, *_args, **_kwargs):
        pass

    def detect(self):
        return SimpleNamespace(
            type=_FakeType(),
            name="demo-agent",
            entry_point="agent.py",
            package_path="agent.py",
            agent_variable="agent",
        )


def _fake_package_zip(zip_path: Path, _detection_result):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("agent.py", "print('ok')\n")


def test_code_builder_skips_rebuild_when_only_mtime_changes(tmp_path: Path, monkeypatch):
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "agentengine.yaml").write_text("name: demo-agent\nframework: langgraph\n", encoding="utf-8")

    package_calls = []

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", _FakeFrameworkDetector)
    monkeypatch.setattr(CodeBuilder, "_install_dependencies", lambda self, _req: True)
    monkeypatch.setattr(
        CodeBuilder,
        "_package_zip",
        lambda self, zip_path, detection_result: (package_calls.append(zip_path), _fake_package_zip(zip_path, detection_result)),
    )

    builder = CodeBuilder(tmp_path)
    first = builder.build()
    assert first.success is True
    assert len(package_calls) == 1

    time.sleep(0.01)
    agent_file = tmp_path / "agent.py"
    original_content = agent_file.read_text(encoding="utf-8")
    agent_file.write_text(original_content, encoding="utf-8")

    second = builder.build()
    assert second.success is True
    assert len(package_calls) == 1


def test_code_builder_rebuilds_when_file_content_changes(tmp_path: Path, monkeypatch):
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "agentengine.yaml").write_text("name: demo-agent\nframework: langgraph\n", encoding="utf-8")

    package_calls = []

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", _FakeFrameworkDetector)
    monkeypatch.setattr(CodeBuilder, "_install_dependencies", lambda self, _req: True)
    monkeypatch.setattr(
        CodeBuilder,
        "_package_zip",
        lambda self, zip_path, detection_result: (package_calls.append(zip_path), _fake_package_zip(zip_path, detection_result)),
    )

    builder = CodeBuilder(tmp_path)
    first = builder.build()
    assert first.success is True
    assert len(package_calls) == 1

    agent_file = tmp_path / "agent.py"
    agent_file.write_text("print('changed')\n", encoding="utf-8")

    second = builder.build()
    assert second.success is True
    assert len(package_calls) == 2
