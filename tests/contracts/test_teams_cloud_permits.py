import base64
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ksadk.plugins.teams.cloud_permits import (
    BindingProbePermit,
    PermitError,
    TeamsCallbackPermit,
    TeamsExecutionPermit,
    TeamsPermitVerifier,
    sign_permit,
    timestamp,
)

NOW = datetime(2026, 9, 18, 8, tzinfo=timezone.utc)
DIGEST = "sha256:" + "a" * 64
COMMAND = "11111111-1111-4111-8111-111111111111"


class TestSigner:
    __test__ = False
    key_id = "test-key"

    def __init__(self):
        self.private = Ed25519PrivateKey.generate()

    def sign(self, value):
        return base64.urlsafe_b64encode(self.private.sign(value)).decode().rstrip("=")


@pytest.fixture
def signed():
    signer = TestSigner()
    verifier = TeamsPermitVerifier({signer.key_id: signer.private.public_key()}, issuer="test")
    return signer, verifier


def execution(**overrides):
    return TeamsExecutionPermit.model_validate(
        {
            "issuer": "test",
            "kid": "test-key",
            "authorityId": "authority-example",
            "issuedAt": timestamp(NOW),
            "expiresAt": timestamp(NOW + timedelta(seconds=20)),
            "nonce": "nonce-example",
            "permitKind": "execute",
            "subjectRef": "subject-example",
            "agentInstanceId": "instance-example",
            "sessionId": "session-example",
            "commandId": COMMAND,
            "allowedOperations": ["enqueue", "renew_grant"],
            "payloadDigest": DIGEST,
            "policyDigest": DIGEST,
            "leaderEpoch": 1,
            "dispatchEpoch": 1,
            "attemptEpoch": 1,
            "grantRevision": 1,
            **overrides,
        }
    )


def expected(permit):
    keys = {
        "authorityId",
        "agentInstanceId",
        "sessionId",
        "commandId",
        "policyDigest",
        "attemptEpoch",
        "grantRevision",
        "subjectRef",
        "payloadDigest",
    }
    return {k: v for k, v in permit.model_dump().items() if k in keys}


def verify(verifier, permit, **kwargs):
    args = dict(
        operation="enqueue",
        expected=expected(permit),
        now=NOW,
        grant_expires_at=NOW + timedelta(seconds=30),
    )
    args.update(kwargs)
    return verifier.verify(permit.model_dump(mode="json"), TeamsExecutionPermit, **args)


def test_valid_signature_requires_original_operation_and_live_grant(signed):
    signer, verifier = signed
    permit = sign_permit(execution(), signer)
    assert verify(verifier, permit).commandId == COMMAND
    with pytest.raises(PermitError, match="active execution grant"):
        verify(verifier, permit, grant_expires_at=None)
    with pytest.raises(PermitError, match="exceeds execution grant"):
        verify(verifier, permit, grant_expires_at=NOW + timedelta(seconds=10))


@pytest.mark.parametrize("key", list(expected(execution())))
def test_every_persisted_scope_claim_is_required_and_exact(signed, key):
    signer, verifier = signed
    permit = sign_permit(execution(), signer)
    scope = expected(permit)
    scope.pop(key)
    with pytest.raises(PermitError, match="scope"):
        verify(verifier, permit, expected=scope)
    scope[key] = "different-original-operation"
    with pytest.raises(PermitError, match="scope"):
        verify(verifier, permit, expected=scope)


@pytest.mark.parametrize("seconds", [-1, 20, 60])
def test_no_future_or_expired_permit(signed, seconds):
    signer, verifier = signed
    permit = sign_permit(execution(), signer)
    with pytest.raises(PermitError, match="not currently valid"):
        verify(verifier, permit, now=NOW + timedelta(seconds=seconds))


