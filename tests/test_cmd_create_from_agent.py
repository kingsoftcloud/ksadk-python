from pathlib import Path

from click.testing import CliRunner

from ksadk.cli import cmd_create


def test_find_entry_file_from_agentengine_yaml(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    entry = src / "agentengine_adapter.py"
    entry.write_text("root_agent = object()\n", encoding="utf-8")
    (tmp_path / "agentengine.yaml").write_text(
        "framework: langgraph\nentry_point: src/agentengine_adapter.py\nagent_variable: root_agent\n",
        encoding="utf-8",
    )

    found = cmd_create._find_entry_file(tmp_path)
    assert found is not None
    found_file, found_var = found
    assert found_file == entry
    assert found_var == "root_agent"


def test_find_entry_file_recursive_scan(tmp_path: Path):
    entry = tmp_path / "src" / "nested" / "custom_entry.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("root_agent = object()\n", encoding="utf-8")

    found = cmd_create._find_entry_file(tmp_path)
    assert found is not None
    found_file, found_var = found
    assert found_file == entry
    assert found_var == "root_agent"


def test_wrap_agent_directory_ignores_venv_and_exports_nested_entry(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    entry = source / "src" / "agentengine_adapter.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        "def build_agent():\n"
        "    return {\"ok\": True}\n"
        "root_agent = build_agent()\n",
        encoding="utf-8",
    )

    # Should be excluded by copytree ignore rules
    venv_file = source / ".venv-ae" / "lib" / "dummy.py"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text("x = 1\n", encoding="utf-8")

    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: False)
    monkeypatch.setattr("ksadk.configs.global_config.get_env_from_global_config", lambda: {})

    project_path = tmp_path / "wrapped-project"
    cmd_create._wrap_agent_directory(source, str(project_path), "langgraph", entry, "root_agent")

    package_dir = project_path / "wrapped_project"
    assert package_dir.exists()
    assert not (package_dir / ".venv-ae").exists()

    init_content = (package_dir / "__init__.py").read_text(encoding="utf-8")
    assert "from .src.agentengine_adapter import root_agent as root_agent" in init_content


def test_create_openclaw_only_generates_env_file(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: True)
    monkeypatch.setattr(
        "ksadk.configs.global_config.get_env_from_global_config",
        lambda: {
            "OPENAI_API_KEY": "sk-openclaw",
            "OPENAI_BASE_URL": "https://model.example.com/v1",
            "OPENAI_MODEL_NAME": "glm-5",
            "LANGFUSE_PUBLIC_KEY": "pk-should-not-exist",
            "LANGFUSE_SECRET_KEY": "sk-should-not-exist",
            "LANGFUSE_BASE_URL": "https://langfuse.example.com",
            "KSYUN_ACCESS_KEY": "ak-demo",
            "KSYUN_SECRET_KEY": "sk-demo",
            "KSYUN_REGION": "cn-beijing-6",
            "KSYUN_ACCOUNT_ID": "1234567890",
        },
    )

    result = runner.invoke(cmd_create.create, ["demo-openclaw", "-f", "openclaw"])

    assert result.exit_code == 0, result.output

    project_dir = tmp_path / "demo-openclaw"
    assert project_dir.exists()
    assert sorted(path.name for path in project_dir.iterdir()) == [".env"]

    env_text = (project_dir / ".env").read_text(encoding="utf-8-sig")
    assert "KSYUN_ACCESS_KEY=ak-demo" in env_text
    assert "KSYUN_SECRET_KEY=sk-demo" in env_text
    assert "KSYUN_REGION=cn-beijing-6" in env_text
    assert "KSYUN_ACCOUNT_ID=1234567890" in env_text
    assert "OPENAI_API_KEY=sk-openclaw" in env_text
    assert "OPENAI_BASE_URL=https://model.example.com/v1" in env_text
    assert "OPENAI_MODEL_NAME=glm-5" in env_text
    assert "LANGFUSE_" not in env_text
