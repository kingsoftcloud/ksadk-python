"""Optional A2UI decoration cannot replace the authoritative business schema."""

import logging
import re
from copy import deepcopy

from ksadk.a2ui.catalog import catalog_manifest
from ksadk.a2ui.legacy import legacy_catalog_document
from ksadk.a2ui.registry import A2uiCatalogRegistry, current_catalog_registry
from ksadk.a2ui.wire import A2uiWireState

logger = logging.getLogger(__name__)


def build_a2ui_presentation(
    messages: list[dict],
    *,
    title: str = "补充信息",
    description: str | None = None,
    catalog_registry: A2uiCatalogRegistry | None = None,
    catalog_digest: str | None = None,
) -> dict:
    """Build an interaction display from a complete, single-catalog snapshot.

    This helper does not interrupt or run business tools. Registered catalogs
    supply their authoritative digest; an unknown frontend-owned catalog must
    supply its declared digest and receives protocol-only validation.
    """
    from ksadk.interaction.contracts import InteractionPresentation

    registry = catalog_registry or current_catalog_registry()
    messages = deepcopy(messages)
    state = A2uiWireState(registry)
    state.accept(messages)
    identities = {message["createSurface"]["catalogId"] for message in messages
                  if "createSurface" in message}
    if len(identities) != 1:
        raise ValueError("Interaction presentation requires exactly one catalog")
    actual = registry.describe(next(iter(identities))).get("catalog_digest")
    if actual is not None and catalog_digest not in (None, actual):
        raise ValueError("A2UI catalog digest mismatch")
    digest = actual or catalog_digest
    if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest):
        raise ValueError("Register the A2UI catalog or supply its declared digest")
    return InteractionPresentation(title=title, description=description, a2ui={
        "wire_version": "0.9.1", "catalog_digest": digest, "messages": messages,
    }).model_dump(mode="json", exclude_none=True)


def prepare_presentation(presentation, *, registry=None):
    result = deepcopy(presentation)
    a2ui = result.get("a2ui")
    if a2ui is None:
        return result
    try:
        if not isinstance(a2ui, dict) or a2ui.get("wire_version") != "0.9.1":
            raise ValueError("Unsupported A2UI presentation version")
        digest = a2ui.get("catalog_digest")
        if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest):
            raise ValueError("Invalid A2UI catalog digest")
        registry = registry or current_catalog_registry()
        state = A2uiWireState(registry)
        state.accept(a2ui.get("messages"))
        identities = {message["createSurface"]["catalogId"] for message in a2ui["messages"]
                      if "createSurface" in message}
        if len(identities) != 1:
            raise ValueError("Interaction presentation requires exactly one catalog")
        description = registry.describe(next(iter(identities)))
        # Earlier SDKs stamped the official digest on validated legacy input
        # presentations. Retain only that explicit historical compatibility.
        legacy_digest = (description["catalog_id"] == legacy_catalog_document()["catalogId"]
                         and digest == catalog_manifest()["catalog_digest"])
        if description.get("catalog_digest", digest) != digest and not legacy_digest:
            raise ValueError("A2UI catalog digest mismatch")
    except Exception as exc:
        # Neither invalid card data nor validator details become chat output.
        logger.warning("A2UI presentation rejected; using Schema form (%s)", type(exc).__name__)
        result.pop("a2ui", None)
    return result
