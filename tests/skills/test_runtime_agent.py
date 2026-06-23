from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import httpx

from ksadk.skills.loader import load_local_skill
from ksadk.skills.models import SkillRef
from ksadk.skills.runtime.registry import select_remote_skill_refs
from ksadk.skills.runtime import agent as runtime_agent
from ksadk.skills.runtime.agent import run_agent


def _zip_bytes(skill_name: str = "demo-skill") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(f"{skill_name}/SKILL.md", f"---\nname: {skill_name}\ndescription: Demo\n---\n# Demo\n")
    return buf.getvalue()


def test_runtime_agent_loads_active_skills_from_service(monkeypatch, tmp_path: Path, capsys):
    archive = _zip_bytes()
    digest = hashlib.sha256(archive).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ListSkillsBySpaceId"):
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "SkillSpaceId": "ss-1",
                        "Skills": [
                            {
                                "SkillId": "sk-demo",
                                "VersionId": "sv-demo-v1",
                                "Version": "v1",
                                "Name": "demo-skill",
                                "Status": "Active",
                                "ContentHash": f"sha256:{digest}",
                            }
                        ],
                    }
                },
            )
        if request.url.path.endswith("/GetSkillDownloadUrl"):
            return httpx.Response(200, json={"Data": {"DownloadUrl": "https://download.example/demo.zip"}})
        if str(request.url) == "https://download.example/demo.zip":
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-1")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setenv("KSADK_SKILL_CACHE_DIR", str(tmp_path / "cache"))

    code = run_agent(
        ["使用 demo-skill build something"],
        service_transport=httpx.MockTransport(handler),
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "workflow=使用 demo-skill build something" in out
    assert "loaded_skills=demo-skill" in out
    assert (tmp_path / "cache" / "sk-demo__sv-demo-v1" / "extracted" / "demo-skill" / "SKILL.md").exists()


def test_runtime_selects_remote_skill_by_alias_tag_and_description():
    skills = [
        SkillRef(
            skill_id="sk-report",
            version_id="v1",
            version="1",
            name="report-writer",
            description="Write research reports",
            aliases=("研究报告",),
            tags=("research",),
        ),
        SkillRef(
            skill_id="sk-web",
            version_id="v1",
            version="1",
            name="web-builder",
            description="Build web pages",
            tags=("frontend",),
        ),
    ]

    assert [skill.name for skill in select_remote_skill_refs(skills, "帮我生成一份研究报告")] == ["report-writer"]
    assert [skill.name for skill in select_remote_skill_refs(skills, "frontend artifact")] == ["web-builder"]
    assert [skill.name for skill in select_remote_skill_refs(skills, "write a research report")] == ["report-writer"]


def test_runtime_agent_auto_resolves_aicp_skill_service_when_url_unset(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    archive = _zip_bytes()
    digest = hashlib.sha256(archive).hexdigest()
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.params.get("Action") == "ListSkillsBySpaceId":
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "SkillSpaceId": "ss-1",
                        "Skills": [
                            {
                                "SkillId": "sk-demo",
                                "VersionId": "sv-demo-v1",
                                "Version": "v1",
                                "Name": "demo-skill",
                                "Status": "Active",
                                "ContentHash": f"sha256:{digest}",
                            }
                        ],
                    }
                },
            )
        if request.url.params.get("Action") == "GetSkillDownloadUrl":
            return httpx.Response(200, json={"Data": {"DownloadUrl": "https://download.example/demo.zip"}})
        if str(request.url) == "https://download.example/demo.zip":
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-1")
    monkeypatch.delenv("KSADK_SKILL_SERVICE_URL", raising=False)
    monkeypatch.delenv("KSADK_SKILL_SERVICE_ENDPOINT", raising=False)
    monkeypatch.delenv("KSADK_SKILL_SERVICE_SCHEME", raising=False)
    monkeypatch.setenv("KSADK_AICP_ENDPOINT_MODE", "internal")
    monkeypatch.setenv("KSADK_SKILL_CACHE_DIR", str(tmp_path / "cache"))

    code = run_agent(
        ["使用 demo-skill build something"],
        service_transport=httpx.MockTransport(handler),
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "loaded_skills=demo-skill" in out
    assert seen_urls[0].startswith(
        "http://aicp.internal.api.ksyun.com/?Action=ListSkillsBySpaceId&Version=2024-06-12"
    )


def test_runtime_agent_downloads_only_prompted_remote_skill(monkeypatch, tmp_path: Path, capsys):
    demo_archive = _zip_bytes("demo-skill")
    unused_archive = _zip_bytes("unused-skill")
    demo_digest = hashlib.sha256(demo_archive).hexdigest()
    unused_digest = hashlib.sha256(unused_archive).hexdigest()
    download_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ListSkillsBySpaceId"):
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "SkillSpaceId": "ss-1",
                        "Skills": [
                            {
                                "SkillId": "sk-demo",
                                "VersionId": "sv-demo-v1",
                                "Version": "v1",
                                "Name": "demo-skill",
                                "Status": "Active",
                                "ContentHash": f"sha256:{demo_digest}",
                            },
                            {
                                "SkillId": "sk-unused",
                                "VersionId": "sv-unused-v1",
                                "Version": "v1",
                                "Name": "unused-skill",
                                "Status": "Active",
                                "ContentHash": f"sha256:{unused_digest}",
                            },
                        ],
                    }
                },
            )
        if request.url.path.endswith("/GetSkillDownloadUrl"):
            skill_id = request.url.params.get("SkillId")
            return httpx.Response(200, json={"Data": {"DownloadUrl": f"https://download.example/{skill_id}.zip"}})
        if str(request.url) == "https://download.example/sk-demo.zip":
            download_urls.append(str(request.url))
            return httpx.Response(200, content=demo_archive)
        if str(request.url) == "https://download.example/sk-unused.zip":
            download_urls.append(str(request.url))
            return httpx.Response(200, content=unused_archive)
        return httpx.Response(404)

    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-1")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setenv("KSADK_SKILL_CACHE_DIR", str(tmp_path / "cache"))

    code = run_agent(
        ["请使用 demo-skill 处理这个任务"],
        service_transport=httpx.MockTransport(handler),
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "loaded_skills=demo-skill" in out
    assert download_urls == ["https://download.example/sk-demo.zip"]
    assert (tmp_path / "cache" / "sk-demo__sv-demo-v1" / "extracted" / "demo-skill" / "SKILL.md").exists()
    assert not (tmp_path / "cache" / "sk-unused__sv-unused-v1").exists()


def test_runtime_agent_downloads_explicit_remote_skill_even_when_prompt_omits_name(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    demo_archive = _zip_bytes("demo-skill")
    unused_archive = _zip_bytes("unused-skill")
    demo_digest = hashlib.sha256(demo_archive).hexdigest()
    unused_digest = hashlib.sha256(unused_archive).hexdigest()
    download_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ListSkillsBySpaceId"):
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "SkillSpaceId": "ss-1",
                        "Skills": [
                            {
                                "SkillId": "sk-demo",
                                "VersionId": "sv-demo-v1",
                                "Version": "v1",
                                "Name": "demo-skill",
                                "Status": "Active",
                                "ContentHash": f"sha256:{demo_digest}",
                            },
                            {
                                "SkillId": "sk-unused",
                                "VersionId": "sv-unused-v1",
                                "Version": "v1",
                                "Name": "unused-skill",
                                "Status": "Active",
                                "ContentHash": f"sha256:{unused_digest}",
                            },
                        ],
                    }
                },
            )
        if request.url.path.endswith("/GetSkillDownloadUrl"):
            skill_id = request.url.params.get("SkillId")
            return httpx.Response(200, json={"Data": {"DownloadUrl": f"https://download.example/{skill_id}.zip"}})
        if str(request.url) == "https://download.example/sk-demo.zip":
            download_urls.append(str(request.url))
            return httpx.Response(200, content=demo_archive)
        if str(request.url) == "https://download.example/sk-unused.zip":
            download_urls.append(str(request.url))
            return httpx.Response(200, content=unused_archive)
        return httpx.Response(404)

    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-1")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setenv("KSADK_SKILL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("KSADK_SELECTED_SKILL_NAMES", "demo-skill")

    code = run_agent(
        ["请处理这个任务"],
        service_transport=httpx.MockTransport(handler),
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "loaded_skills=demo-skill" in out
    assert download_urls == ["https://download.example/sk-demo.zip"]
    assert (tmp_path / "cache" / "sk-demo__sv-demo-v1" / "extracted" / "demo-skill" / "SKILL.md").exists()
    assert not (tmp_path / "cache" / "sk-unused__sv-unused-v1").exists()


def test_runtime_agent_loads_all_public_skills_without_allowlist(monkeypatch, tmp_path: Path, capsys):
    pdf_archive = _zip_bytes("pdf")
    weather_archive = _zip_bytes("weather")
    pdf_digest = hashlib.sha256(pdf_archive).hexdigest()
    weather_digest = hashlib.sha256(weather_archive).hexdigest()
    download_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ListAvailablePremadeSkills"):
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "SkillSpaceId": "ss-public",
                        "Skills": [
                            {
                                "SkillId": "premade-pdf",
                                "VersionId": "",
                                "Version": "",
                                "Name": "pdf",
                                "Status": "AVAILABLE",
                                "ContentHash": pdf_digest,
                            },
                            {
                                "SkillId": "premade-weather",
                                "VersionId": "",
                                "Version": "",
                                "Name": "weather",
                                "Status": "AVAILABLE",
                                "ContentHash": weather_digest,
                            },
                        ],
                    }
                },
            )
        if request.url.path.endswith("/GetPremadeSkillDownloadUrl"):
            skill_id = request.url.params.get("SkillId")
            return httpx.Response(200, json={"Data": {"DownloadUrl": f"https://download.example/{skill_id}.zip"}})
        if str(request.url) == "https://download.example/premade-pdf.zip":
            download_urls.append(str(request.url))
            return httpx.Response(200, content=pdf_archive)
        if str(request.url) == "https://download.example/premade-weather.zip":
            download_urls.append(str(request.url))
            return httpx.Response(200, content=weather_archive)
        return httpx.Response(404)

    monkeypatch.setenv("KSADK_PUBLIC_SKILL_SPACE_IDS", "ss-public")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setenv("KSADK_SKILL_CACHE_DIR", str(tmp_path / "cache"))

    code = run_agent(["请处理这个任务"], service_transport=httpx.MockTransport(handler))

    out = capsys.readouterr().out
    assert code == 0
    assert "loaded_skills=pdf,weather" in out
    assert download_urls == [
        "https://download.example/premade-pdf.zip",
        "https://download.example/premade-weather.zip",
    ]
    assert (tmp_path / "cache" / f"premade-pdf__{pdf_digest}" / "extracted" / "pdf" / "SKILL.md").exists()
    assert (
        tmp_path
        / "cache"
        / f"premade-weather__{weather_digest}"
        / "extracted"
        / "weather"
        / "SKILL.md"
    ).exists()


