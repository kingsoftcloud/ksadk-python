from types import SimpleNamespace

from ksadk.builders.code_builder import CodeBuilder
from ksadk.builders.container_builder import ContainerBuilder
from ksadk.builders.mcp_builder import MCPCodeBuilder


def _detection_result(framework: str):
    return SimpleNamespace(type=SimpleNamespace(value=framework))


def test_code_builder_prefers_user_pins_over_base_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "fastapi==0.121.2\nuvicorn==0.38.0\npython-dotenv==1.2.1\n",
        encoding="utf-8",
    )
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "fastapi==0.121.2" in deps
    assert "uvicorn==0.38.0" in deps
    assert "python-dotenv==1.2.1" in deps
    assert "fastapi>=0.100.0" not in deps
    assert "uvicorn>=0.23.0" not in deps
    assert "python-dotenv>=1.0.0" not in deps


def test_container_builder_prefers_user_pins_over_base_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "fastapi==0.121.2\nuvicorn==0.38.0\npython-dotenv==1.2.1\n",
        encoding="utf-8",
    )
    builder = ContainerBuilder(tmp_path)

    deps = builder._generate_requirements(
        _detection_result("langgraph"),
        tmp_path,
    ).splitlines()

    assert "fastapi==0.121.2" in deps
    assert "uvicorn==0.38.0" in deps
    assert "python-dotenv==1.2.1" in deps
    assert "fastapi>=0.100.0" not in deps
    assert "uvicorn>=0.23.0" not in deps
    assert "python-dotenv>=1.0.0" not in deps


def test_mcp_builder_prefers_user_pins_over_base_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "uvicorn==0.38.0\npython-dotenv==1.2.1\n",
        encoding="utf-8",
    )
    builder = MCPCodeBuilder(tmp_path)
    builder.build_dir.mkdir(parents=True, exist_ok=True)

    requirements_path = builder._prepare_mcp_requirements(SimpleNamespace())
    deps = requirements_path.read_text(encoding="utf-8").splitlines()

    assert "uvicorn==0.38.0" in deps
    assert "python-dotenv==1.2.1" in deps
    assert "uvicorn>=0.23.0" not in deps
    assert "python-dotenv>=1.0.0" not in deps


def test_code_builder_omits_bundled_ksadk_package_from_runtime_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "fastapi==0.121.2\nksadk==0.3.9\n",
        encoding="utf-8",
    )
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "fastapi==0.121.2" in deps
    assert "ksadk==0.3.9" not in deps
    assert all(not dep.startswith("ksadk") for dep in deps)
    assert "a2a-sdk>=0.3.22" in deps
    assert "requests-aws4auth>=1.2.0" in deps


def test_container_builder_omits_bundled_ksadk_package_from_runtime_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "fastapi==0.121.2\nksadk==0.3.9\n",
        encoding="utf-8",
    )
    builder = ContainerBuilder(tmp_path)

    deps = builder._generate_requirements(
        _detection_result("langgraph"),
        tmp_path,
    ).splitlines()

    assert "fastapi==0.121.2" in deps
    assert "ksadk==0.3.9" not in deps
    assert all(not dep.startswith("ksadk") for dep in deps)
    assert "a2a-sdk>=0.3.22" in deps
    assert "requests-aws4auth>=1.2.0" in deps


def test_code_builder_bundles_attachment_runtime_requirements_without_optional_backends(tmp_path):
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "pypdf>=6.0.0" in deps
    assert "beautifulsoup4>=4.12.0" in deps
    assert "rapidocr-onnxruntime>=1.2.0" in deps
    assert "boto3==1.40.61" not in deps
    assert "SQLAlchemy==2.0.44" not in deps
    assert "psycopg[binary]==3.3.0" not in deps
    assert "psycopg-pool==3.3.0" not in deps
    assert "pandas==2.2.2" not in deps
    assert "openpyxl==3.1.5" not in deps
    assert "xlrd==2.0.2" not in deps
    assert "python-pptx==1.0.2" not in deps
    assert "docx2python==3.5.0" not in deps


def test_container_builder_bundles_attachment_runtime_requirements_without_optional_backends(tmp_path):
    builder = ContainerBuilder(tmp_path)

    deps = builder._generate_requirements(
        _detection_result("langgraph"),
        tmp_path,
    ).splitlines()

    assert "pypdf>=6.0.0" in deps
    assert "beautifulsoup4>=4.12.0" in deps
    assert "rapidocr-onnxruntime>=1.2.0" in deps
    assert "boto3==1.40.61" not in deps
    assert "SQLAlchemy==2.0.44" not in deps
    assert "psycopg[binary]==3.3.0" not in deps
    assert "psycopg-pool==3.3.0" not in deps
    assert "pandas==2.2.2" not in deps
    assert "openpyxl==3.1.5" not in deps
    assert "xlrd==2.0.2" not in deps
    assert "python-pptx==1.0.2" not in deps
    assert "docx2python==3.5.0" not in deps


def test_code_builder_includes_bundled_attachment_runtime_requirements(tmp_path):
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "pypdf>=6.0.0" in deps
    assert "beautifulsoup4>=4.12.0" in deps


def test_container_builder_includes_bundled_attachment_runtime_requirements(tmp_path):
    builder = ContainerBuilder(tmp_path)

    deps = builder._generate_requirements(
        _detection_result("langgraph"),
        tmp_path,
    ).splitlines()

    assert "pypdf>=6.0.0" in deps
    assert "beautifulsoup4>=4.12.0" in deps
