"""Replay-safe authorization boundary for explicitly integrated LangGraph nodes."""

import os
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

from .client import BrokerError, MCPToolOutcomeUnknown

SCHEMA = "mcp.authorization/v1"
MARKER = "x-ksadk-mcp-authorization"
_declaration = ContextVar("mcp_oauth_declaration", default=None)
_extension = ContextVar("mcp_oauth_extension", default=None)


def create_mcp_agent(
    model,
    *,
    mcp_servers,
    system_prompt="",
    read_only_tools=None,
    execution_policies=None,
    tools=(),
    max_tool_calls=12,
    checkpointer=None,
):
    """Build an opt-in LangGraph agent using named MCP connections.

    mcp_servers is a list of connection names, or a name/description mapping.
    This helper manages OAuth connections only; pass existing ordinary tools
    through tools without changing their clients or authentication.
    Tools are discovered only when the model needs a connection. No execution
    approval is added by default. execution_policies optionally maps connection
    and tool names to developer-supplied ToolPolicy values. YAML tool_policies
    remain optional additional argument constraints, enforced by call_mcp_tool.

    The returned graph uses the injected Kernel extension in either local Web
    or hosted mode. Construction performs no discovery, starts no callback
    server and does not configure storage, credentials or environment variables.
    Existing LangChain tools and an Agent-owned checkpointer can be supplied.
    """
    from ._agent import build_agent

    return build_agent(
        model,
        mcp_servers=mcp_servers,
        system_prompt=system_prompt,
        read_only_tools=read_only_tools,
        execution_policies=execution_policies,
        tools=tools,
        max_tool_calls=max_tool_calls,
        checkpointer=checkpointer,
    )


@contextmanager
def bind_declaration(declaration):
    token = _declaration.set(declaration)
    try:
        yield
    finally:
        _declaration.reset(token)


def current_declaration(name):
    declaration = _declaration.get()
    if not declaration or declaration["name"] != name:
        return None
    from copy import deepcopy

    # Resolve a confidential client's environment reference only at the private
    # Broker boundary. The object and ToolMessage artifacts contain no secret.
    value = deepcopy(declaration)
    secret_env = value["oauth"].pop("client_secret_env", None)
    if secret_env:
        secret = os.getenv(secret_env)
        if not secret:
            raise BrokerError("MCP_CLIENT_SECRET_MISSING")
        value["oauth"]["client_secret"] = secret
    return value


@contextmanager
def bind_oauth_extension(extension):
    token = _extension.set(extension)
    try:
        yield
    finally:
        _extension.reset(token)


def classify_mcp_error(error):
    if isinstance(error, BrokerError):
        return error.code
    import httpx

    children = getattr(error, "exceptions", ())
    for child in children:
        code = classify_mcp_error(child)
        if code != "CONNECTION_FAILED":
            return code
    if isinstance(error, httpx.HTTPStatusError):
        challenge = error.response.headers.get("WWW-Authenticate", "")
        if error.response.status_code == 403:
            return (
                "INSUFFICIENT_SCOPE"
                if "insufficient_scope" in challenge
                else "BUSINESS_PERMISSION_DENIED"
            )
        if error.response.status_code == 401:
            return "REAUTHORIZATION_REQUIRED"
    return "CONNECTION_FAILED"