def test_runtime_agent_filters_public_skills_with_allowlist(monkeypatch, tmp_path: Path, capsys):
    pdf_archive = _zip_bytes("pdf")
    weather_archive = _zip_bytes("weather")
    pdf_digest = hashlib.sha256(pdf_archive).hexdigest()
    weather_digest = hashlib.sha256(weather_archive).hexdigest()
    download_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ListAvailablePremadeSkills"):
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "SkillSpaceId": "ss-public",
                        "Skills": [
                            {
                                "SkillId": "premade-pdf",
                                "VersionId": "",
                                "Version": "",
                                "Name": "pdf",
                                "Status": "AVAILABLE",
                                "ContentHash": pdf_digest,
                            },
                            {
                                "SkillId": "premade-weather",
                                "VersionId": "",
                                "Version": "",
                                "Name": "weather",
                                "Status": "AVAILABLE",
                                "ContentHash": weather_digest,
                            },
                        ],
                    }
                },
            )
        if request.url.path.endswith("/GetPremadeSkillDownloadUrl"):
            skill_id = request.url.params.get("SkillId")
            return httpx.Response(200, json={"Data": {"DownloadUrl": f"https://download.example/{skill_id}.zip"}})
        if str(request.url) == "https://download.example/premade-pdf.zip":
            download_urls.append(str(request.url))
            return httpx.Response(200, content=pdf_archive)
        if str(request.url) == "https://download.example/premade-weather.zip":
            download_urls.append(str(request.url))
            return httpx.Response(200, content=weather_archive)
        return httpx.Response(404)

    monkeypatch.setenv("KSADK_PUBLIC_SKILL_SPACE_IDS", "ss-public")
    monkeypatch.setenv("KSADK_PUBLIC_SKILL_ALLOWLIST", "weather")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setenv("KSADK_SKILL_CACHE_DIR", str(tmp_path / "cache"))

    code = run_agent(["请处理这个任务"], service_transport=httpx.MockTransport(handler))

    out = capsys.readouterr().out
    assert code == 0
    assert "loaded_skills=weather" in out
    assert download_urls == ["https://download.example/premade-weather.zip"]
    assert not (tmp_path / "cache" / f"premade-pdf__{pdf_digest}").exists()
    assert (
        tmp_path
        / "cache"
        / f"premade-weather__{weather_digest}"
        / "extracted"
        / "weather"
        / "SKILL.md"
    ).exists()


