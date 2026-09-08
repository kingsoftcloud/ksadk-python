"""Single frozen descriptor host for wheel-owned DSH AgentProviders.

The provider key is a fixed command argument selected by KsADK.  The host
owns discovery and lifecycle only; model credentials and execution services
remain in the parent RuntimeAdapter bridge.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Sequence

from ksadk.plugins.providers.dsh import (
    DSH_AGENT_PROVIDER_HOST_METHODS,
    DSH_AGENT_PROVIDER_HOST_PROTOCOL,
    DshAgentProviderDescriptor,
)
from ksadk.plugins.providers.shipped_dsh import (
    SHIPPED_DSH_PROVIDER_SPECS,
    ShippedDshProviderSpec,
)


class HostRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class DescriptorHostState:
    spec: ShippedDshProviderSpec
    profile: dict[str, Any] | None = None
    descriptor: DshAgentProviderDescriptor | None = None
    state: str = "ready"

    def handshake(self, params: dict[str, Any]) -> dict[str, Any]:
        projection = self._profile(params)
        bundles = projection.get("bundles")
        if not isinstance(bundles, list) or self.spec.package_name not in bundles:
            raise HostRequestError(
                self.spec.error_code("bundle_inactive"),
                f"{self.spec.display_name} Bundle is not active in the selected DSH Profile",
            )
        if self.profile is not None and (
            projection.get("configDigest") != self.profile.get("configDigest")
        ):
            raise HostRequestError(
                self.spec.error_code("profile_changed"),
                "host cannot cross a DSH Profile fence",
            )
        self.profile = projection
        self.descriptor = DshAgentProviderDescriptor.model_validate(
            {
                "providerId": self.spec.provider_id,
                "providerVersion": self.spec.version,
                "displayName": self.spec.display_name,
                "pluginName": self.spec.package_name,
                "profile": projection.get("profile"),
                "profileDigest": projection.get("configDigest"),
                "runtimeProtocols": ["agentkit.runtime/v1"],
            }
        )
        return {
            "protocolVersion": DSH_AGENT_PROVIDER_HOST_PROTOCOL,
            "methods": sorted(DSH_AGENT_PROVIDER_HOST_METHODS),
            "hostVersion": self.spec.version,
        }

    def describe(self, params: dict[str, Any]) -> dict[str, Any]:
        descriptor = self._require_ready_descriptor()
        if self._profile(params).get("configDigest") != descriptor.profile_digest:
            raise HostRequestError(
                self.spec.error_code("profile_mismatch"),
                "descriptor crossed a DSH Profile fence",
            )
        return descriptor.model_dump(by_alias=True, mode="json")

    def preflight(self, params: dict[str, Any]) -> dict[str, Any]:
        descriptor = self._require_ready_descriptor()
        self._check_fences(params)
        return {
            "ready": self.state == "ready",
            "descriptorDigest": descriptor.descriptor_digest,
            "profileDigest": descriptor.profile_digest,
        }

    def inventory(self, params: dict[str, Any]) -> dict[str, Any]:
        descriptor = self._require_descriptor()
        self._check_fences(params, profile_optional=True)
        return {
            "providerId": descriptor.provider_id,
            "providerVersion": descriptor.provider_version,
            "profile": descriptor.profile,
            "profileDigest": descriptor.profile_digest,
            "descriptorDigest": descriptor.descriptor_digest,
            "state": self.state,
            "activationCount": 0,
        }

    def health(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("activationId"):
            return {"healthy": False}
        return {"healthy": self.state == "ready"}

    def drain(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("scope") != "provider":
            raise self._execution_bridge_required()
        self.state = "draining"
        return {"ok": True}

    def dispose(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("scope") != "host":
            raise self._execution_bridge_required()
        self.state = "disposed"
        return {"ok": True}

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, method, None)
        if method in {
            "handshake",
            "describe",
            "preflight",
            "inventory",
            "health",
            "drain",
            "dispose",
        } and callable(handler):
            return handler(params)
        raise self._execution_bridge_required()

    def _profile(self, params: dict[str, Any]) -> dict[str, Any]:
        value = params.get("profile")
        if not isinstance(value, dict):
            raise HostRequestError(
                self.spec.error_code("profile_invalid"),
                "request requires a DSH Profile projection",
            )
        return value

    def _require_descriptor(self) -> DshAgentProviderDescriptor:
        if self.descriptor is None:
            raise HostRequestError(
                self.spec.error_code("handshake_required"),
                "DSH Profile handshake is incomplete",
            )
        return self.descriptor

    def _require_ready_descriptor(self) -> DshAgentProviderDescriptor:
        descriptor = self._require_descriptor()
        if self.state != "ready":
            raise HostRequestError(
                self.spec.error_code("provider_unavailable"),
                f"{self.spec.display_name} DSH provider is not ready",
            )
        return descriptor

    def _check_fences(self, params: dict[str, Any], *, profile_optional: bool = False) -> None:
        descriptor = self._require_descriptor()
        if not profile_optional and params.get("profileDigest") != descriptor.profile_digest:
            raise HostRequestError(
                self.spec.error_code("profile_mismatch"),
                "request crossed a DSH Profile fence",
            )
        if params.get("descriptorDigest") != descriptor.descriptor_digest:
            raise HostRequestError(
                self.spec.error_code("descriptor_mismatch"),
                "request crossed a descriptor fence",
            )

    def _execution_bridge_required(self) -> HostRequestError:
        return HostRequestError(
            self.spec.bridge_required_code,
            self.spec.bridge_required_message,
        )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1 or arguments[0] not in SHIPPED_DSH_PROVIDER_SPECS:
        return 2
    spec = SHIPPED_DSH_PROVIDER_SPECS[arguments[0]]
    state = DescriptorHostState(spec)
    for line in sys.stdin:
        request: dict[str, Any] | None = None
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise HostRequestError(
                    spec.error_code("request_invalid"), "request must be an object"
                )
            request = value
            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}
            if (
                not isinstance(request_id, str)
                or method not in DSH_AGENT_PROVIDER_HOST_METHODS
                or not isinstance(params, dict)
            ):
                raise HostRequestError(
                    spec.error_code("request_invalid"), "request envelope is invalid"
                )
            response = {"id": request_id, "result": state.dispatch(str(method), params)}
        except Exception as error:  # protocol boundary must always answer
            response = {
                "id": request.get("id") if isinstance(request, dict) else "unknown",
                "error": {
                    "code": getattr(error, "code", spec.error_code("internal_error")),
                    "message": str(error),
                },
            }
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        if state.state == "disposed":
            break
    return 0


if __name__ == "__main__":  # pragma: no cover - subprocess entrypoint
    raise SystemExit(main())


__all__ = ["DescriptorHostState", "HostRequestError", "main"]