@asynccontextmanager
async def connect(credential):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from .transport import http_client_factory

    async with streamablehttp_client(
        credential["resource"],
        headers={"Authorization": "Bearer " + credential["access_token"]},
        httpx_client_factory=http_client_factory(credential),
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def prepare_connection(client, wait_id):
    credential = await client.credential(wait_id)
    try:
        async with connect(credential) as session:
            tools = await _catalog(session, credential)
            if not tools:
                raise BrokerError("TOOLS_EMPTY")
    except Exception as exc:
        code = classify_mcp_error(exc)
        if code in {"REAUTHORIZATION_REQUIRED", "INSUFFICIENT_SCOPE"}:
            raise BrokerError(code, rejected_grant_version=credential["grant_version"]) from None
        if isinstance(exc, BrokerError):
            raise
        raise BrokerError(code) from None


def require_wait_checkpoint(*, hosted: bool):
    from langgraph.config import get_config
    from ksadk.runners.langgraph_runner import LangGraphRunner

    saver = get_config()["configurable"].get("__pregel_checkpointer")
    capability = LangGraphRunner.checkpoint_capability_for_saver(saver)
    if not capability["Durable"] or (hosted and not capability["SharedAcrossPods"]):
        raise BrokerError(
            "MCP_OAUTH_CHECKPOINT_REQUIRED: configure a durable LangGraph checkpointer "
            "or enable KSADK_LANGGRAPH_AUTO_CHECKPOINT=1. Hosted Agents can reuse "
            "their PostgreSQL session database."
        )


async def _authorized_credential(
    config_id, request_key, *, rejected_grant_version=None, force_wait=False
):
    from langgraph.types import interrupt

    from ksadk.events.run_authority import current_run_event_authority

    extension = _extension.get()
    authority = current_run_event_authority()
    if extension is None or authority is None:
        if os.getenv("AGENT_RUNTIME_ID"):
            if os.getenv("KSADK_ENABLE_KERNEL", "").strip().lower() not in {"1", "true", "on", "yes"}:
                raise BrokerError("MCP_OAUTH_KERNEL_REQUIRED: 请在发布环境变量中设置 KSADK_ENABLE_KERNEL=1。")
            if not os.getenv("KSADK_MCP_OAUTH_PROJECTION"):
                raise BrokerError("MCP_OAUTH_BROKER_UNAVAILABLE: 请联系平台启用此版本的 OAuth Broker。")
        raise BrokerError("MCP_OAUTH_RUNTIME_UNSUPPORTED: use an SDK Runtime with the MCP OAuth extension")
    lookup = getattr(extension, "lookup_credential", None)
    if lookup is not None and not force_wait:
        status = await lookup(
            authority.run_id, config_id, request_key, rejected_grant_version=rejected_grant_version
        )
        if status.get("status") == "authorized":
            return {**status["credential"], "_direct": True}
    wait = await extension.ensure_authorization(authority.run_id, config_id, request_key)
    if wait["status"] in {"cancelled", "expired", "failed"}:
        raise BrokerError(wait.get("error") or "WAIT_NOT_ACTIONABLE")
    answer = interrupt(
        {
            "kind": "external_authorization",
            "schema": SCHEMA,
            "wait_id": wait["wait_id"],
            "prompt": "需要授权 MCP 服务",
        }
    )
    # Only the extension's durable completion reservation can authorize
    # this value. A public resume(True) is not proof of user consent.
    if not isinstance(answer, dict) or answer.get("wait_id") != wait["wait_id"]:
        raise BrokerError("AUTHORIZATION_COMPLETION_REQUIRED")
    await extension.require_completion(wait["wait_id"], authority.run_id, answer.get("revision"))
    return await extension.client.credential(wait["wait_id"])


async def _catalog(session, credential):
    """Read all catalog pages and apply the immutable connection filter."""
    allowed = credential.get("allowed_tools", [])
    tools, cursors = {}, set()
    cursor = None
    while True:
        page = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
        for tool in page.tools:
            if not allowed or tool.name in allowed:
                if tool.name in tools:
                    raise BrokerError("MCP_TOOL_CATALOG_INVALID")
                tools[tool.name] = tool
        cursor = page.nextCursor
        if not cursor:
            return tools
        if cursor in cursors or len(cursors) >= 100:
            raise BrokerError("MCP_TOOL_CATALOG_INVALID")
        cursors.add(cursor)


@asynccontextmanager
async def _tool_session(config_id, request_key, credential):
    """Retry access checks only before handing a live session to a business tool."""
    from langgraph.errors import GraphInterrupt

    while True:
        handed_off = False
        completed = False
        try:
            async with connect(credential) as session:
                catalog = await _catalog(session, credential)
                if not catalog:
                    raise BrokerError("TOOLS_EMPTY")
                handed_off = True
                yield session, catalog, credential
                completed = True
            return
        except (GraphInterrupt, MCPToolOutcomeUnknown):
            raise
        except Exception as exc:
            if isinstance(exc, BaseExceptionGroup) and exc.subgroup(
                (GraphInterrupt, MCPToolOutcomeUnknown)
            ):
                raise
            if completed:
                # Session teardown cannot invalidate an already recorded reply.
                return
            reason = exc.code if isinstance(exc, BrokerError) else classify_mcp_error(exc)
            if handed_off or not credential.get("_direct"):
                raise BrokerError(reason) from None
        if reason in {"REAUTHORIZATION_REQUIRED", "INSUFFICIENT_SCOPE"}:
            credential = await _authorized_credential(
                config_id, request_key, rejected_grant_version=credential["grant_version"]
            )
        elif reason in {"CONNECTION_FAILED", "TOOLS_EMPTY"}:
            # Keep the grant. Retry connection on the original task.
            credential = await _authorized_credential(config_id, request_key, force_wait=True)
        else:
            raise BrokerError(reason)


async def list_mcp_tools(config_id: str, *, request_key: str):
    """Authorize on demand and return MCP tool names, descriptions and schemas.

    Call from a separate graph node before invoking the model. The returned
    JSON contains no credential/connection handles and can be checkpointed.
    Convert each inputSchema to a model tool's parameters when binding tools.
    request_key must remain stable when this node resumes after authorization.
    """
    credential = await _authorized_credential(config_id, request_key)
    async with _tool_session(config_id, request_key, credential) as (_, tools, _):
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema,
            }
            for tool in tools.values()
        ]


