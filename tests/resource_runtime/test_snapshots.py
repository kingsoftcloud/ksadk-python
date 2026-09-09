import pytest
from pydantic import ValidationError

from ksadk.resource_runtime.snapshots import ConnectionTarget, ResourceSnapshot


def target(**changes):
    return ConnectionTarget.model_validate(
        {
            "connectionRef": "connection-a",
            "tenantRef": "tenant-a",
            "principalRef": "account-a",
            "endpoint": "https://resources.example.test",
            "authMode": "signed",
            **changes,
        }
    )


def snapshot():
    return ResourceSnapshot.model_validate(
        {
            "pluginLockDigest": "sha256:" + "a" * 64,
            "bindings": [
                {
                    "config": {
                        "binding": {
                            "id": "binding-a",
                            "connectionRef": "connection-a",
                            "resource": {
                                "kind": "knowledge-base",
                                "id": "kb-a",
                                "region": "region-a",
                            },
                        }
                    },
                    "connection": target().model_dump(),
                }
            ],
        }
    )


@pytest.mark.parametrize(
    "change",
    [
        {"principalRef": "account-b"},
        {"tenantRef": "tenant-b"},
        {"endpoint": "https://other.example.test"},
        {"authMode": "sts"},
        {"connectionRef": "connection-b"},
    ],
)
def test_changed_connection_cannot_activate_old_snapshot(change):
    with pytest.raises(ValueError, match="RESOURCE_CONNECTION_CHANGED"):
        snapshot().verify_connection("binding-a", target(**change))


def test_endpoint_equivalence_and_absence_of_credentials():
    frozen = snapshot()
    frozen.verify_connection("binding-a", target(endpoint="https://RESOURCES.example.test:443/"))
    assert target(endpoint="http://[::1]:80/").endpoint == "http://[::1]"
    with pytest.raises(ValidationError):
        target(apiKey="fake-secret")
    assert "fake-secret" not in frozen.model_dump_json()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret@example.test",
        "https://example.test?key=fake",
        "https://example.test/#fragment",
        "https://example.test:99999",
        "https://example.test\\@other.test",
        "file:///tmp/resource",
    ],
)
def test_ambiguous_endpoint_is_rejected(endpoint):
    with pytest.raises(ValidationError):
        target(endpoint=endpoint)


def test_config_and_plugin_lock_are_part_of_digest():
    original = snapshot()
    data = original.model_dump(by_alias=True, mode="json")
    data["bindings"][0]["config"]["binding"]["resource"]["id"] = "kb-b"
    assert ResourceSnapshot.model_validate(data).digest != original.digest
    data = original.model_dump(by_alias=True, mode="json")
    data["pluginLockDigest"] = "sha256:" + "b" * 64
    assert ResourceSnapshot.model_validate(data).digest != original.digest
    data = original.model_dump(by_alias=True, mode="json")
    data["bindings"][0]["connectionRevision"] = 2
    assert ResourceSnapshot.model_validate(data).digest != original.digest
