# Environment Variables

This reference lists the environment variables most projects need when running
KsADK locally or in a release pipeline. Use placeholders in committed files and
put real values in local `.env` files or CI secrets.

## Model Provider

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | API key for an OpenAI-compatible model provider |
| `OPENAI_BASE_URL` | provider base URL, usually ending in `/v1` |
| `OPENAI_API_BASE` | compatibility alias for `OPENAI_BASE_URL` |
| `OPENAI_MODEL_NAME` | default model name used by local runners and UI |
| `MODEL_NAME` | compatibility alias used by some projects |

Example:

```bash
OPENAI_API_KEY=sk-test
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL_NAME=my-model
```

## Project And Local UI

| Variable | Purpose |
| --- | --- |
| `KSADK_PROJECT_DIR` | project directory resolved by `agentengine web` |
| `AGENTENGINE_UI_DIR` | local UI state directory; defaults under `.agentengine/ui` |
| `KSYUN_REGION` | region used by cloud actions and some SDK clients |

## Session Storage

| Variable | Purpose |
| --- | --- |
| `KSADK_SESSION_BACKEND` | session backend, for example `local` or `postgres` |
| `AGENTENGINE_SESSION_BACKEND` | compatibility alias for session backend |
| `KSADK_SESSION_DSN` | PostgreSQL or shared backend DSN |
| `KSADK_SESSION_PATH` | local session database path |
| `KSADK_SESSION_NAMESPACE` | namespace for shared session backends |
| `KSADK_STM_BACKEND` | short-term memory/session backend compatibility variable |
| `KSADK_STM_PATH` | local SQLite path for UI/session state |
| `KSADK_STM_DB_PATH` | compatibility alias for local SQLite path |
| `KSADK_STM_URL` | compatibility DSN for database-backed session state |
| `KSADK_STM_DB_URL` | compatibility DSN for database-backed session state |
| `KSADK_ADK_SESSION_BACKEND` | ADK-native session backend selector |
| `KSADK_ADK_SESSION_PATH` | ADK-native SQLite session path |
| `KSADK_ADK_SESSION_URL` | ADK-native database session URL |
| `KSADK_TENANT_ID` | tenant id used for session namespace scoping |
| `KSADK_WORKSPACE_ID` | workspace id used for session namespace scoping |
| `AGENTENGINE_TENANT_ID` | compatibility tenant id |
| `AGENTENGINE_WORKSPACE_ID` | compatibility workspace id |

Local UI usually sets:

```bash
KSADK_STM_BACKEND=sqlite
KSADK_STM_PATH=.agentengine/ui/sessions.sqlite
```

## Ambient Runtime Context

| Variable | Purpose |
| --- | --- |
| `KSADK_KB_AMBIENT_ENABLED` | enable runtime-injected knowledge context; default enabled |
| `KSADK_KB_AMBIENT_POLICY` | `on_demand`, `always`, or `disabled` |
| `KSADK_LTM_AMBIENT_ENABLED` | enable runtime-injected memory context; default enabled |
| `KSADK_LTM_AMBIENT_POLICY` | `on_demand`, `always`, or `disabled` |
| `KSADK_LTM_AUTO_SAVE` | save completed turns to long-term memory when supported |
| `KSADK_LTM_AGENT_ID` | agent id recorded with memory entries |

## Workspace Files

| Variable | Purpose |
| --- | --- |
| `KSADK_WORKSPACE_FILES_ENABLED` | enable workspace file routes |
| `KSADK_WORKSPACE_ROOT_LABEL` | display label for the workspace root |
| `KSADK_WORKSPACE_MAX_UPLOAD_BYTES` | single upload size limit |

## Long-Term Memory

| Variable | Purpose |
| --- | --- |
| `KSADK_LTM_BACKEND` | memory backend: `local`, `http`, or `sdk` |
| `KSADK_LTM_TOP_K` | default number of memories to retrieve |
| `KSADK_LTM_INDEX` | local or generic memory index name |
| `KSADK_LTM_APP_NAME` | application name used by memory service |
| `KSADK_LTM_HTTP_URL` | HTTP memory backend URL |
| `KSADK_LTM_HTTP_TOKEN` | HTTP memory backend token |
| `KSADK_LTM_ACCESS_KEY` | SDK memory access key; can fall back to cloud AK env vars |
| `KSADK_LTM_SECRET_KEY` | SDK memory secret key; can fall back to cloud SK env vars |
| `KSADK_LTM_REGION` | SDK memory region |
| `KSADK_LTM_ENDPOINT` | SDK memory endpoint |
| `KSADK_LTM_SCHEME` | SDK memory scheme, usually `https` |
| `KSADK_LTM_NAMESPACE` | SDK memory namespace |
| `KSADK_LTM_AGENT_ID` | agent id recorded with memory entries |
| `KSADK_LTM_SCENE_ID` | scene id, default `_sys_general` |
| `KSADK_LTM_AUTO_SAVE` | enable automatic memory saving when supported |
| `KSADK_MEMORY_BACKEND` | generic memory backend selector for legacy integrations |
| `KSADK_MEMORY_URL` | generic memory backend URL |
| `KSADK_MEMORY_PREFIX` | generic memory key prefix |
| `KSADK_MEMORY_TTL` | generic memory TTL in seconds |

## Knowledge Base

