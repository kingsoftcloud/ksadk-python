import io
import json
import subprocess
import sys

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


def test_install_dependencies_uses_persistent_project_pip_cache(tmp_path, monkeypatch):
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

    assert "--cache-dir" in calls[0]
    cache_pos = calls[0].index("--cache-dir")
    assert calls[0][cache_pos + 1] == str(builder.build_dir / "pip_cache")


def test_install_dependencies_timeout_is_configurable(tmp_path, monkeypatch):
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

    observed_timeouts = []

    def fake_run_streamed(self, install_cmd, *, timeout):
        observed_timeouts.append(timeout)
        return subprocess.CompletedProcess(install_cmd, 0, "", "")

    monkeypatch.setenv("KSADK_BUILD_PIP_INSTALL_TIMEOUT_SECONDS", "2700")
    monkeypatch.setattr("ksadk.builders.code_builder.subprocess.Popen", fake_popen)
    monkeypatch.setattr(CodeBuilder, "_run_streamed_pip_install", fake_run_streamed)
    monkeypatch.setattr(CodeBuilder, "_scan_incompatible_binaries_in_deps", lambda self: [])

    assert builder._install_dependencies(requirements_path) is True

    assert observed_timeouts == [2700]


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


def test_install_dependencies_advances_download_progress_with_wheel_activity(
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
        f"Downloading demo-{index}.0-py3-none-any.whl\n"
        for index in range(1, 26)
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
    assert "下载依赖" in output
    assert "耗时" in output
    assert "已处理 25 个 wheel" in output
    download_percents = [
        int(line.split("%", 1)[0].rsplit(" ", 1)[-1])
        for line in output.splitlines()
        if "下载依赖" in line and "%" in line
    ]
    assert max(download_percents) >= 60


def test_install_dependencies_does_not_pin_long_downloads_at_68_percent(
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
        for index in range(1, 71)
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
    download_percents = [
        int(line.split("%", 1)[0].rsplit(" ", 1)[-1])
        for line in output.splitlines()
        if "下载依赖" in line and "%" in line
    ]
    assert max(download_percents) > 68


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


def test_install_dependencies_bootstraps_pip_when_missing(
    tmp_path,
    monkeypatch,
    capsys,
):
    builder = CodeBuilder(tmp_path)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)
    requirements_path = tmp_path / "requirements.txt"
    requirements_path.write_text("demo==1.0\n", encoding="utf-8")

    calls = []
    popen_attempts = {"count": 0}

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_popen(cmd, **kwargs):
        popen_attempts["count"] += 1
        if popen_attempts["count"] == 1:
            return _FakePopen(
                cmd,
                calls=[],
                output_lines=[f"{sys.executable}: No module named pip\n"],
                returncode=1,
                **kwargs,
            )
        return _FakePopen(
            cmd,
            calls=[],
            output_lines=[
                "Collecting demo==1.0\n",
                "Installing collected packages: demo\n",
                "Successfully installed demo-1.0\n",
            ],
            **kwargs,
        )

    monkeypatch.setattr("ksadk.builders.code_builder.subprocess.run", fake_run)
    monkeypatch.setattr("ksadk.builders.code_builder.subprocess.Popen", fake_popen)
    monkeypatch.setattr(CodeBuilder, "_scan_incompatible_binaries_in_deps", lambda self: [])

    assert builder._install_dependencies(requirements_path) is True

    output = capsys.readouterr().out
    assert "pip 工具链缺失" in output
    assert calls
    assert calls[0][:3] == [sys.executable, "-m", "ensurepip"]


def test_package_zip_reports_milestone_progress_for_large_dependency_tree(
    tmp_path,
    monkeypatch,
    capsys,
):
    builder = CodeBuilder(tmp_path)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "agentengine.yaml").write_text("name: demo-agent\nframework: langgraph\n", encoding="utf-8")
    for index in range(1, 1002):
        (builder.deps_dir / f"dep_{index}.py").write_text("# dep\n", encoding="utf-8")

    monkeypatch.setattr(CodeBuilder, "_iter_bundled_source_files", lambda self: iter(()))

    detection_result = type(
        "Detection",
        (),
        {
            "package_path": str(tmp_path / "agent.py"),
            "type": type("T", (), {"name": "LANGGRAPH"})(),
            "name": "demo-agent",
            "entry_point": "agent.py",
            "agent_variable": "agent",
        },
    )()

    builder._package_zip(builder.build_dir / "demo.zip", detection_result)

    output = capsys.readouterr().out
    assert "打包依赖" in output
    assert "100%" in output
    assert "1000/1001 files" not in output
