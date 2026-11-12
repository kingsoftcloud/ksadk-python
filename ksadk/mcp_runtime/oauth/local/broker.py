"""Single-user local OAuth client, independent of platform identity/admission.

No API accepts a token or a claimed user identity. The live local Kernel owns
the task binding; browser APIs expose only safe wait projections and tickets.
"""

import asyncio
import secrets
import time
from dataclasses import asdict

import httpx

from ..client import BrokerError
from ..contract import CAPABILITY
from ..security import digest, sign
from .protocol import MCPConnection, OAuthError, OAuthProtocol, refresh_due
from .transport import PublicOAuthTransport

TERMINAL = {"cancelled", "expired", "failed", "resumed"}


class LocalBroker:
    def __init__(self, vault, runtime, declarations, *, http=None, clock=time.time):
        self.vault, self.runtime, self.clock = vault, runtime, clock
        self.data = vault.data
        self.declarations = {d["name"]: d for d in declarations}
        self.http = http or httpx.AsyncClient(
            transport=PublicOAuthTransport(), timeout=20, follow_redirects=False, trust_env=False
        )
        self.protocol = OAuthProtocol(self.http)
        self.lock = asyncio.Lock()
        self.extension = None
        self.base_url = ""
        self.tickets = {}
        # A crash while exchanging/refreshing must never replay a one-use code.
        for tx in self.data["transactions"].values():
            if tx["status"] == "exchanging":
                tx["status"] = "unknown"
                wait = self.data["waits"][tx["wait_id"]]
                if wait["status"] not in TERMINAL:
                    self.change(wait, "authorization_failed", "TOKEN_EXCHANGE_OUTCOME_UNKNOWN")
        for grant in self.data["grants"].values():
            if grant["status"] == "refreshing":
                grant["status"] = "reauthorization_required"
        self.vault.save()

    async def run(self, run_id):
        run = await self.runtime.kernel_store.load_run(run_id)
        if run is None or run.agent_instance_id != self.runtime.config.agent_instance_id:
            raise BrokerError("TASK_BINDING_MISMATCH")
        return run

    def change(self, wait, status, error=None):
        if wait["status"] != status or wait.get("error") != error:
            wait.update(status=status, error=error, revision=wait["revision"] + 1)

    def wait(self, wait_id):
        wait = self.data["waits"].get(wait_id)
        if not wait:
            raise BrokerError("WAIT_NOT_FOUND")
        if wait["status"] not in TERMINAL and self.clock() >= wait["deadline"]:
            self.change(wait, "expired", "WAIT_EXPIRED")
        return wait

    def config(self, wait):
        return MCPConnection(**self.data["configs"][wait["config_id"]])

    def view(self, wait):
        config = self.config(wait)
        actions = ["query"]
        if wait["runtime_ref"] and wait["status"] not in TERMINAL:
            actions.append("cancel")
            if wait["status"] in {"awaiting_authorization", "authorization_failed"}:
                actions += ["open", "retry_authorization"]
            if wait["status"] == "connection_failed":
                actions.append("retry_connection")
        return dict(
            schema="mcp.authorization/v1",
            capability_digest=CAPABILITY["digest"],
            wait_id=wait["wait_id"],
            run_id=wait["run_id"],
            config_id=wait["config_id"],
            display_name=config.display_name,
            status=wait["status"],
            revision=wait["revision"],
            deadline=wait["deadline"],
            error=wait.get("error"),
            allowed_actions=actions,
            requires_resume=bool(wait["runtime_ref"]),
            scopes=list(config.scopes),
        )

    async def notify(self, wait):
        if (
            not self.extension
            or not wait["runtime_ref"]
            or wait["status"]
            not in {
                "credential_ready",
                "cancelled",
                "expired",
            }
        ):
            return
        if await self.extension.row(wait["wait_id"]) is None:
            return  # native wait has not committed yet; next poll retries.
        command = dict(
            schema="mcp.authorization/v1",
            operation="resolve_external_authorization",
            wait_id=wait["wait_id"],
            run_id=wait["run_id"],
            session_id=wait["session_id"],
            instance_id=self.runtime.config.agent_instance_id,
            revision=wait["revision"],
            status=wait["status"],
        )
        permit = sign(
            {"body_digest": digest(command), "sub": command["instance_id"]},
            self.extension.signing_key,
            issuer=self.extension.issuer,
            audience="mcp-authorization-runtime",
        )
        await self.extension.accept(command, permit)

    async def ensure(self, run_id, name, request_key, *, declaration=None, lookup=False, rejected_grant_version=None, **_):
        run = await self.run(run_id)
        async with self.lock:
            # Replay uses the original configuration, even after project edits.
            wait_id = digest([run_id, name, request_key])
            if wait_id in self.data["waits"]:
                wait = self.wait(wait_id)
                self.vault.save()
                return {"status": "resume_wait"} if lookup else self.view(wait)
            declaration = self.declarations.get(name) or declaration
            if declaration is None:
                raise BrokerError("MCP_CONFIGURATION_NOT_FOUND")
            if declaration["oauth"].get("client_ref"):
                raise BrokerError("LOCAL_OAUTH_PLATFORM_CLIENT_REF_UNSUPPORTED")
            version = digest(declaration)
            config_id = digest([name, version, self.base_url])
            if config_id not in self.data["configs"]:
                try:
                    config = await self.protocol.resolve_connection(
                        config_id,
                        declaration,
                        self.base_url + "/agentengine/oauth/mcp/callback",
                        version=version,
                        allow_loopback=True,
                    )
                except OAuthError as exc:
                    raise BrokerError(exc.code) from None
                self.data["configs"][config_id] = asdict(config)
            config = MCPConnection(**self.data["configs"][config_id])
            await self.refresh(config_id, config)
            grant = self.data["grants"].get(config_id, {})
            if (
                rejected_grant_version is not None
                and grant.get("version") == rejected_grant_version
            ):
                grant["status"] = "reauthorization_required"
                self.vault.save()
            ready = grant.get("status") == "active" and not refresh_due(grant["expires"], grant["tokens"], self.clock())
            if lookup:
                if ready:
                    return {"status": "authorized", "credential": self._credential(grant, config)}
                return {"status": "authorization_required"}
            wait = dict(
                wait_id=wait_id,
                run_id=run_id,
                session_id=run.session_id,
                agent_id=(run.metadata.get("start_request") or {}).get("agent_id"),
                config_id=config_id,
                status="credential_ready" if ready else "preparing",
                revision=1,
                deadline=self.clock() + 600,
                runtime_ref=None,
                error=None,
            )
            self.data["waits"][wait_id] = wait
            self.vault.save()
            return self.view(wait)

    async def get(self, wait_id):
        async with self.lock:
            wait = self.wait(wait_id)
            await self.run(wait["run_id"])
            self.vault.save()
            await self.notify(wait)
            return self.view(wait)

    async def confirm(
        self, wait_id, runtime_ref=None, *, phase=None, error=None, rejected_grant_version=None,
    ):
        async with self.lock:
            wait = self.wait(wait_id)
            if wait["status"] not in TERMINAL:
                if phase:
                    if phase not in {
                        "connecting",
                        "connection_failed",
                        "authorization_failed",
                        "resume_unknown",
                        "resumed",
                    } or wait["status"] not in {
                        "credential_ready",
                        "connecting",
                        "connection_failed",
                    }:
                        raise BrokerError("INVALID_WAIT_TRANSITION")
                    if phase == "authorization_failed":
                        grant = self.data["grants"].get(wait["config_id"])
                        if grant and grant["status"] == "active":
                            if grant["version"] == rejected_grant_version:
                                # Retain the version counter through reauthorization.
                                grant["status"] = "reauthorization_required"
                            elif rejected_grant_version is not None or grant["expires"] > self.clock():
                                return self.view(wait)
                    safe_errors = {
                        "CONNECTION_FAILED",
                        "BUSINESS_PERMISSION_DENIED",
                        "INSUFFICIENT_SCOPE",
                        "TOOLS_EMPTY",
                        "REAUTHORIZATION_REQUIRED",
                    }
                    if error and error not in safe_errors:
                        error = "CONNECTION_FAILED"
                    self.change(
                        wait,
                        phase,
                        "RESUME_OUTCOME_UNKNOWN" if phase == "resume_unknown" else error,
                    )
                else:
                    if not runtime_ref or not all(
                        runtime_ref.get(k) for k in ("checkpoint_id", "thread_id", "interrupt_id")
                    ):
                        raise BrokerError("WAIT_NOT_DURABLE")
                    if wait["runtime_ref"] and wait["runtime_ref"] != runtime_ref:
                        raise BrokerError("WAIT_BINDING_MISMATCH")
                    if not wait["runtime_ref"]:
                        wait["runtime_ref"] = runtime_ref
                        wait["revision"] += 1
                        if wait["status"] == "preparing":
                            wait["status"] = "awaiting_authorization"
            self.vault.save()
            await self.notify(wait)
            return self.view(wait)

    async def registration(self, config):
        auth = self.data["clients"].get(config.id)
        if auth is None:
            metadata = await self.protocol.metadata(config)
            auth = {"metadata": metadata, "client": await self.protocol.register(config, metadata)}
            self.data["clients"][config.id] = auth
            self.vault.save()
        return auth

    async def action(self, action, wait_id, revision, *, agent_id=None, session_id=None):
        async with self.lock:
            wait = self.wait(wait_id)
            await self.run(wait["run_id"])
            if (session_id and session_id != wait["session_id"]) or (
                agent_id and agent_id != wait["agent_id"]
            ):
                raise BrokerError("WAIT_NOT_FOUND")
            if action != "GetMcpAuthorizationWait" and revision != wait["revision"]:
                raise BrokerError("REVISION_MISMATCH")
            view = self.view(wait)
            if action in {"OpenMcpAuthorization", "RetryMcpAuthorization"}:
                if "open" not in view["allowed_actions"]:
                    raise BrokerError("WAIT_NOT_ACTIONABLE")
                config = self.config(wait)
                tx = self.data["transactions"].get(wait.get("transaction", ""))
                if not tx or tx["status"] != "pending":
                    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
                    state_hash = digest(state)
                    try:
                        auth = await self.registration(config)
                        url = self.protocol.authorization_url(
                            config, auth["metadata"], auth["client"], state, verifier
                        )
                    except OAuthError as exc:
                        self.change(wait, "authorization_failed", exc.code)
                        self.vault.save()
                        raise BrokerError(exc.code) from None
                    tx = {"wait_id": wait_id, "status": "pending", "verifier": verifier, "url": url}
                    self.data["transactions"][state_hash] = tx
                    wait["transaction"] = state_hash
                    self.change(wait, "awaiting_authorization")
                ticket = secrets.token_urlsafe(32)
                self.tickets[ticket] = (tx["url"], wait_id, self.clock() + 60)
                self.vault.save()
                return {
                    **self.view(wait),
                    "authorization_url": self.base_url + "/open/" + ticket,
                }
            if action == "CancelMcpAuthorizationWait":
                if "cancel" not in view["allowed_actions"]:
                    raise BrokerError("WAIT_NOT_ACTIONABLE")
                self.change(wait, "cancelled", "USER_CANCELLED")
            elif action == "RetryMcpConnection":
                if "retry_connection" not in view["allowed_actions"]:
                    raise BrokerError("WAIT_NOT_ACTIONABLE")
                self.change(wait, "credential_ready")
            elif action != "GetMcpAuthorizationWait":
                raise BrokerError("UNKNOWN_AUTHORIZATION_ACTION")
            self.vault.save()
            await self.notify(wait)
            return self.view(wait)

    async def open_ticket(self, ticket):
        async with self.lock:
            value = self.tickets.pop(ticket, None)
            if not value or value[2] <= self.clock():
                raise BrokerError("AUTHORIZATION_LINK_EXPIRED")
            wait = self.wait(value[1])
            if wait["status"] != "awaiting_authorization":
                raise BrokerError("WAIT_NOT_ACTIONABLE")
            return value[0]

    async def callback(self, *, state, code=None, error=None, issuer=None):
        async with self.lock:
            tx = self.data["transactions"].get(digest(state))
            if not tx:
                raise BrokerError("INVALID_OAUTH_STATE")
            wait = self.wait(tx["wait_id"])
            if wait["transaction"] != digest(state):
                raise BrokerError("INVALID_OAUTH_STATE")
            if tx["status"] != "pending" or wait["status"] in TERMINAL:
                self.vault.save()
                return self.view(wait)
            config = self.config(wait)
            auth = self.data["clients"][config.id]
            if (issuer is not None and issuer != config.issuer) or (
                auth["metadata"].get("authorization_response_iss_parameter_supported")
                and not issuer
            ):
                raise BrokerError("OAUTH_ISSUER_MISMATCH")
            if error or not code:
                tx["status"] = "denied"
                self.change(
                    wait,
                    "authorization_failed",
                    "USER_DENIED" if error == "access_denied" else "AUTHORIZATION_FAILED",
                )
                self.vault.save()
                return self.view(wait)
            tx["status"] = "exchanging"
            self.vault.save()
        try:
            tokens = await self.protocol.token(
                config,
                auth["metadata"],
                auth["client"],
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": config.redirect_uri,
                    "code_verifier": tx["verifier"],
                },
            )
            failure = None
        except OAuthError as exc:
            tokens, failure = None, exc.code
        async with self.lock:
            wait = self.wait(tx["wait_id"])
            tx["status"] = "completed" if tokens else "failed"
            tx.pop("verifier", None)
            tx.pop("url", None)
            changed = [wait]
            if wait["status"] not in TERMINAL:
                if tokens:
                    self.data["grants"][config.id] = {
                        "status": "active",
                        "tokens": tokens,
                        "expires": self.clock() + float(tokens.get("expires_in", 300)),
                        "version": self.data["grants"].get(config.id, {}).get("version", 0) + 1,
                    }
                    self.change(wait, "credential_ready")
                    # One local project has one subject. Other sessions can use
                    # this exact connection if they have not started their own consent.
                    for sibling in self.data["waits"].values():
                        if (sibling["config_id"] == config.id and not sibling.get("transaction")
                                and sibling["status"] in {"preparing", "awaiting_authorization"}):
                            self.wait(sibling["wait_id"])
                            if sibling["status"] != "expired":
                                self.change(sibling, "credential_ready")
                            changed.append(sibling)
                else:
                    self.change(wait, "authorization_failed", failure)
            self.vault.save()
            for item in changed:
                await self.notify(item)
            return self.view(wait)

    async def refresh(self, key, config):
        grant = self.data["grants"].get(key)
        if not grant or grant["status"] != "active" or not refresh_due(grant["expires"], grant["tokens"], self.clock()):
            return
        if not grant["tokens"].get("refresh_token"):
            grant["status"] = "reauthorization_required"
            self.vault.save()
            return
        grant["status"] = "refreshing"
        self.vault.save()
        try:
            auth = await self.registration(config)
            token = await self.protocol.token(
                config,
                auth["metadata"],
                auth["client"],
                {"grant_type": "refresh_token", "refresh_token": grant["tokens"]["refresh_token"]},
            )
        except OAuthError:
            grant["status"] = "reauthorization_required"
        else:
            token.setdefault("refresh_token", grant["tokens"]["refresh_token"])
            grant.update(
                tokens=token,
                expires=self.clock() + float(token.get("expires_in", 300)),
                version=grant["version"] + 1,
                status="active",
            )
        self.vault.save()

    async def credential(self, wait_id):
        async with self.lock:
            wait = self.wait(wait_id)
            await self.run(wait["run_id"])
            if wait["status"] not in {"credential_ready", "connecting", "resumed"}:
                raise BrokerError("CREDENTIAL_NOT_READY")
            config = self.config(wait)
            await self.refresh(config.id, config)
            grant = self.data["grants"].get(config.id, {})
            if grant.get("status") != "active" or grant["expires"] <= self.clock():
                raise BrokerError("REAUTHORIZATION_REQUIRED")
            return self._credential(grant, config)

    @staticmethod
    def _credential(grant, config):
        return dict(
            access_token=grant["tokens"]["access_token"],
            resource=config.resource,
            config_ref=config.id,
            grant_version=grant["version"],
            expires=grant["expires"],
            allowed_tools=list(config.allowed_tools),
            tool_policies=config.tool_policies,
            allow_loopback=config.allow_loopback,
        )

    async def call(self, operation, **body):
        if operation == "CancelMcpAuthorizationWait":
            view = await self.get(body["wait_id"])
            if view["status"] in TERMINAL:
                return view
            return await self.action(operation, body["wait_id"], view["revision"])
        if operation == "BrowserMcpAuthorization":
            return await self.action(
                body["public_action"],
                body["wait_id"],
                body.get("expected_revision"),
                agent_id=body.get("agent_id"),
                session_id=body.get("session_id"),
            )
        raise BrokerError("UNKNOWN_AUTHORIZATION_ACTION")

    async def close(self):
        await self.http.aclose()
