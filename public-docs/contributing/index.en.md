# Contributing

Contributions should target the public repository, public documentation, and
public CI. Do not include internal credentials, private endpoints, customer data,
or company-only deployment runbooks in pull requests.

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Build docs locally:

```bash
make public-docs-build
make public-docs-serve
```

Run open-source checks:

```bash
make open-source-audit
make public-docs-audit
```

For changes that touch packaging, public docs, release metadata, or repository
layout, run the broader review target before asking for release approval:

```bash
make open-source-review
```

## Public CI Expectations

Public CI must not require internal kubeconfig files, internal registries, internal object storage, or local `.zread/` state.

Before submitting a public PR:

- run focused tests for the changed area.
- run docs build when editing `public-docs/` or `mkdocs.yml`.
- update CLI docs when command behavior changes.
- update release notes when changing packaging or public API behavior.
- keep examples local-first unless a hosted feature is explicitly approved.

## Test Strategy

KsADK keeps public confidence through layered tests:

| Layer | Purpose | Typical command |
| --- | --- | --- |
| unit and component tests | validate isolated helpers, detection, runners, sessions, and packaging rules | `pytest tests/ -q` |
| CLI snapshot tests | protect user-visible command help, resource output, and error hints | focused pytest files under `tests/` |
| ASGI service tests | validate FastAPI routes and session events without a real network server | service/session pytest files |
| HTTP protocol E2E | validate `/v1/responses`, `/v1/chat/completions`, upload, and local Web UI action payloads | OpenAI protocol E2E tests |
| browser E2E | validate built UI behavior when Chromium is available | browser-tagged E2E tests |
| open-source audits | verify public tree, docs, Pages artifact, sdist, wheel, and clean export boundaries | `make open-source-review` |

When a change affects protocol shape, attachment handling, session events, or
the local Web UI payload, prefer a test that crosses the same boundary a real
client uses. For example, a pure helper test is not enough for an upload flow
that must pass through `UploadFile`, `ResponsesInput`, normalization, runner
payload construction, and session event persistence.

## Snapshot Updates

CLI snapshots are intentional public contracts. If a command, option, or error
hint changes by design:

1. update the implementation.
2. run the focused snapshot test and inspect the diff.
3. update only the affected snapshot section.
4. run the focused test again.

Do not refresh all snapshots as a bulk operation unless the formatting system
itself changed.

## Documentation Contributions

Public docs should be written for external developers. Prefer:

- commands that work in a clean virtual environment.
- placeholder credentials and provider URLs.
- explicit local fallback paths.
- clear distinction between local SDK behavior and hosted AgentEngine behavior.

Avoid:

- private URLs.
- kubeconfig paths.
- internal registry names.
- real tokens or customer data.
- references to generated `.zread/` output as the published source.

Local zread wiki output can be useful as an engineering note source, but public
documentation should be curated Markdown under `public-docs/`. Do not publish
the generated wiki directory or depend on it during public CI.

## Open-Source Review Boundary

The public repository should contain the SDK, public examples, public docs, CI,
and release metadata needed by external developers. It should not contain:

- internal deployment automation.
- private registry or object-storage locations.
- internal incident notes or operator playbooks.
- `.pypirc`, PyPI/TestPyPI tokens, GitHub tokens, kubeconfigs, or local cloud
  credentials.
- local session state, uploaded files, extracted attachment content, or
  generated build output.

If a file is useful internally but not safe or useful externally, keep it out of
the clean export and summarize the relevant public behavior in curated docs.

## Security

Report vulnerabilities through the security process documented in `SECURITY.md` once the public repository is created.
