"""Pure validation and projection of DSH profile and bundle manifests."""

from __future__ import annotations

from collections.abc import Mapping


def profile_dependencies(
    manifest: Mapping[str, object], *, error_type: type[Exception],
) -> dict[str, str]:
    raw = manifest.get("dependencies")
    if raw is None:
        return {}
    if not isinstance(raw, dict) or any(
        not isinstance(name, str) or not isinstance(value, str) for name, value in raw.items()
    ):
        raise error_type("DSH profile dependencies are invalid")
    return dict(raw)


def profile_bundles(manifest: Mapping[str, object], *, error_type: type[Exception]) -> list[str]:
    dsh = manifest.get("dsh")
    profile = dsh.get("profile") if isinstance(dsh, dict) else None
    bundles = profile.get("bundles") if isinstance(profile, dict) else None
    if bundles is None:
        return []
    if not isinstance(bundles, list) or any(not isinstance(item, str) for item in bundles):
        raise error_type("DSH profile bundle order is invalid")
    return list(bundles)


def bundle_patch(package: Mapping[str, object]) -> str | None:
    dsh = package.get("dsh")
    bundle = dsh.get("bundle") if isinstance(dsh, dict) else None
    patch = bundle.get("patch") if isinstance(bundle, dict) else None
    return patch.strip() if isinstance(patch, str) and patch.strip() else None
