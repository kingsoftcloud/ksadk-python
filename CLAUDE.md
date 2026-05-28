# CLAUDE.md

This file gives public, contributor-safe guidance for AI coding assistants.

## Repository Scope

`ksadk-python` contains the public Python SDK, CLI, runtime adapters, packaged
static UI output, and MkDocs documentation. Editable Web UI source is maintained
in `https://github.com/kingsoftcloud/ksadk-web`.

## Before Editing

- Read nearby code and tests before changing behavior.
- Keep generated artifacts, credentials, local machine paths, private
  deployment files, and internal operational notes out of commits.
- Preserve Apache-2.0 licensing and public-facing links.

## Checks

Use focused checks while iterating:

```bash
uv run --extra dev pytest tests/test_open_source_audit.py tests/test_runtime_common_packaging.py -q
uv run --extra dev python -m mkdocs build --strict
make open-source-audit
```

For release work, also run:

```bash
uv build
uv run --extra dev python -m twine check dist/*
make open-source-audit-dist
```

Never store or commit PyPI credentials, `.pypirc`, GitHub tokens, kubeconfig
files, private registry credentials, or real model provider API keys.