def test_runtime_agent_prefers_user_skill_over_same_name_public_skill(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    user_archive = _zip_bytes("demo-skill")
    public_archive = _zip_bytes("demo-skill") + b"public"
    user_digest = hashlib.sha256(user_archive).hexdigest()
    public_digest = hashlib.sha256(public_archive).hexdigest()
    download_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ListSkillsBySpaceId"):
            space_id = request.url.params.get("SpaceId")
            if space_id == "ss-user":
                return httpx.Response(
                    200,
                    json={
                        "Data": {
                            "SkillSpaceId": "ss-user",
                            "Skills": [
                                {
                                    "SkillId": "sk-user-demo",
                                    "VersionId": "sv-user-v1",
                                    "Version": "v1",
                                    "Name": "demo-skill",
                                    "Status": "Active",
                                    "ContentHash": f"sha256:{user_digest}",
                                }
                            ],
                        }
                    },
                )
            return httpx.Response(404)
        if request.url.path.endswith("/ListAvailablePremadeSkills"):
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "SkillSpaceId": "ss-public",
                        "Skills": [
                            {
                                "SkillId": "premade-demo",
                                "VersionId": "",
                                "Version": "",
                                "Name": "demo-skill",
                                "Status": "AVAILABLE",
                                "ContentHash": f"sha256:{public_digest}",
                            }
                        ],
                    }
                },
            )
        if request.url.path.endswith("/GetSkillDownloadUrl"):
            return httpx.Response(200, json={"Data": {"DownloadUrl": "https://download.example/user.zip"}})
        if request.url.path.endswith("/GetPremadeSkillDownloadUrl"):
            return httpx.Response(200, json={"Data": {"DownloadUrl": "https://download.example/public.zip"}})
        if str(request.url) == "https://download.example/user.zip":
            download_urls.append(str(request.url))
            return httpx.Response(200, content=user_archive)
        if str(request.url) == "https://download.example/public.zip":
            download_urls.append(str(request.url))
            return httpx.Response(200, content=public_archive)
        return httpx.Response(404)

    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-user")
    monkeypatch.setenv("KSADK_PUBLIC_SKILL_SPACE_IDS", "ss-public")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setenv("KSADK_SKILL_CACHE_DIR", str(tmp_path / "cache"))

    code = run_agent(["请使用 demo-skill 处理任务"], service_transport=httpx.MockTransport(handler))

    out = capsys.readouterr().out
    assert code == 0
    assert "loaded_skills=demo-skill" in out
    assert download_urls == ["https://download.example/user.zip"]
    assert (tmp_path / "cache" / "sk-user-demo__sv-user-v1" / "extracted" / "demo-skill" / "SKILL.md").exists()
    assert not (tmp_path / "cache" / f"premade-demo__{public_digest}").exists()


