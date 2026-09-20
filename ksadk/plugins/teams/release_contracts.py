"""Frozen build/release evidence wire DTOs; no storage or execution authority."""

from typing import Literal

from pydantic import Field

from .build_artifacts import TeamsBuildManifest
from .cloud_contracts import Digest, Identifier, OperationKey, Timestamp, WireModel

BUILD_VERSION = "teams-build/v1"
LOADED_BUILD_VERSION = "teams-loaded-build/v1"


class ReleaseVerifyInput(WireModel):
    buildArtifactRef: Identifier
    localBindingRef: Identifier
    cloudBindingRef: Identifier
    idempotencyKey: OperationKey


class DeploymentArtifactReceipt(WireModel):
    agentId: Identifier
    versionId: Identifier
    instanceId: Identifier
    codeArtifactDigest: Digest
    buildManifestDigest: Digest
    bundleDigest: Digest
    contractDigest: Digest
    deployedAt: str


class BuildArtifactReceipt(WireModel):
    buildArtifactRef: Identifier
    state: Literal["verified"]
    authorityId: Identifier
    codeArtifactDigest: Digest
    buildManifestDigest: Digest
    sizeBytes: int = Field(strict=True, ge=1, le=20 * 1024 * 1024)
    manifest: TeamsBuildManifest


class ReleaseVerificationReceipt(WireModel):
    releaseRef: Identifier
    buildArtifactRef: Identifier
    localBindingRef: Identifier
    cloudBindingRef: Identifier
    evidenceDigest: Digest
    verifiedAt: Timestamp


class BuildArtifactLookupInput(WireModel):
    idempotencyKey: OperationKey


class MissingBuildArtifact(WireModel):
    status: Literal["missing"]


class RecordedBuildArtifact(BuildArtifactReceipt):
    status: Literal["recorded"]
