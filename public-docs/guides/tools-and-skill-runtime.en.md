# Tools And Skill Runtime

KsADK can expose tools to an agent through framework-native tools, MCP/A2A
integrations, and the optional Skill Runtime. The public rule is simple: tools
should be declared explicitly, validated at runtime, and isolated from secrets
or local files the agent does not need.

## Tool Layers

```mermaid
flowchart LR
  Agent["User agent"] --> Framework["Framework adapter"]
  Framework --> Builtin["Framework-native tools"]
  Framework --> MCP["MCP / A2A tools"]
  Framework --> Skill["Skill Runtime tools"]
  Skill --> Backend["local or sandbox backend"]
  Backend --> Result["structured result"]
  Result --> Agent
```

The framework adapter normalizes tool definitions before an agent run. The
agent should receive a stable tool description, a narrow input schema, and a
structured result that can be rendered in the local Web UI or projected back
into session history.

## When To Use Each Tool Path

| Need | Recommended path |
| --- | --- |
| Simple Python helper inside the agent project | framework-native function tool |
| External tool server with its own lifecycle | MCP toolset |
| Agent-to-agent protocol integration | A2A client or adapter |
| Reusable executable skill with optional sandboxing | Skill Runtime |
| Local development only | local backend with explicit paths and test data |
| Untrusted or expensive execution | reviewed sandbox backend and limits |

Use the simplest path that gives the agent enough capability. Do not route a
plain deterministic helper through a remote runtime just to make it look like a
tool.

## Public Skill Runtime Contract

A public Skill Runtime integration should document:

- the skill name and purpose.
- input schema and required fields.
- output schema and error shape.
- required optional dependencies or extras.
- whether the skill runs locally or through a sandbox backend.
- which environment variables are needed.
- file, network, and execution limits.

The tool description should be precise enough for an LLM to decide when not to
call it. Avoid descriptions that imply broad filesystem, shell, network, or
credential access.

## Environment Configuration

Keep secrets out of source control. Store local development values in `.env` or
your shell environment, and publish only placeholder names in examples.

Common public examples:

```bash
export KSADK_SKILL_RUNTIME_BACKEND=local
export KSADK_SKILL_RUNTIME_TIMEOUT_SECONDS=30
```

If a backend requires credentials, document the variable names and setup steps
without publishing actual values:

```bash
export EXAMPLE_SANDBOX_API_KEY=...
```

Do not commit `.env`, `.pypirc`, PyPI tokens, kubeconfig files, private registry
credentials, cloud access keys, or generated runtime state.

## Local Backend

The local backend is useful for development, tests, and examples that operate on
known input. It should be treated as trusted local execution:

- use temporary directories in tests.
- pass explicit input files instead of scanning the whole repository.
- keep timeouts short.
- return structured failures rather than raw tracebacks when possible.
- avoid examples that execute arbitrary user-provided shell.

Local backend examples are acceptable in public docs when they are deterministic
and do not require internal infrastructure.

## Sandbox Backend

A sandbox backend is appropriate when the skill needs stronger isolation,
network policy, or dependency control. Public docs should describe the contract,
not private provider wiring:

| Topic | Public documentation should say |
| --- | --- |
| authentication | required variable names, not token values |
| limits | timeout, memory, file size, and network policy |
| files | allowed upload/download paths and retention behavior |
| errors | stable error codes or categories |
| cleanup | whether the sandbox is disposable per run |

Internal account IDs, private images, registry hosts, hosted control-plane URLs,
and provider-specific support runbooks should stay out of the public repository.

## Runner Payload

Framework adapters should pass tool and skill results as structured data. A
typical result includes:

| Field | Meaning |
| --- | --- |
| `name` | tool or skill name |
| `status` | `ok`, `failed`, `timeout`, or `cancelled` |
| `content` | user-visible result text or content blocks |
| `metadata` | execution metadata safe for logs and UI |
| `artifacts` | file references created by the tool, when supported |
| `error` | stable error summary for failed runs |

Business code should read structured fields instead of parsing local UI text.

## ADK Integration

For Google ADK projects, Skill Runtime tools can be injected during runner
loading when optional dependencies and configuration are available. Keep a
minimal ADK example free of optional runtime variables first, then add tools in
a separate example:

```python
from google.adk.agents import Agent

root_agent = Agent(
    name="tool_ready_agent",
    instruction="Use tools only when they are relevant to the user request.",
)
```

Then document the runtime setup beside the example:

```bash
pip install -U "ksadk[adk,skills]"
agentengine web . --no-open
```

If the tool is not available, the agent project should still fail with a clear
setup error instead of silently running with a different capability set.

## Testing Tool Integrations

Public tests should cover the boundary, not a private service account:

- tool schema conversion.
- successful local execution with deterministic input.
- timeout and failure handling.
- runner payload fields.
- Web UI request shape when the tool result is displayed.
- audit checks that no credentials or private endpoints appear in fixtures.

Prefer fake clients, temporary files, and local HTTP servers for public tests.
Only run provider-backed tests behind explicit environment gates.

## Security Checklist

Before publishing a tool or skill example:

- verify the example runs without internal accounts.
- remove private endpoints and customer data.
- check that generated files are ignored.
- document required optional extras.
- cap execution time and file sizes.
- avoid broad shell, network, or filesystem access.
- run the open-source audit before committing.

```bash
make open-source-audit
```

## Relationship To Other Guides

Read this page with:

- [Frameworks](frameworks.en.md) for runner loading behavior.
- [Agent Context](agent-context.en.md) for the structured invocation context.
- [Attachments And Multimodal Input](attachments-multimodal.en.md) for file input
  normalization.
- [Runtime Sessions And Files](../reference/runtime-sessions-files.en.md) for how
  tool events and file references are stored in local sessions.
