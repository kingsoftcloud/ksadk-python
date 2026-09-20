"""Export the exact Teams cloud contract schema, without starting a runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ksadk.plugins.teams.cloud_contracts import (
    canonical_bytes,
    contract_schemas,
    contract_versions,
)
from ksadk.plugins.teams.cloud_permits import PERMIT_MODELS


def export_contracts(output: Path) -> dict:
    schemas = contract_schemas()
    schemas.update({name: model.model_json_schema() for name, model in PERMIT_MODELS.items()})
    output.mkdir(parents=True, exist_ok=True)
    manifest = {**contract_versions(), "schemas": {}}
    for name, schema in sorted(schemas.items()):
        filename = name + ".schema.json"
        data = canonical_bytes(schema) + b"\n"
        (output / filename).write_bytes(data)
        manifest["schemas"][filename] = "sha256:" + hashlib.sha256(data).hexdigest()
    manifest["contractDigest"] = "sha256:" + hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = export_contracts(arguments.output)
    print(result["contractDigest"])