async def call_mcp_tool(
    config_id: str,
    tool: str,
    arguments: dict,
    *,
    request_key: str,
    policy=None,
    raise_on_error: bool = True,
):
    """Execute a model-selected call in its own checkpointed graph node.

    Use the persisted model tool-call ID as request_key, not a new random UUID
    on replay. Execution approval is added only by an explicit policy. Optional
    Broker tool_policies constrain arguments; input validation also uses the
    provider's current inputSchema. raise_on_error=False lets an agent feed
    MCP tool errors back to the model for correction.
    """
    from ksadk.events.run_authority import current_run_event_authority

    from .security import digest

    credential = await _authorized_credential(config_id, request_key)
    run_id = current_run_event_authority().run_id
    extension = _extension.get()
    validate_tool_arguments(credential, tool, arguments)
    require_tool_approval(tool, arguments, policy)
    # A resumed node obtains a fresh credential above; no open MCP connection
    # survives the approval interrupt.
    while True:
        dispatched = False
        result = None
        try:
            async with _tool_session(config_id, request_key, credential) as (
                session, catalog, credential,
            ):
                if tool not in catalog:
                    raise BrokerError("MCP_TOOL_UNAVAILABLE")
                validate_input_schema(catalog[tool].inputSchema, arguments)
                call_digest = digest([credential["config_ref"], tool, arguments])
                result = await extension.claim_dispatch(run_id, request_key, call_digest)
                if result is None:
                    dispatched = True
                    result = (await session.call_tool(tool, arguments)).model_dump(mode="json")
                    await extension.finish_dispatch(run_id, request_key, call_digest, result)
        except BrokerError as exc:
            if not dispatched:
                raise
            # Streamable HTTP can report a lost reply when exiting its task group.
            # Cover both the call and teardown, including a failed durable receipt.
            if result is not None or exc.code not in {
                "REAUTHORIZATION_REQUIRED", "INSUFFICIENT_SCOPE", "BUSINESS_PERMISSION_DENIED",
            }:
                raise MCPToolOutcomeUnknown("MCP_TOOL_OUTCOME_UNKNOWN") from None
            await extension.release_declined_dispatch(run_id, request_key, call_digest)
            if exc.code == "BUSINESS_PERMISSION_DENIED" or not credential.get("_direct"):
                raise
            # The provider explicitly declined this grant. No business call ran.
            credential = await _authorized_credential(
                config_id, request_key, rejected_grant_version=credential["grant_version"]
            )
            continue
        if result.get("isError") and raise_on_error:
            raise BrokerError("MCP_TOOL_FAILED")
        return result


def require_tool_approval(tool, arguments, policy=None):
    # OAuth controls service access. Only an explicit application policy may
    # add execution approval; do not infer risk from the authentication mode.
    if policy is None:
        return
    from langgraph.types import interrupt

    from ksadk.tools.gateway import ToolGateway

    gateway = ToolGateway({tool: policy})
    decision = gateway.invoke(tool, lambda: True)
    if isinstance(decision, dict) and decision.get("approval_required"):
        approval = interrupt(
            {
                "kind": "approval",
                "tool_name": tool,
                "name": tool,
                "args": arguments,
                "action_requests": [{"name": tool, "args": arguments}],
                "review_configs": [
                    {"action_name": tool, "allowed_decisions": ["approve", "reject"]}
                ],
            }
        )
        if not isinstance(approval, dict) or approval.get("decisions") != [{"type": "approve"}]:
            raise BrokerError("TOOL_APPROVAL_DENIED")


def validate_tool_arguments(credential, tool, arguments):
    """Rules come from the Broker's immutable Agent version, not the model."""
    from jsonschema import Draft202012Validator

    allowed = credential.get("allowed_tools", [])
    if allowed and tool not in allowed:
        raise BrokerError("MCP_TOOL_NOT_ALLOWED")
    schema = credential.get("tool_policies", {}).get(tool)
    if schema is not None and not Draft202012Validator(schema).is_valid(arguments):
        # Never include arguments or validation details in a public error.
        raise BrokerError("MCP_TOOL_ARGUMENTS_NOT_ALLOWED")


def validate_input_schema(schema, arguments):
    """Validate the MCP schema without fetching external JSON Schema refs."""
    from jsonschema import Draft202012Validator
    from referencing import Registry

    try:
        Draft202012Validator.check_schema(schema)
        valid = Draft202012Validator(schema, registry=Registry()).is_valid(arguments)
    except Exception:
        raise BrokerError("MCP_TOOL_SCHEMA_INVALID") from None
    if not valid:
        raise BrokerError("MCP_TOOL_INPUT_INVALID")
