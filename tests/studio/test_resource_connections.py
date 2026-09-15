from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pydantic import ValidationError

from ksadk.studio.api import create_studio_app
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import CredentialResolver
from ksadk.studio.resource_connections import (
    ResourceConnectionDeclaration,
    ResourceConnectionRepository,
)
from ksadk.studio.workspace import Workspace


def declaration():
    return ResourceConnectionDeclaration.model_validate(
        {
            "label": "Test resources",
            "target": {
                "connectionRef": "platform-a",
                "tenantRef": "declared-tenant",
                "principalRef": "declared-account",
                "endpoint": "https://api.example.test/",
                "authMode": "signed",
            },
            "credentials": {
                "accessKeyRef": "env://RESOURCE_AK",
                "secretKeyRef": "env://RESOURCE_SK",
            },
        }
    )


def test_declaration_reopen_rotation_and_target_drift(tmp_path):
    resolver = CredentialResolver()
    resolver.put_session("RESOURCE_AK", "fake-access")
    resolver.put_session("RESOURCE_SK", "fake-secret")
    repo = ResourceConnectionRepository(Workspace(tmp_path), resolver)
    record = repo.save(declaration(), expected_revision=0)
    assert record.revision == 1
    reopened = ResourceConnectionRepository(Workspace(tmp_path), resolver)
    assert reopened.list() == [record]
    assert (
        reopened.resolve_credentials(record.target).secret_key.get_secret_value() == "fake-secret"
    )
    with pytest.raises(StudioError) as revision_drift:
        reopened.resolve_credentials(record.target, expected_revision=2)
    assert revision_drift.value.code == "RESOURCE_CONNECTION_CHANGED"
    resolver.put_session("RESOURCE_SK", "fake-rotated")
    assert (
        reopened.resolve_credentials(record.target).secret_key.get_secret_value() == "fake-rotated"
    )
    assert "fake-" not in "".join(path.read_text() for path in repo.root.glob("*.json"))
    assert "fake-rotated" not in repr(reopened.resolve_credentials(record.target))
    changed = declaration().model_dump()
    changed["target"]["endpoint"] = "https://other.example.test"
    reopened.save(ResourceConnectionDeclaration.model_validate(changed), expected_revision=1)
    with pytest.raises(StudioError) as drift:
        reopened.resolve_credentials(record.target)
    assert drift.value.code == "RESOURCE_CONNECTION_CHANGED"


def test_explicit_secret_reference_never_uses_model_alias(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTKIT_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-unrelated-model-key")
    resolver = CredentialResolver()
    assert resolver.resolve("env://AGENTKIT_MODEL_API_KEY") == "fake-unrelated-model-key"
    with pytest.raises(StudioError, match="尚未配置"):
        resolver.resolve("env://AGENTKIT_MODEL_API_KEY", allow_aliases=False)
    payload = declaration().model_dump()
    payload["target"]["auth_mode"] = "token"
    payload["credentials"] = {"token_ref": "env://AGENTKIT_MODEL_API_KEY"}
    repo = ResourceConnectionRepository(Workspace(tmp_path), resolver)
    record = repo.save(ResourceConnectionDeclaration.model_validate(payload), expected_revision=0)
    with pytest.raises(StudioError) as missing:
        repo.resolve_credentials(record.target)
    assert missing.value.code == "SECRET_NOT_FOUND"


@pytest.mark.parametrize(
    "credentials",
    [
        {"tokenRef": "env://TOKEN"},
        {"accessKeyRef": "env://AK"},
        {"accessKeyRef": "env://AK", "secretKeyRef": "literal-secret"},
        {"accessKeyRef": "env://AK", "secretKeyRef": "env://SK", "apiKey": "literal-secret"},
    ],
)
def test_raw_or_mismatched_credentials_rejected(credentials):
    payload = declaration().model_dump(by_alias=True)
    payload["credentials"] = credentials
    with pytest.raises(ValidationError):
        ResourceConnectionDeclaration.model_validate(payload)


def test_concurrent_save_cannot_overwrite_same_revision(tmp_path):
    repo = ResourceConnectionRepository(Workspace(tmp_path), CredentialResolver())

    def save():
        try:
            return repo.save(declaration(), expected_revision=0).revision
        except StudioError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(), range(2)))
    assert sorted(map(str, results)) == ["1", "RESOURCE_CONNECTION_REVISION_CONFLICT"]
    assert len(repo.list()) == 1


def test_connection_directory_cannot_escape_workspace(tmp_path):
    root = tmp_path / "workspace"
    repo = ResourceConnectionRepository(Workspace(root), CredentialResolver())
    repo.root.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    repo.root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(StudioError) as rejected:
        repo.save(declaration(), expected_revision=0)
    assert rejected.value.code == "WORKSPACE_PATH_FORBIDDEN"
    assert not list(outside.iterdir())


async def test_real_studio_api_guard_save_reopen_and_conflict(tmp_path):
    app = create_studio_app(tmp_path, session_token="fixture-session", csrf_token="fixture-csrf")
    headers = {
        "X-AgentKit-Session": "fixture-session",
        "X-CSRF-Token": "fixture-csrf",
        "Origin": "http://testserver",
    }
    payload = {"expectedRevision": 0, "connection": declaration().model_dump(by_alias=True)}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/api/v1/resource-connections")).status_code == 401
        no_csrf = await client.put(
            "/api/v1/resource-connections/platform-a",
            json=payload,
            headers={"X-AgentKit-Session": "fixture-session"},
        )
        assert no_csrf.status_code == 403
        saved = await client.put(
            "/api/v1/resource-connections/platform-a", json=payload, headers=headers
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 1
        assert saved.json()["validationState"] == "unverified"
        stale = await client.put(
            "/api/v1/resource-connections/platform-a", json=payload, headers=headers
        )
        assert stale.status_code == 409
        mismatch = await client.put(
            "/api/v1/resource-connections/other", json=payload, headers=headers
        )
        assert mismatch.status_code == 422
    reopened = create_studio_app(tmp_path, session_token="fixture-session")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reopened), base_url="http://testserver"
    ) as client:
        listed = await client.get("/api/v1/resource-connections", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["items"] == [saved.json()]