def test_runtime_agent_can_load_legacy_remote_skill_when_hash_mismatch_is_allowed(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    archive = _zip_bytes("legacy-skill")
    download_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ListSkillsBySpaceId"):
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "SkillSpaceId": "ss-1",
                        "Skills": [
                            {
                                "SkillId": "sk-legacy",
                                "VersionId": "sv-legacy-v1",
                                "Version": "v1",
                                "Name": "legacy-skill",
                                "Status": "Active",
                                "ContentHash": f"sha256:{'0' * 64}",
                            }
                        ],
                    }
                },
            )
        if request.url.path.endswith("/GetSkillDownloadUrl"):
            return httpx.Response(200, json={"Data": {"DownloadUrl": "https://download.example/legacy.zip"}})
        if str(request.url) == "https://download.example/legacy.zip":
            download_urls.append(str(request.url))
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-1")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setenv("KSADK_SKILL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("KSADK_SELECTED_SKILL_NAMES", "legacy-skill")
    monkeypatch.setenv("KSADK_SKILL_ALLOW_HASH_MISMATCH", "true")

    code = run_agent(
        ["请处理这个任务"],
        service_transport=httpx.MockTransport(handler),
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "loaded_skills=legacy-skill" in out
    assert "skill_warnings=" in out
    assert "ContentHash mismatch for legacy-skill" in out
    assert download_urls == ["https://download.example/legacy.zip"]
    assert (
        tmp_path
        / "cache"
        / "unverified-sk-legacy__sv-legacy-v1"
        / "extracted"
        / "legacy-skill"
        / "SKILL.md"
    ).exists()


def test_runtime_agent_without_service_still_reports_workflow(monkeypatch, capsys):
    monkeypatch.delenv("KSADK_SKILL_SERVICE_URL", raising=False)
    monkeypatch.delenv("KSADK_SKILL_SPACE_IDS", raising=False)
    monkeypatch.delenv("SKILL_SPACE_ID", raising=False)
    monkeypatch.delenv("KSADK_PUBLIC_SKILL_SPACE_IDS", raising=False)

    code = run_agent(["noop"])

    out = capsys.readouterr().out
    assert code == 0
    assert "workflow=noop" in out
    assert "loaded_skills=" in out


def test_runtime_agent_reads_prompt_file(tmp_path: Path, monkeypatch, capsys):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("from file", encoding="utf-8")
    monkeypatch.delenv("KSADK_SKILL_SERVICE_URL", raising=False)

    code = run_agent(["--prompt-file", str(prompt_file)])

    out = capsys.readouterr().out
    assert code == 0
    assert "workflow=from file" in out


def test_runtime_agent_accepts_request_file_json(tmp_path: Path, monkeypatch, capsys):
    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps(
            {
                "workflow_prompt": "from request",
                "skill_names": ["generic-workflow"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("KSADK_SKILL_SERVICE_URL", raising=False)

    code = run_agent(["--request-file", str(request_file)])

    out = capsys.readouterr().out
    assert code == 0
    assert "workflow=from request" in out
    assert 'selected_skills=["generic-workflow"]' in out


def test_runtime_agent_rejects_prompt_file_and_request_file_together(tmp_path: Path, monkeypatch, capsys):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("from file", encoding="utf-8")
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps({"workflow_prompt": "from request"}), encoding="utf-8")
    monkeypatch.delenv("KSADK_SKILL_SERVICE_URL", raising=False)

    code = run_agent(["--prompt-file", str(prompt_file), "--request-file", str(request_file)])

    out = capsys.readouterr().out
    assert code == 1
    assert "workflow_result=" in out
    payload = json.loads(out.split("workflow_result=", 1)[1])
    assert payload["status"] == "failed"
    assert "cannot be used together" in payload["error"]


def test_runtime_agent_executes_generic_run_workflow_script(monkeypatch, tmp_path: Path):
    skill_root = tmp_path / "skills" / "generic-workflow"
    scripts_dir = skill_root / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: generic-workflow\ndescription: Generic workflow\n---\n# Demo\n",
        encoding="utf-8",
    )
    (scripts_dir / "run-workflow.sh").write_text(
        "#!/bin/bash\n"
        "mkdir -p \"$KSADK_SKILL_WORKDIR/out\"\n"
        "printf '%s' \"$KSADK_WORKFLOW_PROMPT\" > \"$KSADK_SKILL_WORKDIR/out/prompt.txt\"\n"
        "echo \"artifact=$KSADK_SKILL_WORKDIR/out/prompt.txt\"\n",
        encoding="utf-8",
    )
    workdir = tmp_path / "work"
    monkeypatch.setenv("KSADK_SKILL_WORKDIR", str(workdir))

    result = runtime_agent._execute_workflow(
        "run generic workflow",
        [load_local_skill(skill_root)],
        selected_skill_names=["generic-workflow"],
    )

    artifact = str(workdir / "out" / "prompt.txt")
    assert result.status == "ok"
    assert result.executed_skill == "generic-workflow"
    assert result.selected_skills == ["generic-workflow"]
    assert result.loaded_skills == ["generic-workflow"]
    assert result.output_files == [artifact]
    assert result.artifacts == [artifact]
    assert result.commands[0]["exit_code"] == 0
    assert (workdir / "out" / "prompt.txt").read_text(encoding="utf-8") == "run generic workflow"


def test_runtime_agent_collects_generic_workflow_output_dir(monkeypatch, tmp_path: Path):
    skill_root = tmp_path / "skills" / "output-dir-workflow"
    scripts_dir = skill_root / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: output-dir-workflow\ndescription: Output dir workflow\n---\n# Demo\n",
        encoding="utf-8",
    )
    (scripts_dir / "run-workflow.sh").write_text(
        "#!/bin/bash\n"
        "mkdir -p \"$KSADK_SKILL_OUTPUT_DIR\"\n"
        "printf 'generated' > \"$KSADK_SKILL_OUTPUT_DIR/result.txt\"\n",
        encoding="utf-8",
    )
    workdir = tmp_path / "work"
    monkeypatch.setenv("KSADK_SKILL_WORKDIR", str(workdir))

    result = runtime_agent._execute_workflow(
        "run output-dir workflow",
        [load_local_skill(skill_root)],
        selected_skill_names=["output-dir-workflow"],
    )

    artifact = str(workdir / "artifacts" / "result.txt")
    assert result.status == "ok"
    assert result.output_files == [artifact]
    assert result.artifacts == [artifact]


def test_runtime_agent_warns_when_loaded_skill_has_no_workflow_entrypoint(tmp_path: Path):
    skill_root = tmp_path / "skills" / "instruction-only"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: instruction-only\ndescription: Instruction only\n---\n# Demo\n",
        encoding="utf-8",
    )

    result = runtime_agent._execute_workflow(
        "run instruction-only",
        [load_local_skill(skill_root)],
        selected_skill_names=["instruction-only"],
    )

    assert result.status == "skipped"
    assert result.selected_skills == ["instruction-only"]
    assert result.loaded_skills == ["instruction-only"]
    assert result.warnings == ["No loaded skill exposes an executable workflow entrypoint."]


