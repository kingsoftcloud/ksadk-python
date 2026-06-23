import asyncio
import json
from pathlib import Path

from ksadk.builders import BuildResult
from ksadk.cli import cmd_build


class _FakeNow:
    def strftime(self, _fmt: str) -> str:
        return "20260308154645"


class _FakeDatetime:
    @staticmethod
    def now():
        return _FakeNow()


class _FakeCodeBuilder:
    last_config: dict | None = None

    def __init__(self, project_dir: Path, config: dict = None):
        self.project_dir = Path(project_dir)
        self.config = config or {}
        self.__class__.last_config = self.config

    def build(self) -> BuildResult:
        return BuildResult(
            success=True,
            artifact_path=self.project_dir / ".agentengine" / "code_build" / "demo.zip",
            artifact_size=1234,
            metadata={"agent_name": "demo_agent", "framework": "langgraph"},
        )


class _FakeKS3Uploader:
    last_object_key: str | None = None

    def __init__(self, region: str, bucket: str = None):
        self.region = region
        self.bucket = bucket

    async def upload(self, _file_path: Path, object_key: str):
        self.__class__.last_object_key = object_key
        return f"ks3://agentengine-test/{object_key}"

    def get_public_url_by_key(self, object_key: str) -> str:
        return f"https://public.example.com/{object_key.lstrip('/')}"

    def get_internal_url_by_key(self, object_key: str) -> str:
        return f"https://internal.example.com/{object_key.lstrip('/')}"


def test_build_push_prints_object_key_urls_and_never_prints_code_zip(tmp_path: Path, monkeypatch, capsys):
    import ksadk.builders as builders_module

    monkeypatch.setattr(builders_module, "CodeBuilder", _FakeCodeBuilder)
    monkeypatch.setattr(builders_module, "KS3Uploader", _FakeKS3Uploader)
    monkeypatch.setattr(cmd_build, "datetime", _FakeDatetime)

    asyncio.run(
        cmd_build._build_code(
            agent_path=tmp_path,
            push=True,
            region="cn-beijing-6",
            ks3_bucket="agentengine-test",
            no_cache=True,
            repackage=False,
        )
    )

    out = capsys.readouterr().out
    expected_name = "code_20260308154645.zip"
    expected_key = f"agents/demo_agent/{expected_name}"

    assert _FakeKS3Uploader.last_object_key == expected_key
    assert expected_name in out
    assert "/code.zip" not in out
    assert "回滚请使用历史不可变包路径 (ks3_path)" in out

    metadata_path = tmp_path / ".agentengine" / "build-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["metadata"]["ks3_path"].endswith(expected_key)


def test_build_code_passes_repackage_to_code_builder(tmp_path: Path, monkeypatch):
    import ksadk.builders as builders_module

    monkeypatch.setattr(builders_module, "CodeBuilder", _FakeCodeBuilder)

    asyncio.run(
        cmd_build._build_code(
            agent_path=tmp_path,
            push=False,
            region="cn-beijing-6",
            ks3_bucket=None,
            no_cache=False,
            repackage=True,
        )
    )

    assert _FakeCodeBuilder.last_config == {"no_cache": False, "repackage": True}
