"""OAuth HTTP adapter with explicit trusted issuer and resource configuration."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field, replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx


class OAuthError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


MAX_REFRESH_LEAD = 30


def refresh_due(expires_at, token, now):
    """Use at most a tenth of the issued lifetime as a refresh lead."""
    lead = min(MAX_REFRESH_LEAD, float(token.get("expires_in", 300)) / 10) if token.get("refresh_token") else 0
    return expires_at <= now + lead


def metadata_url(authority):
    parts = urlsplit(authority)
    if parts.query or parts.fragment or parts.username or parts.password:
        raise OAuthError("OAUTH_UNTRUSTED_ENDPOINT")
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            "/.well-known/oauth-authorization-server" + parts.path.rstrip("/"),
            "",
            "",
        )
    )


def is_ksyun_authority(authority):
    return bool(re.fullmatch(r"https://iam\.api\.ksyun\.com/id/[A-Za-z0-9_-]+", authority))


@dataclass(frozen=True)
class MCPConnection:
    id: str
    resource: str
    issuer: str
    redirect_uri: str
    scopes: tuple[str, ...] = ("mcp",)
    version: str = "1"
    authorization_metadata_url: str = ""
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    token_endpoint_auth_method: str = "none"
    allow_loopback: bool = False
    allowed_tools: tuple[str, ...] = ()
    protected_resource_metadata_url: str = ""
    authorization_server: str = ""
    tool_policies: dict = field(default_factory=dict)
    display_name: str = ""

    def __post_init__(self):
        if self.token_endpoint_auth_method not in {
            "none",
            "client_secret_basic",
            "client_secret_post",
        }:
            raise ValueError("OAUTH_CLIENT_INCOMPATIBLE")
        for value in (self.resource, self.issuer, self.redirect_uri):
            self.validate_url(value)

    def registration_redirect_uri(self) -> str:
        """The hosted Ksyun registrar returns one official client for multiple Agents."""
        if self.client_id or not is_ksyun_authority(self.authorization_server):
            return self.redirect_uri
        url = urlsplit(self.redirect_uri)
        prefix = "/agentengine/oauth/mcp/callback/"
        if url.scheme != "https" or url.query or url.path != prefix + self.id:
            return self.redirect_uri
        # One public callback per trusted IAM issuer. The transaction still binds
        # the individual user, Agent, resource, PKCE verifier and browser cookie.
        binding = hashlib.sha256(b"https://iam.api.ksyun.com").hexdigest()
        return urlunsplit((url.scheme, url.netloc, prefix + binding, "", ""))

    def validate_url(self, url: str, *, issuer_only: bool = False):
        parsed = urlsplit(url)
        if (
            parsed.username
            or parsed.password
            or parsed.fragment
            or not parsed.hostname
            or (
                parsed.scheme != "https"
                and not (
                    self.allow_loopback
                    and parsed.scheme == "http"
                    and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                )
            )
        ):
            raise OAuthError("OAUTH_UNTRUSTED_ENDPOINT")
        if issuer_only and (parsed.scheme, parsed.netloc) != (
            urlsplit(self.issuer).scheme,
            urlsplit(self.issuer).netloc,
        ):
            raise OAuthError("OAUTH_UNTRUSTED_ENDPOINT")


class OAuthProtocol:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def discover_resource(self, config, *, check_authority=True):
        try:
            response = await self.http.get(config.resource)
            challenge = response.headers.get("WWW-Authenticate", "")
            matches = re.findall(r'resource_metadata="([^"\s]+)"', challenge)
            url = config.protected_resource_metadata_url or (
                matches[0] if len(matches) == 1 else ""
            )
            if not url:
                parts = urlsplit(config.resource)
                url = urlunsplit(
                    (
                        parts.scheme,
                        parts.netloc,
                        "/.well-known/oauth-protected-resource" + parts.path,
                        "",
                        "",
                    )
                )
            config.validate_url(url)
            if urlsplit(url).netloc != urlsplit(config.resource).netloc:
                raise OAuthError("OAUTH_UNTRUSTED_ENDPOINT")
            metadata = await self.http.get(url)
            data = metadata.json()
            if (
                metadata.status_code != 200
                or not isinstance(data, dict)
                or data.get("resource") != config.resource
                or (
                    check_authority
                    and (config.authorization_server or config.issuer)
                    not in data.get("authorization_servers", [])
                )
            ):
                raise OAuthError("OAUTH_RESOURCE_MISMATCH")
            return data
        except (httpx.TransportError, ValueError, KeyError, TypeError):
            raise OAuthError("OAUTH_DISCOVERY_FAILED") from None

    async def metadata(self, config: MCPConnection) -> dict:
        await self.discover_resource(config)
        # Explicit metadata URL accommodates a resource-specific IAM discovery
        # path while still validating the returned issuer exactly.
        url = config.authorization_metadata_url or metadata_url(
            config.authorization_server or config.issuer
        )
        config.validate_url(url, issuer_only=True)
        try:
            response = await self.http.get(url)
            data = response.json()
        except (httpx.TransportError, ValueError):
            raise OAuthError("OAUTH_DISCOVERY_FAILED") from None
        if response.status_code != 200:
            raise OAuthError("OAUTH_DISCOVERY_FAILED")
        return self.validate_metadata(config, data)

    @staticmethod
    def validate_metadata(config, data):
        accepted_issuers = {config.issuer}
        if is_ksyun_authority(config.authorization_server):
            accepted_issuers = {config.authorization_server, "https://iam.api.ksyun.com"}
        if (
            not isinstance(data, dict)
            or data.get("issuer") not in accepted_issuers
            or not isinstance(data.get("code_challenge_methods_supported"), list)
            or "S256" not in data["code_challenge_methods_supported"]
        ):
            raise OAuthError("OAUTH_CLIENT_INCOMPATIBLE")
        for key in ("authorization_endpoint", "token_endpoint"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise OAuthError("OAUTH_CLIENT_INCOMPATIBLE")
            config.validate_url(data[key], issuer_only=True)
        for key in ("registration_endpoint", "revocation_endpoint"):
            if data.get(key):
                config.validate_url(data[key], issuer_only=True)
        if is_ksyun_authority(config.authorization_server):
            # A narrowly pinned compatibility profile, not an issuer bypass.
            if config.issuer not in {config.authorization_server, "https://iam.api.ksyun.com"}:
                raise OAuthError("OAUTH_ISSUER_MISMATCH")
            origin = "https://iam.api.ksyun.com"
            expected = {
                "authorization_endpoint": origin + "/oauth/authorize",
                "token_endpoint": origin + "/oauth/token",
                "registration_endpoint": origin
                + "/oauth/register"
                + urlsplit(config.authorization_server).path,
                "revocation_endpoint": origin + "/oauth/revoke",
            }
            if any(data.get(k) and data[k] != v for k, v in expected.items()):
                raise OAuthError("OAUTH_UNTRUSTED_ENDPOINT")
        return data

    @staticmethod
    def candidate(config_id, declaration, redirect_uri, *, version, allow_loopback=False):
        options = declaration["oauth"]
        candidate = MCPConnection(
            id=config_id,
            resource=declaration["url"],
            issuer=declaration["url"],
            redirect_uri=redirect_uri,
            scopes=tuple(options["scopes"]),
            version=version,
            client_id=options.get("client_id", ""),
            client_secret=options.get("client_secret", ""),
            token_endpoint_auth_method=options.get("token_endpoint_auth_method", "none"),
            allowed_tools=tuple(declaration.get("tool_filter", [])),
            display_name=declaration["name"],
            tool_policies=declaration.get("tool_policies", {}),
            allow_loopback=allow_loopback,
        )
        return candidate

    @staticmethod
    def bind_resource(candidate, resource):
        if not isinstance(resource, dict) or resource.get("resource") != candidate.resource:
            raise OAuthError("OAUTH_RESOURCE_MISMATCH")
        authorities = resource.get("authorization_servers")
        if (
            not isinstance(authorities, list)
            or len(authorities) != 1
            or not isinstance(authorities[0], str)
        ):
            raise OAuthError("OAUTH_AUTHORIZATION_SERVER_AMBIGUOUS")
        authority = authorities[0]
        candidate.validate_url(authority)
        issuer = authority
        candidate = replace(
            candidate,
            issuer=issuer,
            authorization_server=authority,
            authorization_metadata_url=metadata_url(authority),
        )
        return candidate

    async def resolve_connection(
        self, config_id, declaration, redirect_uri, *, version, allow_loopback=False
    ):
        candidate = self.candidate(config_id, declaration, redirect_uri,
                                   version=version, allow_loopback=allow_loopback)
        resource = await self.discover_resource(candidate, check_authority=False)
        candidate = self.bind_resource(candidate, resource)
        metadata = await self.metadata(candidate)
        return replace(candidate, issuer=metadata["issuer"], redirect_uri=candidate.registration_redirect_uri())

    async def register(self, config: MCPConnection, metadata: dict) -> dict:
        if config.client_id:
            return {
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "token_endpoint_auth_method": config.token_endpoint_auth_method,
            }
        if not metadata.get("registration_endpoint"):
            raise OAuthError("OAUTH_CLIENT_INCOMPATIBLE")
        try:
            response = await self.http.post(
                metadata["registration_endpoint"],
                json={
                    "client_name": "AgentEngine MCP",
                    "redirect_uris": [config.redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": config.token_endpoint_auth_method,
                    "scope": " ".join(config.scopes),
                },
            )
            client = response.json()
        except (httpx.TransportError, ValueError):
            raise OAuthError("OAUTH_CLIENT_INCOMPATIBLE") from None
        if response.status_code not in {200, 201}:
            raise OAuthError("OAUTH_CLIENT_INCOMPATIBLE")
        return self.validate_client(config, client)

    @staticmethod
    def validate_client(config, client):
        if (
            not isinstance(client, dict)
            or not isinstance(client.get("client_id"), str)
            or not client["client_id"]
            or client.get("token_endpoint_auth_method", "none") != config.token_endpoint_auth_method
            or (config.token_endpoint_auth_method != "none" and not client.get("client_secret"))
        ):
            raise OAuthError("OAUTH_CLIENT_INCOMPATIBLE")
        if config.redirect_uri not in client.get("redirect_uris", [config.redirect_uri]):
            raise OAuthError("OAUTH_CLIENT_INCOMPATIBLE")
        return client

    def authorization_url(self, config, metadata, client, state, verifier):
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        parameters = {
            "client_id": client["client_id"],
            "response_type": "code",
            "redirect_uri": config.redirect_uri,
            "resource": config.resource,
            "scope": " ".join(config.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        endpoint = urlsplit(metadata["authorization_endpoint"])
        if any(name in parameters for name, _ in parse_qsl(endpoint.query, keep_blank_values=True)):
            raise OAuthError("OAUTH_CLIENT_INCOMPATIBLE")
        query = urlencode(parameters)
        if endpoint.query:
            query = endpoint.query + "&" + query
        return endpoint._replace(query=query).geturl()

    async def token(self, config, metadata, client, data):
        data = {**data, "client_id": client["client_id"], "resource": config.resource}
        kwargs = {}
        if config.token_endpoint_auth_method == "client_secret_basic":
            kwargs["auth"] = httpx.BasicAuth(client["client_id"], client["client_secret"])
        elif config.token_endpoint_auth_method == "client_secret_post":
            data["client_secret"] = client["client_secret"]
        try:
            response = await self.http.post(metadata["token_endpoint"], data=data, **kwargs)
        except httpx.TransportError:
            raise OAuthError("TOKEN_EXCHANGE_OUTCOME_UNKNOWN") from None
        if response.status_code != 200:
            raise OAuthError("TOKEN_EXCHANGE_FAILED")
        try:
            token = response.json()
        except ValueError:
            raise OAuthError("TOKEN_EXCHANGE_FAILED") from None
        return self.validate_token(config, token)

    @staticmethod
    def validate_token(config, token):
        try:
            expires = float(token.get("expires_in", 300))
            if not 0 < expires <= 365 * 86400:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise OAuthError("TOKEN_EXCHANGE_FAILED") from None
        if not token.get("access_token") or str(token.get("token_type", "")).lower() != "bearer":
            raise OAuthError("TOKEN_EXCHANGE_FAILED")
        if not isinstance(token.get("scope", ""), str) or not set(config.scopes).issubset(
            set(token.get("scope", " ".join(config.scopes)).split())
        ):
            raise OAuthError("INSUFFICIENT_SCOPE")
        if is_ksyun_authority(config.authorization_server):
            granted = set(token.get("scope", " ".join(config.scopes)).split())
            if not granted <= set(config.scopes) | {"ksc/openid"}:
                raise OAuthError("OAUTH_UNEXPECTED_SCOPE")
        return token

    async def revoke(self, config, metadata, client, tokens):
        if not metadata.get("revocation_endpoint"):
            return
        for name in ("refresh_token", "access_token"):
            if not tokens.get(name):
                continue
            data = {
                "client_id": client["client_id"],
                "token": tokens[name],
                "token_type_hint": name,
            }
            kwargs = {}
            if config.token_endpoint_auth_method == "client_secret_basic":
                kwargs["auth"] = httpx.BasicAuth(client["client_id"], client["client_secret"])
            elif config.token_endpoint_auth_method == "client_secret_post":
                data["client_secret"] = client["client_secret"]
            try:
                response = await self.http.post(
                    metadata["revocation_endpoint"], data=data, **kwargs
                )
                if response.status_code != 200:
                    raise OAuthError("REVOCATION_FAILED")
            except httpx.TransportError:
                raise OAuthError("REVOCATION_OUTCOME_UNKNOWN") from None
