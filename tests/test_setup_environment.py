from pathlib import Path

from ksadk.configs import setup_environment


def test_setup_environment_mirrors_openai_model_name_to_model_name(monkeypatch, tmp_path: Path):
    (tmp_path / ".env").write_text("OPENAI_MODEL_NAME=deepseek-v3.2\n", encoding="utf-8")

    monkeypatch.delenv("OPENAI_MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)

    setup_environment(tmp_path)

    assert str(Path.cwd())  # keep test explicit about no exception
    assert __import__("os").environ["OPENAI_MODEL_NAME"] == "deepseek-v3.2"
    assert __import__("os").environ["MODEL_NAME"] == "deepseek-v3.2"
