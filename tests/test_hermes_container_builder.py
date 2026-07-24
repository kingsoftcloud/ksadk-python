import os
import subprocess
import sys
from pathlib import Path

from ksadk.builders.container_builder import ContainerBuilder
from ksadk.detection import DetectionResult, FrameworkDetector, FrameworkType


def test_container_builder_preserves_hermes_template_dockerfile(tmp_path: Path):
    project = tmp_path / "demo-hermes"
    project.mkdir()
    (project / "runtime").mkdir()
    (project / "runtime" / "app.py").write_text("app = object()\n", encoding="utf-8")
    (project / "entrypoint.sh").write_text("#!/usr/bin/env bash\nexec true\n", encoding="utf-8")
    (project / "Dockerfile").write_text(
        'FROM python:3.12-slim\nCMD ["/app/entrypoint.sh"]\n', encoding="utf-8"
    )
    (project / "agentengine.yaml").write_text(
        "name: demo_hermes\nframework: hermes\nartifact_type: Container\n",
        encoding="utf-8",
    )

    detection = FrameworkDetector(str(project)).detect()
    assert detection.type.value == "hermes"
    assert detection.entry_point == "runtime/app.py"

    package = ContainerBuilder(project)._package(detection)

    build_dir = Path(package.build_dir)
    assert (build_dir / "Dockerfile").read_text(
        encoding="utf-8"
    ) == 'FROM python:3.12-slim\nCMD ["/app/entrypoint.sh"]\n'
    assert (build_dir / "entrypoint.sh").exists()
    assert not (build_dir / "entrypoint.py").exists()


def test_container_builder_bundles_runtime_common_for_image_mode(tmp_path: Path):
    project = tmp_path / "demo-langgraph"
    project.mkdir()
    package_dir = project / "demo_langgraph"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "agent.py").write_text("root_agent = object()\n", encoding="utf-8")

    detection = DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="demo-langgraph",
        entry_point="demo_langgraph/agent.py",
        package_path=str(package_dir),
        agent_variable="root_agent",
        confidence=1.0,
    )

    package = ContainerBuilder(project)._package(detection)

    build_dir = Path(package.build_dir)
    assert (build_dir / "ksadk" / "server" / "app.py").exists()
    assert (build_dir / "ksadk_runtime_common" / "workspace_files" / "__init__.py").exists()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(build_dir)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ksadk.server.app; import ksadk_runtime_common; print('ok')",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_container_builder_packages_project_custom_ui_dist_without_node_modules(tmp_path: Path):
    project = tmp_path / "demo-langgraph"
    project.mkdir()
    package_dir = project / "demo_langgraph"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "agent.py").write_text("root_agent = object()\n", encoding="utf-8")
    custom_dist = project / "research-ui" / "dist" / "assets"
    custom_dist.mkdir(parents=True)
    (project / "research-ui" / "dist" / "index.html").write_text(
        "<title>Custom UI</title>", encoding="utf-8"
    )
    (custom_dist / "index.js").write_text("console.log('custom ui')\n", encoding="utf-8")
    (project / "research-ui" / "node_modules").mkdir(parents=True)
    (project / "research-ui" / "node_modules" / "ignored.js").write_text(
        "ignored\n", encoding="utf-8"
    )

    detection = DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="demo-langgraph",
        entry_point="demo_langgraph/agent.py",
        package_path=str(package_dir),
        agent_variable="root_agent",
        confidence=1.0,
    )

    package = ContainerBuilder(project)._package(detection)
    build_dir = Path(package.build_dir)

    assert (build_dir / "research-ui" / "dist" / "index.html").exists()
    assert (build_dir / "research-ui" / "dist" / "assets" / "index.js").exists()
    assert not (build_dir / "research-ui" / "node_modules" / "ignored.js").exists()
    assert "from ksadk.server import app, set_runner" in (build_dir / "entrypoint.py").read_text(
        encoding="utf-8"
    )


def test_container_builder_excludes_real_dotenv_files_but_keeps_example(tmp_path: Path):
    project = tmp_path / "demo-langgraph"
    project.mkdir()
    package_dir = project / "demo_langgraph"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "agent.py").write_text("root_agent = object()\n", encoding="utf-8")
    (project / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    (project / ".env.local").write_text("LOCAL_SECRET=secret\n", encoding="utf-8")
    (project / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")

    detection = DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="demo-langgraph",
        entry_point="demo_langgraph/agent.py",
        package_path=str(package_dir),
        agent_variable="root_agent",
        confidence=1.0,
    )

    package = ContainerBuilder(project)._package(detection)

    build_dir = Path(package.build_dir)
    assert not (build_dir / ".env").exists()
    assert not (build_dir / ".env.local").exists()
    assert (build_dir / ".env.example").exists()
