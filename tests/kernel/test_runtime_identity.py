from __future__ import annotations

import sys
from types import ModuleType

from ksadk.kernel import runtime_identity as identity_module
from ksadk.version import VERSION


def test_runtime_identity_uses_imported_source_version_not_distribution_metadata(monkeypatch):
    bundle = ModuleType("ksadk._bundle_identity")
    bundle.BUNDLE_IDENTITY = {
        "ksadk_version": VERSION,
        "ksadk_commit": "a" * 40,
        "ksadk_source_digest": "b" * 64,
    }
    monkeypatch.setitem(sys.modules, "ksadk._bundle_identity", bundle)
    identity_module.runtime_identity.cache_clear()

    assert identity_module.runtime_identity() == {
        "ksadk_version": VERSION,
        "ksadk_commit": "a" * 40,
        "ksadk_source_digest": "b" * 64,
    }


def test_runtime_identity_rejects_stale_or_malformed_bundle_provenance(monkeypatch):
    bundle = ModuleType("ksadk._bundle_identity")
    bundle.BUNDLE_IDENTITY = {
        "ksadk_version": "0.0.0",
        "ksadk_commit": "not-a-commit",
        "ksadk_source_digest": "not-a-digest",
    }
    monkeypatch.setitem(sys.modules, "ksadk._bundle_identity", bundle)
    identity_module.runtime_identity.cache_clear()

    assert identity_module.runtime_identity() == {
        "ksadk_version": VERSION,
        "ksadk_commit": "",
        "ksadk_source_digest": "",
    }
