"""Protocol checks for every catalog; offline semantics for registered catalogs."""

import json
import re
from copy import deepcopy

from a2ui.core.processing import MessageProcessor, MessageProcessorOptions
from a2ui.core.schema.v0_9 import ServerToClientMessage
from a2ui.core.validation import ValidationConfig
from pydantic import TypeAdapter

from ksadk.a2ui.registry import current_catalog_registry
from ksadk.interaction.schema import require_json
from ksadk.a2ui.catalog import catalog_document

# Historical clients imported these before the application registry was introduced.
OFFICIAL_CATALOG_ID = catalog_document()["catalogId"]


def _official_catalog():
    return catalog_document()

MAX_OPERATIONS = 4096
MAX_BYTES = 1_048_576
OPERATIONS = ("createSurface", "updateComponents", "updateDataModel", "deleteSurface")
_protocol = TypeAdapter(ServerToClientMessage)


class A2uiWireState:
    """Atomic presentation cache; unknown catalogs remain opaque protocol data."""

    def __init__(self, registry=None):
        self.registry = registry or current_catalog_registry()
        self.history = []
        self.catalogs = {}

    def accept(self, messages):
        require_json(messages)
        if not isinstance(messages, list) or not messages:
            raise ValueError("A2UI requires a nonempty operation list")
        history = self.history + messages
        if len(history) > MAX_OPERATIONS or len(json.dumps(history).encode()) > MAX_BYTES:
            raise ValueError("A2UI presentation exceeds its operation budget")
        processor = MessageProcessor(self.registry.processor_catalogs(), options=MessageProcessorOptions(
            validation_config=ValidationConfig(
                allow_orphan_components=True, allow_dangling_references=True,
                allow_missing_root=True, allow_unknown_elements=False,
            )
        ))
        catalogs = {}
        try:
            for message in history:
                if not isinstance(message, dict) or message.get("version") not in {"v0.9", "v0.9.1"}:
                    raise ValueError("Unsupported A2UI protocol version")
                keys = [key for key in OPERATIONS if key in message]
                if len(keys) != 1:
                    raise ValueError("Invalid A2UI operation envelope")
                # Public upstream models validate the wire without inventing
                # a renderer for opaque component properties.
                _protocol.validate_python(message, strict=True)
                kind, detail = keys[0], message[keys[0]]
                sid = detail["surfaceId"]
                if not sid.strip():
                    raise ValueError("Invalid A2UI surface identity")
                if kind == "createSurface":
                    identity = detail["catalogId"]
                    if not identity.strip():
                        raise ValueError("Invalid A2UI catalog identity")
                    if sid in catalogs:
                        raise ValueError("A2UI surface already exists; delete before rebuilding")
                    catalogs[sid] = identity
                elif sid not in catalogs:
                    raise ValueError("A2UI surface has no accepted create operation")
                identity = catalogs[sid]
                if not self.registry.supports(identity, message["version"]):
                    raise ValueError("A2UI catalog does not support the wire version")
                if kind == "updateComponents":
                    items = detail["components"]
                    if not items or len(items) > 2048:
                        raise ValueError("Invalid or oversized A2UI component list")
                    ids = set()
                    for item in items:
                        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                                or not item["id"].strip() or item["id"] in ids
                                or not isinstance(item.get("component"), str) or not item["component"].strip()):
                            raise ValueError("Invalid or duplicate A2UI component identity")
                        ids.add(item["id"])
                    if identity in self.registry.catalog_ids:
                        self.registry.validate_components(identity, items)
                if kind == "updateDataModel":
                    path = detail.get("path")
                    if path is not None and (not isinstance(path, str) or path and not path.startswith("/")
                                             or re.search(r"~(?![01])", path)):
                        raise ValueError("Invalid A2UI JSON pointer")
                if identity in self.registry.catalog_ids:
                    processor.process_messages(deepcopy(message))
                if kind == "deleteSurface":
                    catalogs.pop(sid)
        finally:
            processor.model.dispose()
        self.history = deepcopy(history)
        self.catalogs = catalogs
        return deepcopy(messages)


def validate_operations(messages, *, registry=None):
    return A2uiWireState(registry).accept(messages)


__all__ = ["A2uiWireState", "validate_operations"]