def test_tamper_wrong_key_wrong_audience_and_unknown_issuer(signed):
    signer, verifier = signed
    permit = sign_permit(execution(), signer)
    for changes in (
        {"sessionId": "other"},
        {"signature": "invalid"},
        {"kid": "untrusted"},
        {"issuer": "untrusted"},
        {"audience": "agentengine-teams-callback"},
    ):
        with pytest.raises(PermitError):
            verifier.verify(
                {**permit.model_dump(), **changes},
                TeamsExecutionPermit,
                operation="enqueue",
                expected=expected(permit),
                now=NOW,
            )


def test_recovery_cannot_restore_execution(signed):
    signer, verifier = signed
    for operation in ["enqueue", "invoke", "renew_grant", "respond_interaction"]:
        with pytest.raises(ValueError):
            execution(permitKind="recovery", allowedOperations=[operation])
    permit = sign_permit(execution(permitKind="recovery", allowedOperations=["cancel"]), signer)
    assert verify(verifier, permit, operation="cancel", grant_expires_at=None)
    with pytest.raises(PermitError, match="not permitted"):
        verify(verifier, permit, operation="enqueue", grant_expires_at=None)


def test_probe_has_independent_audience_and_complete_expected_digests(signed):
    signer, verifier = signed
    base = execution().model_dump(
        include={"issuer", "kid", "authorityId", "issuedAt", "expiresAt", "nonce"}
    )
    scope = {
        "authorityId": base["authorityId"],
        "probeId": COMMAND,
        "bindingRef": "binding",
        "target": {"kind": "node", "nodeId": "node", "nodeGeneration": 1},
        "expectedDigests": {name: DIGEST for name in ["bundle", "contract", "capabilities"]},
    }
    permit = sign_permit(
        BindingProbePermit(
            **base,
            **{k: v for k, v in scope.items() if k != "authorityId"},
            allowedOperations=["describe"],
        ),
        signer,
    )
    assert verifier.verify(
        permit, BindingProbePermit, operation="describe", expected=scope, now=NOW
    )
    with pytest.raises(ValueError):
        BindingProbePermit.model_validate({**permit.model_dump(), "expectedDigests": {}})
    with pytest.raises(PermitError):
        verifier.verify(permit, BindingProbePermit, operation="enqueue", expected=scope, now=NOW)


def test_callback_lifetime_and_recovery_are_more_restricted():
    base = execution().model_dump(
        exclude={"audience", "subjectRef", "payloadDigest", "dispatchEpoch"}
    )
    value = {
        **base,
        "groupId": "group",
        "teamRunId": "run",
        "memberId": "member",
        "bundleDigest": DIGEST,
        "allowedOperations": ["invoke"],
    }
    assert TeamsCallbackPermit.model_validate(value)
    with pytest.raises(ValueError):
        TeamsCallbackPermit.model_validate(
            {**value, "expiresAt": timestamp(NOW + timedelta(seconds=31))}
        )
    with pytest.raises(ValueError):
        TeamsCallbackPermit.model_validate({**value, "permitKind": "recovery"})
    assert TeamsCallbackPermit.model_validate(
        {**value, "permitKind": "recovery", "allowedOperations": ["append_effect_evidence"]}
    )


def test_leader_epoch_only_fences_operations_that_require_leader(signed):
    signer, verifier = signed
    permit = sign_permit(execution(leaderEpoch=1), signer)
    assert verify(verifier, permit)  # Ordinary members bind their own attempt epoch.
    with pytest.raises(PermitError, match="scope"):
        verify(verifier, permit, expected={**expected(permit), "leaderEpoch": 2})


def test_modified_model_instance_is_revalidated(signed):
    signer, verifier = signed
    permit = execution().model_copy(update={"allowedOperations": ["arbitrary"]})
    permit = sign_permit(permit, signer)
    with pytest.raises(PermitError, match="invalid Teams permit"):
        verifier.verify(
            permit, TeamsExecutionPermit, operation="arbitrary", expected=expected(permit), now=NOW
        )
