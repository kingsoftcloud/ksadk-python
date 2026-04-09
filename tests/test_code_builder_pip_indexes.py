import io
import json
import subprocess

from ksadk.builders.code_builder import CodeBuilder


def _completed_process(cmd):
    return subprocess.CompletedProcess(cmd, 0, "", "")


class _FakePopen:
    def __init__(self, cmd, *, calls, output_lines=None, returncode=0, **_kwargs):
        calls.append(cmd)
        self.args = cmd
        self.returncode = returncode
        self.stdout = io.StringIO("".join(output_lines or []))

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        return None


def test_install_dependencies_respects_explicit_pip_index(tmp_path, monkeypatch):
    builder = CodeBuilder(tmp_path)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)
    requirements_path = tmp_path / "requirements.txt"
    requirements_path.write_text("demo==1.0\n", encoding="utf-8")

    calls = []

    def fake_popen(cmd, **kwargs):
        return _FakePopen(
            cmd,
            calls=calls,
            output_lines=[
                "Collecting demo==1.0\n",
                "Installing collected packages: demo\n",
                "Successfully installed demo-1.0\n",
            ],
            **kwargs,
        )

    monkeypatch.setenv("PIP_INDEX_URL", "https://pypi.org/simple")
    monkeypatch.setattr("ksadk.builders.code_builder.subprocess.Popen", fake_popen)
    monkeypatch.setattr(CodeBuilder, "_scan_incompatible_binaries_in_deps", lambda self: [])

    assert builder._install_dependencies(requirements_path) is True
    assert calls
    assert "-i" not in calls[0]


def test_install_dependencies_prefers_target_runtime_wheels(tmp_path, monkeypatch):
    builder = CodeBuilder(tmp_path)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)
    requirements_path = tmp_path / "requirements.txt"
    requirements_path.write_text("demo==1.0\n", encoding="utf-8")

    calls = []

    def fake_popen(cmd, **kwargs):
        return _FakePopen(
            cmd,
            calls=calls,
            output_lines=[
                "Collecting demo==1.0\n",
                "Downloading demo-1.0-py3-none-any.whl\n",
                "Installing collected packages: demo\n",
                "Successfully installed demo-1.0\n",
            ],
            **kwargs,
        )

    monkeypatch.setattr("ksadk.builders.code_builder.subprocess.Popen", fake_popen)
    monkeypatch.setattr(CodeBuilder, "_scan_incompatible_binaries_in_deps", lambda self: [])

    assert builder._install_dependencies(requirements_path) is True
    assert calls
    assert "--platform" in calls[0]
    assert "manylinux2014_x86_64" in calls[0]
    assert "--python-version" in calls[0]
    assert builder.TARGET_PYTHON_VERSION in calls[0]
    assert "--only-binary=:all:" in calls[0]


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


def test_install_dependencies_reports_percent_bar_and_recent_event(
    tmp_path,
    monkeypatch,
    capsys,
):
    builder = CodeBuilder(tmp_path)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)
    requirements_path = tmp_path / "requirements.txt"
    requirements_path.write_text("demo==1.0\n", encoding="utf-8")

    calls = []

    def fake_popen(cmd, **kwargs):
        return _FakePopen(
            cmd,
            calls=calls,
            output_lines=[
                "Collecting demo==1.0\n",
                "Downloading demo-1.0-py3-none-any.whl\n",
                "Installing collected packages: demo\n",
                "Successfully installed demo-1.0\n",
            ],
            **kwargs,
        )

    monkeypatch.setattr("ksadk.builders.code_builder.subprocess.Popen", fake_popen)
    monkeypatch.setattr(CodeBuilder, "_scan_incompatible_binaries_in_deps", lambda self: [])

    assert builder._install_dependencies(requirements_path) is True

    output = capsys.readouterr().out
    assert "100%" in output
    assert "安装包: demo" in output


