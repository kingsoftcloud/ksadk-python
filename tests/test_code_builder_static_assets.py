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
