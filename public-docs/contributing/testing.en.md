# Testing Strategy

KsADK uses layered tests so changes can be checked at the boundary they affect:
helpers are tested in process, command output is tested as user-visible text,
protocol changes are tested through HTTP, and release candidates are audited as
publishable artifacts.

## Test Layers

| Layer | What it protects | Typical command |
| --- | --- | --- |
| unit and component tests | helpers, config parsing, runners, sessions, package rules | `uv run --extra dev pytest tests/ -q` |
| CLI snapshot tests | help text, error hints, resource output | focused pytest files under `tests/` |
| ASGI service tests | FastAPI routes without opening a real port | service and session pytest files |
| HTTP protocol E2E | `/v1/responses`, `/v1/chat/completions`, upload, session events | protocol E2E tests |
| browser E2E | local Web UI request construction and upload behavior | browser-capable E2E tests |
| open-source audits | public tree, clean exports, Pages, sdist, wheel | `make open-source-review` |

Use the narrowest test that proves the change, then run the broader gate when a
change touches public behavior, packaging, docs, or release boundaries.

## Local Setup

```bash
uv sync --extra dev
uv run --extra dev pytest tests/ -q
```

For documentation changes:

```bash
make public-docs-build
make public-docs-audit
```

For release or open-source boundary changes:

```bash
make open-source-review
make open-source-review-bundle
```

## Snapshot Tests

CLI output is a public contract. Snapshot tests protect:

- command help and options.
- workflow command help.
- resource list/status/share output.
- error summaries and repair hints.

When a snapshot fails, decide whether the user-visible contract changed on
purpose. If it did, update only the affected snapshot section. If it did not,
treat the failure as a regression.

Do not bulk-refresh snapshots to make tests pass. A snapshot diff should be
small enough for a reviewer to understand what changed.

## Protocol Tests

Protocol tests should cross the same boundary as the real client. For example,
a change to file uploads should prove the flow through:

1. upload or inline input parsing.
2. request normalization.
3. runner payload construction.
4. session event persistence.
5. response serialization.

A helper-level unit test is not enough when the real risk is at the protocol
boundary.

## Browser Tests

Browser E2E tests are used when the Web UI must construct the exact request a
server expects. These tests may require Chromium. If Chromium is unavailable,
the browser cases can be skipped, but HTTP protocol tests should still cover the
backend behavior.

Use browser E2E for:

- image and file upload payload shape.
- session resume and stream subscription behavior.
- workspace preview interactions.
- UI behavior that cannot be proven from backend tests alone.

## Open-Source Review Gate

`make open-source-review` is the local gate for the public release candidate. It
checks:

- open-source contract tests.
- current public repository audit.
- clean `ksadk-python` export candidate.
- independent `ksadk-web` export candidate and smoke tests.
- GitHub Pages candidate build and audit.
- package metadata with Twine.
- sdist and wheel file-list plus extracted-content audit.
- clean virtualenv wheel smoke.

This target is intentionally broader than normal development tests. Run it
before asking maintainers to approve GitHub import, Pages, release, or PyPI
publication.

## What To Run

| Change type | Minimum checks |
| --- | --- |
| CLI help, options, or error text | focused CLI tests and affected snapshots |
| runtime request/response behavior | focused runtime tests plus protocol E2E |
| attachments or workspace files | protocol tests and workspace security tests |
| public docs | `make public-docs-build` and `make public-docs-audit` |
| package metadata or release scripts | `uv build`, `twine check`, artifact audit |
| open-source export policy | export tests, open-source audit, review bundle |

When in doubt, prefer evidence from the same boundary the user or release
process will exercise.

