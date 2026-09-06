"""DSH UI sandbox externals: vendored React CJS production builds for the
ModuleLoader shim in the sandbox document.

The sandbox document loads these as classic scripts before the plugin client
bundle. Each is a CommonJS module (`module.exports = ...`); the bootstrap's
require shim resolves them by name.

Loaded once per process from the react-ui node_modules; the content is
immutable per ksadk version (node_modules is pinned by package-lock).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

_REACT_UI_NODE_MODULES = (
    Path(__file__).parent / "react-ui" / "node_modules"
)

_EXTERNAL_FILES: Mapping[str, str] = MappingProxyType(
    {
        "react": "react/cjs/react.production.js",
        "react-jsx-runtime": "react/cjs/react-jsx-runtime.production.js",
        "scheduler": "scheduler/cjs/scheduler.production.js",
        "react-dom": "react-dom/cjs/react-dom.production.js",
        "react-dom-client": "react-dom/cjs/react-dom-client.production.js",
    }
)

_cache: dict[str, dict[str, str]] = {}


def read_dsh_ui_external(name: str) -> dict[str, str]:
    """Return {content, digest} for a vendored external, cached in-process."""
    if name in _cache:
        return _cache[name]
    relative = _EXTERNAL_FILES.get(name)
    if relative is None:
        raise KeyError(name)
    path = (_REACT_UI_NODE_MODULES / relative).resolve()
    if not path.is_relative_to(_REACT_UI_NODE_MODULES.resolve()):
        raise KeyError(name)
    raw = path.read_bytes()
    # Wrap the CJS production build as a self-executing IIFE that registers
    # into window.__DSH_EXTERNALS__. The sandbox CSP has no unsafe-eval, so
    # we cannot use new Function; the wrapper is a plain classic script whose
    # SRI the document pins. `require` resolves against already-registered
    # externals (load order is fixed by the document: react → scheduler →
    # react-dom → react-dom-client → react-jsx-runtime).
    wrapped = (
        b"(function(){var module={exports:{}},exports=module.exports,"
        b"require=window.__DSH_REQUIRE__;\n"
        + raw
        + b"\nwindow.__DSH_EXTERNALS__=window.__DSH_EXTERNALS__||{};"
        b'window.__DSH_EXTERNALS__["'
        + name.encode("ascii")
        + b'"]=module.exports;})();'
    )
    content = wrapped
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    entry = {"content": content, "digest": digest}
    _cache[name] = entry
    return entry


def dsh_ui_external_digests() -> Mapping[str, str]:
    """All known externals' digests, for CSP script-src pinning."""
    return MappingProxyType(
        {name: read_dsh_ui_external(name)["digest"] for name in _EXTERNAL_FILES}
    )
