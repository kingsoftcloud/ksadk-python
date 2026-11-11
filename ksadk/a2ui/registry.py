"""Application-owned offline catalogs. Client declarations never mutate these."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import lru_cache, wraps
from types import MappingProxyType

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import CannotDetermineSpecification
from referencing.jsonschema import UnknownDialect

from ksadk.a2ui.budget import MAX_CATALOG_RESOURCES, bounded_catalog_input
from ksadk.a2ui.catalog import _asset, catalog_digest, catalog_document, catalog_manifest
from ksadk.a2ui.legacy import legacy_catalog_document
from ksadk.a2ui.linking import link_catalog
from ksadk.interaction.schema import require_json


class A2uiCatalogRegistry:
    """Immutable snapshot of catalog JSON and optional offline Schema resources.

    Built-in identities cannot be replaced. Resources resolve Schema references
    without registering extra renderer identities or making network requests.
    """

    def __init__(self, catalogs: Sequence[Mapping] = (), *, resources: Sequence[Mapping] = (),
                 wire_versions: Sequence[str] = ("v0.9", "v0.9.1")):
        versions = frozenset(wire_versions)
        if not versions or not versions <= {"v0.9", "v0.9.1"}:
            raise ValueError("Unsupported A2UI catalog protocol version")
        if len(resources) > MAX_CATALOG_RESOURCES:
            raise ValueError("A2UI catalog exceeds its offline resource budget")
        documents = {}
        for raw in (catalog_document(), legacy_catalog_document(), *catalogs):
            document = bounded_catalog_input(dict(raw))
            document = deepcopy(document)
            require_json(document)
            identity = document.get("catalogId")
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError("A2UI catalog requires a nonempty catalogId")
            if not isinstance(document.get("components"), dict):
                raise ValueError("A2UI catalog requires component schemas")
            if identity in documents and documents[identity] != document:
                raise ValueError("Conflicting A2UI catalog identity")
            documents[identity] = document
        assets = {}
        for raw in (*documents.values(), _asset("common-types-v0_9.json"), *resources):
            document = bounded_catalog_input(dict(raw))
            document = deepcopy(document)
            require_json(document)
            identity = document.get("$id") or document.get("catalogId")
            if not isinstance(identity, str) or not identity:
                raise ValueError("Offline A2UI Schema resource requires $id")
            if identity in assets and assets[identity] != document:
                raise ValueError("Conflicting offline A2UI Schema resource")
            assets[identity] = document
        for alias, identity in catalog_manifest()["schema_resolution_aliases"].items():
            asset = {**documents[identity], "$id": alias}
            if alias in assets and assets[alias] != asset:
                raise ValueError("Conflicting offline A2UI Schema alias")
            assets[alias] = asset
        try:
            registry = Registry().with_resources(
                (identity, Resource.from_contents({"$schema": "https://json-schema.org/draft/2020-12/schema",
                                                   **document}))
                for identity, document in assets.items()
            )
        except (CannotDetermineSpecification, UnknownDialect, RecursionError) as exc:
            raise ValueError("Invalid offline A2UI schema resource") from exc
        validators = {}
        processor_documents = {}
        for identity, document in documents.items():
            base = document.get("$id") or identity
            for name, schema in document["components"].items():
                try:
                    Draft202012Validator.check_schema(schema)
                except (SchemaError, RecursionError) as exc:
                    raise ValueError("Invalid A2UI component schema") from exc
                validators[identity, name] = Draft202012Validator(
                    {"$ref": f"{base}#/components/{name}"}, registry=registry,
                )
            processor_documents[identity] = link_catalog(document, registry)
        self._documents = MappingProxyType(documents)
        self._resources = tuple(deepcopy(dict(resource)) for resource in resources)
        self._validators = MappingProxyType(validators)
        self._processor_documents = MappingProxyType(processor_documents)
        self._versions = versions
        self._digests = MappingProxyType({key: catalog_digest(value) for key, value in documents.items()})

    @property
    def catalog_ids(self):
        return tuple(self._documents)

    @property
    def resources(self):
        return deepcopy(list(self._resources))

    def document(self, catalog_id):
        document = self._documents.get(catalog_id)
        return deepcopy(document) if document is not None else None

    def describe(self, catalog_id):
        if catalog_id not in self._documents:
            return {"catalog_id": catalog_id, "catalog_validation": "protocol_only"}
        manifest = catalog_manifest()
        profile = (manifest["profile"] if catalog_id == manifest["catalog_id"] else
                   "ksadk-legacy-seven-components" if catalog_id == legacy_catalog_document()["catalogId"]
                   else "registered")
        return {"catalog_id": catalog_id, "catalog_validation": "full", "profile": profile,
                "catalog_digest": self._digests[catalog_id]}

    def validate_components(self, catalog_id, components):
        for component in components:
            validate = self._validators.get((catalog_id, component["component"]))
            if validate is None or not validate.is_valid(component):
                raise ValueError("A2UI component does not satisfy the registered catalog")

    def supports(self, catalog_id, version):
        if catalog_id in {catalog_manifest()["catalog_id"], legacy_catalog_document()["catalogId"]}:
            return version in {"v0.9", "v0.9.1"}
        return version in self._versions if catalog_id in self._documents else True

    def processor_catalogs(self):
        from a2ui.core.catalog import Catalog

        return [Catalog.from_json(deepcopy(document), protocol_version="0.9")
                for document in self._processor_documents.values()]


@lru_cache(maxsize=1)
def default_catalog_registry():
    return A2uiCatalogRegistry()


_current_registry: ContextVar[A2uiCatalogRegistry | None] = ContextVar("ksadk_a2ui_catalogs", default=None)


def current_catalog_registry():
    return _current_registry.get() or default_catalog_registry()


@contextmanager
def catalog_scope(registry):
    token = _current_registry.set(registry)
    try:
        yield
    finally:
        _current_registry.reset(token)


def catalog_bound(method):
    """Scope native reads and model/tool tasks to their owning adapter."""
    @wraps(method)
    async def invoke(self, *args, **kwargs):
        with catalog_scope(self.a2ui_catalog_registry):
            return await method(self, *args, **kwargs)
    return invoke


__all__ = ["A2uiCatalogRegistry"]
