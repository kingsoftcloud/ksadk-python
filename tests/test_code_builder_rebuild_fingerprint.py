import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import ksadk

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


def test_code_builder_rebuilds_when_ksadk_source_changes(tmp_path: Path, monkeypatch):
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "agentengine.yaml").write_text("name: demo-agent\nframework: langgraph\n", encoding="utf-8")

    fake_ksadk_root = tmp_path.parent / f"{tmp_path.name}_fake_ksadk" / "ksadk"
    (fake_ksadk_root / "configs").mkdir(parents=True, exist_ok=True)
    (fake_ksadk_root / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
    settings_file = fake_ksadk_root / "configs" / "settings.py"
    settings_file.write_text("VALUE = 'v1'\n", encoding="utf-8")

    package_calls = []

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", _FakeFrameworkDetector)
    monkeypatch.setattr(CodeBuilder, "_install_dependencies", lambda self, _req: True)
    monkeypatch.setattr(
        CodeBuilder,
        "_package_zip",
        lambda self, zip_path, detection_result: (package_calls.append(zip_path), _fake_package_zip(zip_path, detection_result)),
    )
    monkeypatch.setattr(ksadk, "__file__", str(fake_ksadk_root / "__init__.py"))

    builder = CodeBuilder(tmp_path)
    first = builder.build()
    assert first.success is True
    assert len(package_calls) == 1

    settings_file.write_text("VALUE = 'v2'\n", encoding="utf-8")

    second = builder.build()
    assert second.success is True
    assert len(package_calls) == 2


def test_code_builder_no_cache_reinstalls_dependencies_when_requirements_unchanged(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "agentengine.yaml").write_text("name: demo-agent\nframework: langgraph\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("httpx==0.28.1\n", encoding="utf-8")

    install_calls = []
    package_calls = []

    def fake_install(self, _req):
        install_calls.append("install")
        self.deps_dir.mkdir(parents=True, exist_ok=True)
        (self.deps_dir / "httpx.py").write_text("# dep\n", encoding="utf-8")
        return True

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", _FakeFrameworkDetector)
    monkeypatch.setattr(CodeBuilder, "_install_dependencies", fake_install)
    monkeypatch.setattr(CodeBuilder, "_scan_incompatible_binaries_in_deps", lambda self: [])
    monkeypatch.setattr(
        CodeBuilder,
        "_package_zip",
        lambda self, zip_path, detection_result: (package_calls.append(zip_path), _fake_package_zip(zip_path, detection_result)),
    )

    builder = CodeBuilder(tmp_path, config={"no_cache": True})
    first = builder.build()
    second = builder.build()

    assert first.success is True
    assert second.success is True
    assert len(package_calls) == 2
    assert len(install_calls) == 2


def test_code_builder_no_cache_reinstalls_dependencies_when_requirements_change(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "agentengine.yaml").write_text("name: demo-agent\nframework: langgraph\n", encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("httpx==0.28.1\n", encoding="utf-8")

    install_calls = []

    def fake_install(self, _req):
        install_calls.append("install")
        self.deps_dir.mkdir(parents=True, exist_ok=True)
        marker = self.deps_dir / f"dep-{len(install_calls)}.txt"
        marker.write_text("ok\n", encoding="utf-8")
        return True

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", _FakeFrameworkDetector)
    monkeypatch.setattr(CodeBuilder, "_install_dependencies", fake_install)
    monkeypatch.setattr(CodeBuilder, "_scan_incompatible_binaries_in_deps", lambda self: [])
    monkeypatch.setattr(
        CodeBuilder,
        "_package_zip",
        lambda self, zip_path, detection_result: _fake_package_zip(zip_path, detection_result),
    )

    builder = CodeBuilder(tmp_path, config={"no_cache": True})
    first = builder.build()
    requirements.write_text("httpx==0.28.1\nrequests==2.32.3\n", encoding="utf-8")
    second = builder.build()

    assert first.success is True
    assert second.success is True
    assert len(install_calls) == 2


def test_code_builder_repackage_reuses_dependencies_but_rebuilds_zip(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "agentengine.yaml").write_text("name: demo-agent\nframework: langgraph\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("httpx==0.28.1\n", encoding="utf-8")

    install_calls = []
    package_calls = []

    def fake_install(self, _req):
        install_calls.append("install")
        self.deps_dir.mkdir(parents=True, exist_ok=True)
        (self.deps_dir / "httpx.py").write_text("# dep\n", encoding="utf-8")
        return True

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", _FakeFrameworkDetector)
    monkeypatch.setattr(CodeBuilder, "_install_dependencies", fake_install)
    monkeypatch.setattr(CodeBuilder, "_scan_incompatible_binaries_in_deps", lambda self: [])
    monkeypatch.setattr(
        CodeBuilder,
        "_package_zip",
        lambda self, zip_path, detection_result: (package_calls.append(zip_path), _fake_package_zip(zip_path, detection_result)),
    )

    initial = CodeBuilder(tmp_path).build()
    assert initial.success is True

    repackaged = CodeBuilder(tmp_path, config={"repackage": True}).build()

    assert repackaged.success is True
    assert len(install_calls) == 1
    assert len(package_calls) == 2


def test_code_builder_package_zip_reports_top_size_contributors(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")

    builder = CodeBuilder(tmp_path)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)
    (builder.deps_dir / "large_dep").mkdir()
    (builder.deps_dir / "large_dep" / "payload.bin").write_bytes(b"x" * 2048)
    (builder.deps_dir / "small_dep.py").write_text("# dep\n", encoding="utf-8")

    monkeypatch.setattr(CodeBuilder, "_iter_bundled_source_files", lambda self: [])

    zip_path = builder.build_dir / "demo.zip"
    builder._package_zip(zip_path, _FakeFrameworkDetector().detect())

    output = capsys.readouterr().out
    assert "包体积:" in output
    assert "体积 Top" in output
    assert "large_dep" in output


def test_code_builder_package_zip_suggests_container_only_for_large_artifacts(
    tmp_path: Path,
    capsys,
):
    builder = CodeBuilder(tmp_path)

    builder._emit_package_size_report_from_entries(
        raw_total=499 * 1024 * 1024,
        compressed_total=299 * 1024 * 1024,
        by_top_level={"deps": 499 * 1024 * 1024},
    )
    assert "建议使用 container 模式" not in capsys.readouterr().out

    builder._emit_package_size_report_from_entries(
        raw_total=501 * 1024 * 1024,
        compressed_total=299 * 1024 * 1024,
        by_top_level={"deps": 501 * 1024 * 1024},
    )
    assert "建议使用 container 模式" in capsys.readouterr().out

    builder._emit_package_size_report_from_entries(
        raw_total=100 * 1024 * 1024,
        compressed_total=301 * 1024 * 1024,
        by_top_level={"deps": 100 * 1024 * 1024},
    )
    assert "建议使用 container 模式" in capsys.readouterr().out


def test_code_builder_reports_rebuild_reason_for_runtime_source_changes(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "agentengine.yaml").write_text("name: demo-agent\nframework: langgraph\n", encoding="utf-8")

    fake_ksadk_root = tmp_path.parent / f"{tmp_path.name}_fake_ksadk_reason" / "ksadk"
    (fake_ksadk_root / "configs").mkdir(parents=True, exist_ok=True)
    (fake_ksadk_root / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
    settings_file = fake_ksadk_root / "configs" / "settings.py"
    settings_file.write_text("VALUE = 'v1'\n", encoding="utf-8")

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", _FakeFrameworkDetector)
    monkeypatch.setattr(CodeBuilder, "_install_dependencies", lambda self, _req: True)
    monkeypatch.setattr(CodeBuilder, "_package_zip", lambda self, zip_path, detection_result: _fake_package_zip(zip_path, detection_result))
    monkeypatch.setattr(ksadk, "__file__", str(fake_ksadk_root / "__init__.py"))

    first = CodeBuilder(tmp_path).build()
    assert first.success is True
    capsys.readouterr()

    settings_file.write_text("VALUE = 'v2'\n", encoding="utf-8")
    second = CodeBuilder(tmp_path).build()
    assert second.success is True

    output = capsys.readouterr().out
    assert "ksadk runtime 变更" in output
