# Build And Package

This guide explains the public build boundary for KsADK projects and the SDK
itself.

## Local Project Build

For a normal agent project, start with local validation:

```bash
agentengine run . -i
agentengine web . --no-open
```

Then inspect command-specific build options:

```bash
agentengine build --help
agentengine deploy --help
agentengine launch --help
```

Cloud packaging can require credentials, registry access, object storage, or
approved deployment targets. Public examples should keep those paths optional
and provide a local fallback.

## Dry Run First

Use dry-run behavior where supported:

```bash
agentengine --dry-run build .
agentengine --dry-run deploy .
```

Dry runs are useful in docs and reviews because they show intent without
creating remote resources.

## Python SDK Release Candidate

Maintainers preparing a public SDK release should run the open-source gate:

```bash
make open-source-review
make open-source-review-bundle
python3 scripts/audit_public_history_paths.py --json --allow-violations
git diff --check
```

The gate checks:

- public repository audit.
- clean `ksadk-python` export audit.
- clean `ksadk-web` export audit.
- public docs build and audit.
- Python package build.
- release artifact audit.
- wheel smoke install.
- Web UI candidate tests and builds.
- path-level Git history audit evidence for publication strategy.

The history path audit is intentionally path-level only. It helps decide whether
the first public import can use full history, clean export, or a reviewed
history rewrite. It does not replace a content-level secret scan.

## Artifact Rules

Release artifacts must not contain:

- `.env`, `.pypirc`, kubeconfig, cookies, access keys, or tokens.
- private registry names.
- internal Helm values.
- generated local state under `.agentengine/`.
- `node_modules/`.
- hosted-only UI bundles in the Python wheel.
- customer data, traces, logs, or screenshots.

The Python package may include static UI assets needed by `agentengine web`.
Editable UI source belongs to `ksadk-web`.

## Internal Review Before Public Push

The intended publication sequence is:

1. push the review branch to internal ezone.
2. complete internal maintainer review.
3. import reviewed source into `kingsoftcloud/ksadk-python`.
4. import reviewed UI source into `kingsoftcloud/ksadk-web`.
5. enable GitHub Pages.
6. create GitHub releases.
7. publish PyPI or TestPyPI packages.

Do not skip the internal review step. PyPI credentials, TestPyPI credentials,
and release tokens must stay outside GitHub source.

## Clean Export Versus Full History

For the first public import, maintainers must choose one strategy:

| Strategy | When to use | Required evidence |
| --- | --- | --- |
| clean export | history contains internal paths or deployment material | clean export manifest, tree digest, artifact audit, approval record |
| rewritten history | preserving public history is important and feasible | rewrite procedure, content-level history secret scan, reviewer approval |
| full history | only when history audit and content scan are clean | path-level and content-level history evidence |

If the path-level audit reports blocked historical paths, do not import full
history directly. Use the reviewed clean export unless maintainers explicitly
approve a history rewrite and content-level scan.

## Review Bundle

The review bundle should include:

- full diff.
- candidate export manifests.
- clean-export SHA-256 tree digests.
- publication state checks.
- path-level history audit.
- approval request.
- final blocker list.

Reviewers should be able to answer three questions from the bundle:

- what source will become public?
- what was intentionally removed?
- what commands passed before publication?
- which Git history strategy was approved?
