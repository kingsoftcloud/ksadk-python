from pathlib import Path

from ksadk.builders.container_builder import ContainerBuilder
from ksadk.detection import FrameworkDetector


def test_container_builder_preserves_hermes_template_dockerfile(tmp_path: Path):
    project = tmp_path / "demo-hermes"
    project.mkdir()
    (project / "runtime").mkdir()
    (project / "runtime" / "app.py").write_text("app = object()\n", encoding="utf-8")
    (project / "entrypoint.sh").write_text("#!/usr/bin/env bash\nexec true\n", encoding="utf-8")
    (project / "Dockerfile").write_text("FROM python:3.12-slim\nCMD [\"/app/entrypoint.sh\"]\n", encoding="utf-8")
    (project / "agentengine.yaml").write_text(
        "name: demo_hermes\nframework: hermes\nartifact_type: Container\n",
        encoding="utf-8",
    )

    detection = FrameworkDetector(str(project)).detect()
    assert detection.type.value == "hermes"
    assert detection.entry_point == "runtime/app.py"

    package = ContainerBuilder(project)._package(detection)

    build_dir = Path(package.build_dir)
    assert (build_dir / "Dockerfile").read_text(encoding="utf-8") == "FROM python:3.12-slim\nCMD [\"/app/entrypoint.sh\"]\n"
    assert (build_dir / "entrypoint.sh").exists()
    assert not (build_dir / "entrypoint.py").exists()