def test_install_progress_is_monotonic_and_uses_arrow_style_bar(tmp_path):
    builder = CodeBuilder(tmp_path)

    builder._emit_install_progress(40, "下载依赖", "Downloading demo-1.0.whl")
    builder._emit_install_progress(18, "解析依赖", "Collecting demo==1.0")

    assert builder._install_progress_percent == 40
    assert builder._install_progress_stage_name == "下载依赖"
    assert builder._install_progress_summary_text == "Downloading demo-1.0.whl"

    rendered = builder._render_install_progress(40, "下载依赖", "Downloading demo-1.0.whl")
    assert "#" not in rendered
    assert ">" in rendered
    assert "=" in rendered


def test_install_dependencies_aggregates_repeated_download_updates(
    tmp_path,
    monkeypatch,
    capsys,
):
    builder = CodeBuilder(tmp_path)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)
    requirements_path = tmp_path / "requirements.txt"
    requirements_path.write_text("demo==1.0\n", encoding="utf-8")

    calls = []
    download_lines = [
        f"Using cached https://mirror.example/simple/demo-{index}.whl\n"
        for index in range(1, 13)
    ]

    def fake_popen(cmd, **kwargs):
        return _FakePopen(
            cmd,
            calls=calls,
            output_lines=[
                "Collecting demo==1.0\n",
                *download_lines,
                "Installing collected packages: demo\n",
                "Successfully installed demo-1.0\n",
            ],
            **kwargs,
        )

    monkeypatch.setattr("ksadk.builders.code_builder.subprocess.Popen", fake_popen)
    monkeypatch.setattr(CodeBuilder, "_scan_incompatible_binaries_in_deps", lambda self: [])

    assert builder._install_dependencies(requirements_path) is True

    output = capsys.readouterr().out
    assert "已处理 10 个 wheel" in output
    assert output.count("下载依赖") < len(download_lines)


def test_install_dependencies_prefers_fastest_cached_pip_index(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cache_dir = home / ".agentengine"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "pip-index-cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": 999.0,
                "order": [
                    "https://mirrors.aliyun.com/pypi/simple",
                    "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple",
                    "https://mirrors.cloud.tencent.com/pypi/simple",
                    "https://pypi.org/simple",
                ],
            }
        ),
        encoding="utf-8",
    )

    builder = CodeBuilder(tmp_path)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)
    requirements_path = tmp_path / "requirements.txt"
    requirements_path.write_text("demo==1.0\n", encoding="utf-8")

    calls = []

    def fake_popen(cmd, **kwargs):
        return _FakePopen(
            cmd,
            calls=calls,
            output_lines=[
                "Collecting demo==1.0\n",
                "Installing collected packages: demo\n",
                "Successfully installed demo-1.0\n",
            ],
            **kwargs,
        )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("UV_INDEX_URL", raising=False)
    monkeypatch.setattr("ksadk.builders.code_builder.time.time", lambda: 1000.0)
    monkeypatch.setattr("ksadk.builders.code_builder.subprocess.Popen", fake_popen)
    monkeypatch.setattr(CodeBuilder, "_scan_incompatible_binaries_in_deps", lambda self: [])

    assert builder._install_dependencies(requirements_path) is True
    assert calls

    index_pos = calls[0].index("-i")
    assert calls[0][index_pos + 1] == "https://mirrors.aliyun.com/pypi/simple"


def test_install_dependencies_download_summary_uses_artifact_name(
    tmp_path,
    monkeypatch,
    capsys,
):
    builder = CodeBuilder(tmp_path)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)
    requirements_path = tmp_path / "requirements.txt"
    requirements_path.write_text("demo==1.0\n", encoding="utf-8")

    calls = []

    def fake_popen(cmd, **kwargs):
        return _FakePopen(
            cmd,
            calls=calls,
            output_lines=[
                "Collecting demo==1.0\n",
                "Downloading demo-1.0-py3-none-any.whl.metadata (117 kB)\n",
                "Installing collected packages: demo\n",
                "Successfully installed demo-1.0\n",
            ],
            **kwargs,
        )

    monkeypatch.setattr("ksadk.builders.code_builder.subprocess.Popen", fake_popen)
    monkeypatch.setattr(CodeBuilder, "_scan_incompatible_binaries_in_deps", lambda self: [])

    assert builder._install_dependencies(requirements_path) is True

    output = capsys.readouterr().out
    assert "demo-1.0-py3-none-any.whl.metadata" in output
    assert "最近: (117" not in output
