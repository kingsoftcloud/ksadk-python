from ksadk.builders.code_builder import CodeBuilder


def test_detect_critical_binary_issues_accepts_target_python_abi(tmp_path):
    builder = CodeBuilder(tmp_path)
    names = ["pydantic_core/_pydantic_core.cpython-312-x86_64-linux-gnu.so"]

    issues = builder._detect_critical_binary_issues(names)

    assert not issues


def test_detect_critical_binary_issues_rejects_python_abi_mismatch(tmp_path):
    builder = CodeBuilder(tmp_path)
    names = ["pydantic_core/_pydantic_core.cpython-313-x86_64-linux-gnu.so"]

    issues = builder._detect_critical_binary_issues(names)

    assert (
        "python-abi-mismatch:pydantic_core/_pydantic_core:"
        "expected-cpython-312-or-abi3"
    ) in issues


def test_detect_critical_binary_issues_rejects_non_linux_binary(tmp_path):
    builder = CodeBuilder(tmp_path)
    names = ["pydantic_core/_pydantic_core.cpython-312-darwin.so"]

    issues = builder._detect_critical_binary_issues(names)

    assert "missing-linux:pydantic_core/_pydantic_core" in issues
