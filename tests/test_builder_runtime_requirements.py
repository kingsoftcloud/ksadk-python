from ksadk.builders.code_builder import CodeBuilder


def test_bundled_runtime_requirements_include_kingsoftcloud_sdk():
    assert "kingsoftcloud-sdk-python>=1.5.8.78" in CodeBuilder.BUNDLED_KSADK_RUNTIME_REQUIREMENTS


def test_bundled_runtime_requirements_include_python_multipart():
    assert "python-multipart>=0.0.9,<1.0.0" in CodeBuilder.BUNDLED_KSADK_RUNTIME_REQUIREMENTS
