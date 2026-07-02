# Troubleshooting

This page covers common local setup and runtime issues.

## `agentengine` Command Not Found

Confirm the virtual environment is active and the package is installed:

```bash
python -m pip show ksadk
python -m pip install -U ksadk
which agentengine
```

On Windows, reopen the shell after installation if the scripts directory was
added to `PATH`.

## Project Not Detected

Run from the project root and check for an entry file:

```bash
ls
cat agentengine.yaml
```

The project should expose an agent variable, usually `root_agent`, from
`agent.py`, `main.py`, `app.py`, or a package module.

Recommended explicit config:

```yaml
framework: langgraph
entry_point: agent.py
agent_variable: root_agent
```

If detection still fails, create a minimal `agent.py` that exports the configured
variable and add imports back gradually.

Useful checks:

```bash
python - <<'PY'
from pathlib import Path
print(Path("agentengine.yaml").exists())
print(Path("agent.py").exists())
PY
```

If a project uses a `src/` layout, make sure `entry_point` is relative to the
project root and that the configured object is imported or defined in that file.
Detection is static; it will not run setup code that dynamically creates the
agent variable.

## Model Calls Fail

Check `.env`:

```bash
agentengine config show
```

Confirm:

- `OPENAI_API_KEY` is present.
- `OPENAI_BASE_URL` points to an OpenAI-compatible endpoint.
- `OPENAI_MODEL_NAME` is accepted by the provider.
- the provider supports streaming if you are using streaming mode.

Try a non-streaming run:

```bash
agentengine run . -i --no-stream
```

Try a one-run model override:

```bash
agentengine run . -i --model my-model
```

If the provider rejects the request, reproduce with a direct provider curl
outside KsADK before debugging the framework adapter.

## Web UI Does Not Open

Start without opening the browser automatically:

```bash
agentengine web . --no-open
```

Open the printed local URL manually. If the port is busy:

```bash
agentengine web . --port 7860 --no-open
```

If old conversations appear, remove local UI state:

```bash
rm -rf .agentengine/ui
```

## API Server Port Is Busy

Choose another port:

```bash
agentengine run . --port 8090
```

## Build Or Deploy Requires Cloud Credentials

Local development does not require cloud credentials. Commands such as
`build`, `deploy`, and `launch` can require Kingsoft Cloud credentials depending
on target and mode.

For review or documentation, use dry-run behavior where supported:

```bash
agentengine --dry-run build .
```

Do not place real credentials, private registry names, internal kubeconfig
paths, or customer data in public issues, docs, tests, or examples.

## Importing An Existing Agent Fails

Check the generated config:

```bash
cat agentengine.yaml
```

Common fixes:

- set `framework` explicitly.
- set `entry_point` to an existing Python file.
- set `agent_variable` to the exported object.
- install missing framework dependencies.
- move side-effectful startup code behind `if __name__ == "__main__"`.

## Responses API Session Error

If you see a conflict between `conversation` and `session_id`, use only one
session field:

```json
{
  "conversation": {"id": "local-session-1"},
  "input": "continue"
}
```

Do not send a different `session_id` in the same request.

## Streaming Client Hangs

When using `stream: true`, confirm your client consumes server-sent events.
For debugging, switch to non-streaming:

```json
{
  "input": "hello",
  "stream": false
}
```

If non-streaming works, the issue is in the client event loop, proxy buffering,
or SSE parser.

For the local Web UI, a browser refresh can disconnect the original stream while
the run continues on the server. Reconnect-capable clients should use the known
session id, invocation id, and last consumed event sequence id to read later
run events. Do not assume reconnecting recreates the same TCP stream.

## Uploaded File Is Ignored

Check whether the request uses a supported file shape:

```json
{
  "type": "input_file",
  "filename": "notes.txt",
  "file_data": "data:text/plain;base64,..."
}
```

For Web UI uploads, check that the later run request references the returned
`ksadk-upload://...` URI. That URI is local to the running server; it will not
work after deleting `.agentengine/ui`, moving the project, or sending the
request to another runtime.

If the file appears but the answer misses its content:

- confirm the file type is supported.
- check extraction warnings in the runner payload or logs.
- try a smaller text-only file to separate extraction issues from model issues.
- use `current_attachment_results` in custom hooks when the current turn must
  process only newly uploaded files.

## Follow-Up Question Loses File Context

The runtime can carry the most recent attachment context in the same session.
If a follow-up question behaves like no file was provided, verify:

- the second request uses the same `conversation.id` or `session_id`.
- the first request completed successfully.
- the client did not create a new local session after a page refresh.
- custom hooks read `attachment_results` when follow-up context is desired.

Use `current_attachment_results` only for current-turn files. It is expected to
be empty on a text-only follow-up.

## Session History Looks Wrong

The runtime stores an append-only event log and projects model history from that
log. `run_status` and `reasoning` events are lifecycle or diagnostic events;
they are not normal model messages. If model history is unexpectedly long or
short:

- check whether context compaction created a checkpoint.
- verify the session id is stable across requests.
- inspect whether tool or approval events are being projected as text summaries.
- reset local development state with `rm -rf .agentengine/ui` when you want a
  clean local Web UI session.

## Public Docs Build Fails

Run:

```bash
uv run --extra dev python -m mkdocs build --strict
```

Common causes:

- broken relative links.
- page added to `nav` but not created.
- duplicate Markdown extension entries.
- generated files under `site/` accidentally committed.
- docs referencing private files excluded from the public repository.

## Need More Detail

Run command-specific help:

```bash
agentengine --help
agentengine run --help
agentengine web --help
agentengine config --help
```
