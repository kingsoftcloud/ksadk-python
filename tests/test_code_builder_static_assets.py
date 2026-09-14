import ast
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

    static_files = [n for n in names if n.startswith("ksadk/studio/static/")]
    assert static_files, "应包含 ksadk/studio/static 目录下资源"
    assert any(n.endswith(".html") for n in static_files), "应包含 html 入口"
    assert any(n.endswith(".js") for n in static_files), "应包含 js 资源"
    assert any(n.endswith(".css") for n in static_files), "应包含 css 资源"
    assert not any(n.startswith("ksadk/server/web-ui/") for n in names), (
        "runtime 产物不应包含前端源码/node_modules"
    )


def test_code_builder_packages_project_custom_ui_dist(tmp_path):
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")
    custom_dist = tmp_path / "research-ui" / "dist" / "assets"
    custom_dist.mkdir(parents=True)
    (tmp_path / "research-ui" / "dist" / "index.html").write_text(
        "<title>Custom UI</title>", encoding="utf-8"
    )
    (custom_dist / "index.js").write_text("console.log('custom ui')\n", encoding="utf-8")
    (tmp_path / "research-ui" / "node_modules").mkdir(parents=True)
    (tmp_path / "research-ui" / "node_modules" / "ignored.js").write_text(
        "ignored\n", encoding="utf-8"
    )

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

    assert "research-ui/dist/index.html" in names
    assert "research-ui/dist/assets/index.js" in names
    assert "research-ui/node_modules/ignored.js" not in names


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


def test_code_builder_embeds_ksadk_runtime_identity_in_the_archive(tmp_path):
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
        source = zf.read("ksadk/_bundle_identity.py").decode("utf-8")
    identity = ast.literal_eval(source.split("=", 1)[1].strip())
    assert identity["ksadk_version"]
    assert len(identity["ksadk_source_digest"]) == 64
    # Builds outside a Git checkout are still honest: commit can be empty,
    # but it must never be substituted from a process environment variable.
    assert len(identity["ksadk_commit"]) in {0, 40, 64}


def test_code_builder_prints_identity_from_the_actual_zip(tmp_path, capsys):
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
    capsys.readouterr()

    builder._emit_bundled_ksadk_identity(zip_path)

    output = capsys.readouterr().out
    assert "KsADK: version=" in output
    assert "KsADK source: commit=" in output
    assert "KsADK source digest: sha256=" in output


def test_code_builder_excludes_real_dotenv_files_but_keeps_example(tmp_path):
    (tmp_path / "agent.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("LOCAL_SECRET=secret\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")

    private_dir = tmp_path / ".agentkit"
    private_dir.mkdir(exist_ok=True)
    (private_dir / "config.yaml").write_text("secrets: {MODEL_KEY: fixture-private}\n")

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

    assert ".agentkit/config.yaml" not in names
    assert ".env" not in names
    assert ".env.local" not in names
    assert ".env.example" in names