def test_runtime_agent_executes_web_artifacts_builder_without_real_npm(monkeypatch, tmp_path: Path):
    skill_root = tmp_path / "skills" / "web-artifacts-builder"
    scripts_dir = skill_root / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: web-artifacts-builder\ndescription: Build artifacts\n---\n# Demo\n",
        encoding="utf-8",
    )
    (scripts_dir / "init-artifact.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (scripts_dir / "bundle-artifact.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    workdir = tmp_path / "work"
    monkeypatch.setenv("KSADK_SKILL_WORKDIR", str(workdir))
    monkeypatch.setenv("KSADK_SKILL_ARTIFACT_PROJECT", "demo-artifact")

    def fake_run(args, **kwargs):
        if str(args[-1]).endswith("demo-artifact"):
            (workdir / "demo-artifact").mkdir(parents=True)
        elif str(args[1]).endswith("bundle-artifact.sh"):
            (workdir / "demo-artifact" / "bundle.html").write_text("<html></html>", encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(runtime_agent.subprocess, "run", fake_run)

    result = runtime_agent._execute_workflow(
        "使用 web-artifacts-builder 初始化并打包一个最小 artifact",
        [load_local_skill(skill_root)],
    )

    assert result.status == "ok"
    assert result.executed_skill == "web-artifacts-builder"
    assert result.output_files == [str(workdir / "demo-artifact" / "bundle.html")]
    assert [command["exit_code"] for command in result.commands] == [0, 0]
