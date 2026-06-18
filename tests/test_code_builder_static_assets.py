import zipfile
from types import SimpleNamespace

from ksadk.builders.code_builder import CodeBuilder


class _FakeType:
    name = "LANGGRAPH"


def test_code_builder_packages_web_static_assets(tmp_path):
    # 最小项目结构
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")

    builder = CodeBuilder(tmp_path)
    builder.build_dir.mkdir(parents=True, exist_ok=True)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)

    detection_result = SimpleNamespace(
        package_path=str(tmp_path),
        type=_FakeType(),
        name="demo_agent",
        entry_point="agent.py",
        agent_variable="root_agent",
    )

    zip_path = tmp_path / "demo.zip"
    builder._package_zip(zip_path, detection_result)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

    static_files = [n for n in names if n.startswith("ksadk/server/static/")]
    assert static_files, "应包含 ksadk/server/static 目录下资源"
    assert any(n.endswith(".html") for n in static_files), "应包含 html 入口"
    assert any(n.endswith(".js") for n in static_files), "应包含 js 资源"
    assert any(n.endswith(".css") for n in static_files), "应包含 css 资源"
    assert not any(n.startswith("ksadk/server/web-ui/") for n in names), (
        "runtime 产物不应包含前端源码/node_modules"
    )


def test_code_builder_packages_runtime_common_sources(tmp_path):
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")

    builder = CodeBuilder(tmp_path)
    builder.build_dir.mkdir(parents=True, exist_ok=True)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)

    detection_result = SimpleNamespace(
        package_path=str(tmp_path),
        type=_FakeType(),
        name="demo_agent",
        entry_point="agent.py",
        agent_variable="root_agent",
    )

    zip_path = tmp_path / "demo.zip"
    builder._package_zip(zip_path, detection_result)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

    assert any(n.startswith("ksadk_runtime_common/") for n in names), (
        "应包含 ksadk_runtime_common 共享运行时代码"
    )


def test_code_builder_excludes_real_dotenv_files_but_keeps_example(tmp_path):
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("LOCAL_SECRET=secret\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")

    builder = CodeBuilder(tmp_path)
    builder.build_dir.mkdir(parents=True, exist_ok=True)
    builder.deps_dir.mkdir(parents=True, exist_ok=True)

    detection_result = SimpleNamespace(
        package_path=str(tmp_path),
        type=_FakeType(),
        name="demo_agent",
        entry_point="agent.py",
        agent_variable="root_agent",
    )

    zip_path = tmp_path / "demo.zip"
    builder._package_zip(zip_path, detection_result)

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())

    assert ".env" not in names
    assert ".env.local" not in names
    assert ".env.example" in names
