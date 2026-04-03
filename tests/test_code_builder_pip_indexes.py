import subprocess

from ksadk.builders.code_builder import CodeBuilder


class _DummyThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return None

    def join(self):
        return None


def _completed_process(cmd):
    return subprocess.CompletedProcess(cmd, 0, "", "")


def test_install_dependencies_respects_explicit_pip_index(tmp_path, monkeypatch):
    builder = CodeBuilder(tmp_path)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)
    requirements_path = tmp_path / "requirements.txt"
    requirements_path.write_text("demo==1.0\n", encoding="utf-8")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _completed_process(cmd)

    monkeypatch.setenv("PIP_INDEX_URL", "https://pypi.org/simple")
    monkeypatch.setattr("ksadk.builders.code_builder.threading.Thread", _DummyThread)
    monkeypatch.setattr("ksadk.builders.code_builder.subprocess.run", fake_run)
    monkeypatch.setattr(CodeBuilder, "_scan_incompatible_binaries_in_deps", lambda self: [])

    assert builder._install_dependencies(requirements_path) is True
    assert calls
    assert "-i" not in calls[0]


def test_replace_platform_binaries_respects_explicit_pip_index(tmp_path, monkeypatch):
    builder = CodeBuilder(tmp_path)
    builder.build_dir.mkdir(parents=True, exist_ok=True)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)
    (builder.deps_dir / "tiktoken").mkdir(parents=True, exist_ok=True)
    (builder.deps_dir / "tiktoken" / "_tiktoken.cpython-314-darwin.so").write_text("", encoding="utf-8")
    (builder.deps_dir / "tiktoken-0.9.0.dist-info").mkdir(parents=True, exist_ok=True)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _completed_process(cmd)

    monkeypatch.setenv("PIP_INDEX_URL", "https://pypi.org/simple")
    monkeypatch.setattr("ksadk.builders.code_builder.subprocess.run", fake_run)

    builder._replace_platform_binaries()

    assert calls
    assert "-i" not in calls[0]
