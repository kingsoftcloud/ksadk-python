"""ksadk init --framework codex 脚手架 + builder codex 依赖单测(假数据,不打网络)。"""

from pathlib import Path

from click.testing import CliRunner

from ksadk.builders.framework_requirements import (
    code_requirements_for_framework,
    minimal_requirements_for_framework,
    requirements_for_framework,
)
from ksadk.cli.cmd_create import _write_codex_project_config
from ksadk.cli.cmd_create import create as create_command
from ksadk.detection.detector import FrameworkDetector, FrameworkType


def test_write_codex_project_config_files(tmp_path):
    """codex 项目:agentengine.yaml(framework:codex)+requirements+README,无 agent.py。"""
    _write_codex_project_config(tmp_path, "my-codex-agent")
    yaml_text = (tmp_path / "agentengine.yaml").read_text(encoding="utf-8-sig")
    assert "framework: codex" in yaml_text
    assert "artifact_type: ManagedRuntime" in yaml_text
    assert "name: codex" in yaml_text
    assert "model: glm-5.2" in yaml_text
    assert "prompt:" in yaml_text
    # requirements 是 ksadk[codex]
    assert "ksadk[codex]" in (tmp_path / "requirements.txt").read_text(encoding="utf-8-sig")
    # 无 package 目录 / 无 agent.py(codex 逻辑由 prompt 承载)
    assert not (tmp_path / "my-codex-agent").exists()
    assert not list(tmp_path.glob("**/agent.py"))
    assert not (tmp_path / "codex.yaml").exists()
    # README 说明 codex 运行方式
    readme = (tmp_path / "README.md").read_text(encoding="utf-8-sig")
    assert "ksadk web" in readme


def test_detector_recognizes_codex_project(tmp_path):
    """detector 对 codex 项目识别为 FrameworkType.CODEX。"""
    _write_codex_project_config(tmp_path, "my-codex-agent")
    result = FrameworkDetector(str(tmp_path)).detect()
    assert result.type == FrameworkType.CODEX


def test_framework_requirements_codex():
    """Codex 只进入 Linux container/runtime，不进入宿主机 Code zip。"""
    assert requirements_for_framework("codex") == ["openai-codex==0.144.4"]
    assert minimal_requirements_for_framework("codex") == ["openai-codex==0.144.4"]
    assert code_requirements_for_framework("codex") == []
    # 大小写/空白归一
    assert requirements_for_framework(" Codex ") == ["openai-codex==0.144.4"]


def test_create_codex_cli_warns_when_sdk_missing_but_completes(monkeypatch):
    """真实 init 命令在缺 SDK 时提示，但仍生成完整 canonical 项目。"""
    import importlib.util

    original_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "openai_codex" else original_find_spec(name),
    )
    monkeypatch.setattr(
        "ksadk.configs.global_config.global_config_exists",
        lambda: False,
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            create_command,
            ["--framework", "codex", "my-codex-agent"],
        )
        project = Path("my-codex-agent")

        assert result.exit_code == 0, result.output
        assert "ksadk[codex]" in result.output
        assert (project / "agentengine.yaml").exists()
        assert (project / ".env").exists()
        assert (project / "requirements.txt").exists()
        assert (project / "README.md").exists()
        assert not (project / "codex.yaml").exists()
        assert not list(project.glob("**/agent.py"))


def test_requirements_no_codex_when_other_framework():
    """其他框架不受影响(adk/langchain 不含 codex 依赖)。"""
    assert "openai-codex" not in requirements_for_framework("adk")
    assert "openai-codex" not in requirements_for_framework("langchain")