| Variable | Purpose |
| --- | --- |
| `KSADK_KB_DATASET_ID` | enable a knowledge dataset integration |
| `KSADK_KB_TOP_K` | number of snippets to retrieve |
| `KSADK_KB_ACCESS_KEY` | optional knowledge SDK access key |
| `KSADK_KB_SECRET_KEY` | optional knowledge SDK secret key |
| `KSADK_KB_REGION` | knowledge service region |
| `KSADK_KB_ENDPOINT` | knowledge service endpoint |
| `KSADK_KB_SEARCH_METHOD` | retrieval method, default `intelligence_search` |
| `KSADK_KB_SCORE_THRESHOLD` | optional score threshold |
| `KSADK_KB_RERANKING_ENABLE` | enable reranking when supported |

## Skill Runtime

| Variable | Purpose |
| --- | --- |
| `KSADK_SKILL_SPACE_IDS` | comma-separated Skill Space ids |
| `SKILL_SPACE_ID` | compatibility alias for a single Skill Space id |
| `KSADK_PUBLIC_SKILL_SPACE_IDS` | comma-separated public Skill Space ids appended after user spaces |
| `KSADK_PUBLIC_SKILL_ALLOWLIST` | comma-separated public Skill names to expose |
| `KSADK_LOCAL_SKILLS_DIR` | local directory containing `SKILL.md` packages |
| `KSADK_SELECTED_SKILL_NAMES` | comma-separated Skill names selected by an outer agent |
| `KSADK_SKILLS_MODE` | Skill loading mode: `auto`, `local`, or `sandbox` |
| `KSADK_SKILL_SERVICE_URL` | Skill Service endpoint |
| `KSADK_SKILL_SERVICE_TOKEN` | Skill Service bearer token when token auth is used |
| `KSADK_SKILL_SERVICE_ACCESS_KEY` | signed Skill Service access key |
| `KSADK_SKILL_SERVICE_SECRET_KEY` | signed Skill Service secret key |
| `KSADK_SKILL_SERVICE_ACCOUNT_ID` | account id for signed requests |
| `KSADK_SKILL_SERVICE_REGION` | Skill Service region |
| `KSADK_SKILL_SERVICE_API_VERSION` | Skill Service API version |
| `KSADK_SKILL_SERVICE_SIGN_SERVICE` | signing service name |
| `KSADK_SKILL_CACHE_DIR` | local cache for downloaded skill packages |
| `KSADK_SKILL_ALLOW_HASH_MISMATCH` | allow unverified skill package preview |
| `KSADK_SKILL_MANIFEST_TIMEOUT` | remote Skill manifest listing timeout in seconds |
| `KSADK_SKILL_MANIFEST_LIMIT` | maximum manifests injected into agent instructions |
| `KSADK_SKILL_RUNTIME_BACKEND` | isolated execution backend, for example `local_process` or `e2b` |
| `KSADK_SKILL_RUNTIME_TEMPLATE_ID` | runtime template id |
| `KSADK_SANDBOX_BACKEND` | sandbox backend selector used as a compatibility fallback |
| `KSADK_SANDBOX_TEMPLATE_ID` | sandbox template id; also enables E2B-style backend |
| `KSADK_SKILL_RUNTIME_TIMEOUT` | isolated execution timeout in seconds |
| `KSADK_SANDBOX_TIMEOUT` | sandbox timeout compatibility variable |
| `KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS` | allow internet access for sandbox execution |
| `KSADK_SKILL_RUNTIME_AGENT_PATH` | local process runtime agent path |
| `KSADK_SKILL_WORKDIR` | workspace directory exposed to skill execution |
| `KSADK_SKILL_ARTIFACT_PROJECT` | artifact project name for generated outputs |

## MCP

| Variable | Purpose |
| --- | --- |
| `KSADK_ENABLE_MCP_TOOLS` | enable or disable MCP tools |
| `KSADK_MCP_SERVERS` | JSON array of MCP server definitions |
| `KSADK_BUILD_ENABLE_MCP` | include MCP runtime dependencies during code build |

Example:

```bash
KSADK_ENABLE_MCP_TOOLS=1
KSADK_MCP_SERVERS='[{"name":"docs","url":"http://127.0.0.1:9000/mcp"}]'
```

## Build And Packaging

| Variable | Purpose |
| --- | --- |
| `KSADK_BUILD_PIP_INSTALL_TIMEOUT_SECONDS` | pip install timeout in build flows |
| `KSADK_BUILD_ENABLE_ATTACHMENT_OCR` | include OCR-related attachment dependencies |
| `KSADK_BUILD_ENABLE_POSTGRES_SESSION` | include PostgreSQL session dependencies |

## Observability

| Variable | Purpose |
| --- | --- |
| `LANGFUSE_PUBLIC_KEY` | enable Langfuse tracing |
| `LANGFUSE_SECRET_KEY` | Langfuse secret |
| `LANGFUSE_BASE_URL` | Langfuse base URL |
| `LANGFUSE_HOST` | compatibility alias for Langfuse host |
| `LANGFUSE_USE_CALLBACK` | use framework callback mode instead of direct OTLP path |
| `SESSION_TITLE_MODEL` | model override for generated local session titles |
| `COMPACTION_DISABLE_SEMANTIC` | disable semantic session summary compaction |
| `COMPACTION_SUMMARY_TIMEOUT_MS` | timeout for summary generation |
| `COMPACTION_SUMMARY_MAX_GROUPS` | maximum message groups summarized |
| `COMPACTION_SUMMARY_MODEL` | model override for semantic summaries |

## Safety Rules

- Never commit real values for keys, tokens, DSNs, private endpoints, customer
  dataset ids, or kubeconfigs.
- Prefer local `.env` for development and CI secrets for automation.
- Keep public docs on placeholders such as `sk-test`, `my-model`, and
  `https://api.example.com/v1`.
