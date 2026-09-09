"""Explicit E2B connection for dedicated, environment-isolated resource workers."""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from ksadk.sandbox.base import SandboxError


class ExplicitE2BConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    api_url: str = Field(strict=True)
    domain: str = Field(strict=True)
    api_key: SecretStr = Field(min_length=1, repr=False)

    @model_validator(mode="after")
    def validate_target(self):
        url = urlsplit(self.api_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or any(c.isspace() for c in self.api_url)
        ):
            raise ValueError("E2B API target must be an explicit HTTPS origin")
        if not re.fullmatch(r"[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9-]+)*", self.domain):
            raise ValueError("E2B domain must be an explicit hostname")
        return self

    def sdk_options(self) -> dict:
        """Credential-bearing arguments for SDK creation only, never for logs/archives.

        E2B 2.15.3 uses boolean-or defaults for debug and sandbox URL. Passing
        False/None cannot disable their env fallback; dedicated workers omit them.
        """
        contaminated = (
            "E2B_DEBUG",
            "E2B_SANDBOX_URL",
            "E2B_ACCESS_TOKEN",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
        if any(os.environ.get(name) for name in contaminated):
            raise SandboxError("E2B explicit connections require an isolated worker environment")
        return {
            "api_key": self.api_key.get_secret_value(),
            "api_url": self.api_url,
            "domain": self.domain,
            "debug": False,
            "request_timeout": 30,
        }
