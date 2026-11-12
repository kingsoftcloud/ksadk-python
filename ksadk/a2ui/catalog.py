"""Pinned official assets and offline catalog validation, shared with Web."""

import json
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files

import rfc8785
from jsonschema import Draft202012Validator
from referencing import Registry, Resource


@lru_cache(maxsize=3)
def _asset(name):
    return json.loads(files("ksadk.a2ui").joinpath("assets", name).read_text())


def catalog_document():
    return deepcopy(_asset("basic-v0_9_1.json"))


def catalog_manifest():
    return deepcopy(_asset("manifest.json"))


def catalog_digest(document):
    return sha256(rfc8785.dumps(document)).hexdigest()


@lru_cache(maxsize=1)
def _validators():
    catalog = _asset("basic-v0_9_1.json")
    common = _asset("common-types-v0_9.json")
    registry = Registry().with_resources([
        (document["$id"], Resource.from_contents(document))
        for document in (catalog, common)
    ])
    for alias in catalog_manifest()["schema_resolution_aliases"]:
        registry = registry.with_resource(alias, Resource.from_contents({**catalog, "$id": alias}))
    return {
        name: Draft202012Validator(
            {"$ref": f"{catalog['$id']}#/components/{name}"}, registry=registry,
        ) for name in catalog["components"]
    }


def validate_components(components):
    if not isinstance(components, list) or len(components) > 2048:
        raise ValueError("Invalid or oversized A2UI component list")
    ids = set()
    for component in components:
        validate = (
            _validators().get(component.get("component")) if isinstance(component, dict) else None
        )
        if validate is None or not validate.is_valid(component):
            raise ValueError("A2UI component does not satisfy the registered catalog")
        identity = component.get("id")
        if not isinstance(identity, str) or identity in ids:
            raise ValueError("Invalid or duplicate A2UI component identity")
        ids.add(identity)


__all__ = ["catalog_digest", "catalog_document", "catalog_manifest", "validate_components"]
