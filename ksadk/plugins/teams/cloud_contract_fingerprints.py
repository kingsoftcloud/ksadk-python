"""Fingerprint the actual packaged cloud contracts, without filesystem manifests."""

from functools import lru_cache
from hashlib import sha256

from .cloud_contracts import (
    canonical_bytes,
    contract_schemas,
    contract_versions,
)
from .cloud_permits import PERMIT_MODELS


@lru_cache(maxsize=1)
def teams_cloud_contract_digest() -> str:
    schemas = contract_schemas()
    schemas.update({name: model.model_json_schema() for name, model in PERMIT_MODELS.items()})
    manifest = {**contract_versions(), "schemas": {}}
    for name, schema in sorted(schemas.items()):
        data = canonical_bytes(schema) + b"\n"
        manifest["schemas"][name + ".schema.json"] = "sha256:" + sha256(data).hexdigest()
    return "sha256:" + sha256(canonical_bytes(manifest)).hexdigest()
