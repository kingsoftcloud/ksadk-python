from __future__ import annotations

from ksadk.builders.container_builder import ContainerBuilder
from ksadk.builders.mcp_builder import MCPContainerBuilder
from ksadk.cli import cmd_mcp, cmd_openclaw
from ksadk.detection.mcp_detector import MCPDetectionResult
from ksadk.deployment.providers.serverless import ServerlessProvider


def test_enterprise_registry_requires_explicit_kcr_username(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("KCR_USERNAME", raising=False)
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")
    monkeypatch.setenv("KCR_PASSWORD", "secret")
    monkeypatch.setenv("KCR_REGISTRY", "agenthzzqy-vpc.ksyunkcr.com/testagent-pub")

    builder = ContainerBuilder(tmp_path)

    assert builder._auto_login_from_env("agenthzzqy-vpc.ksyunkcr.com") is False

    output = capsys.readouterr().out
    assert "企业版或第三方镜像仓库必须配置 KCR_USERNAME 和 KCR_PASSWORD" in output
    assert "KCR_USERNAME=<镜像仓库访问凭证用户名>" in output
    assert "KSYUN_ACCOUNT_ID 只会作为个人版 KCR 的用户名兜底" in output


def test_personal_registry_can_fallback_to_ksyun_account_id(monkeypatch, tmp_path):
    calls = []
    monkeypatch.delenv("KCR_USERNAME", raising=False)
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")
    monkeypatch.setenv("KCR_PASSWORD", "secret")
    monkeypatch.setenv("KCR_REGISTRY", "hub.kce.ksyun.com/agentengine")

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr("ksadk.builders.container_builder.subprocess.run", fake_run)

    builder = ContainerBuilder(tmp_path)

    assert builder._auto_login_from_env("hub.kce.ksyun.com") is True
    assert calls[0][0] == [
        "docker",
        "login",
        "hub.kce.ksyun.com",
        "-u",
        "2000003485",
        "--password-stdin",
    ]
    assert calls[0][1]["input"] == "secret"


def test_mcp_container_request_does_not_fallback_for_enterprise_registry(monkeypatch, capsys):
    monkeypatch.delenv("KCR_USERNAME", raising=False)
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")
    monkeypatch.setenv("KCR_PASSWORD", "secret")

    class Detection:
        mcp_variable = "mcp"
        tools = []

    request = cmd_mcp._build_mcp_request_data(
        config={},
        mcp_name="demo-mcp",
        artifact_type="Container",
        artifact_reference="agenthzzqy-vpc.ksyunkcr.com/testagent-pub/demo:v1",
        region="cn-beijing-6",
        enable_auth=False,
        detection_result=Detection(),
    )

    assert "image_credential" not in request
    output = capsys.readouterr().out
    assert "未配置企业版 KCR 镜像凭证 (KCR_USERNAME/KCR_PASSWORD)" in output


def test_openclaw_container_request_does_not_fallback_for_third_party_registry(monkeypatch, capsys):
    monkeypatch.delenv("KCR_USERNAME", raising=False)
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")
    monkeypatch.setenv("KCR_PASSWORD", "secret")
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})

    username, password, kind = cmd_openclaw.resolve_registry_credentials(
        "registry-1.docker.io/acme/openclaw:v1",
        environ=cmd_openclaw._openclaw_registry_env(),
    )

    assert (username, password, kind) == ("", "secret", "third_party")


def test_serverless_container_request_does_not_fallback_for_enterprise_registry(monkeypatch, capsys):
    monkeypatch.delenv("KCR_USERNAME", raising=False)
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")
    monkeypatch.setenv("KCR_PASSWORD", "secret")

    credential = ServerlessProvider._image_credential_from_env(
        "agenthzzqy-vpc.ksyunkcr.com/testagent-pub/demo:v1"
    )

    assert credential is None
    output = capsys.readouterr().out
    assert "缺少 KCR_USERNAME" in output
    assert "企业版 KCR" in output


def test_mcp_container_builder_excludes_real_dotenv_files_but_keeps_example(tmp_path):
    (tmp_path / "server.py").write_text(
        "from fastmcp import FastMCP\nmcp = FastMCP('demo')\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("LOCAL_SECRET=secret\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")

    package = MCPContainerBuilder(tmp_path)._package_mcp_project(
        MCPDetectionResult(
            is_mcp=True,
            name="demo-mcp",
            entry_point="server.py",
            package_path=str(tmp_path),
            mcp_variable="mcp",
            tools=[],
            confidence=1.0,
        )
    )

    build_dir = tmp_path / ".agentengine" / "container_build"
    assert package.build_dir == str(build_dir)
    assert not (build_dir / ".env").exists()
    assert not (build_dir / ".env.local").exists()
    assert (build_dir / ".env.example").exists()
