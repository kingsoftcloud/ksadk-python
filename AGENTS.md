# AGENTS.md

This repository is the public `ksadk-python` project.

## Working Agreement

- Keep public changes scoped to the Python SDK, CLI, runtime adapters, public
  documentation, and package metadata.
- Keep Web UI source changes in `https://github.com/kingsoftcloud/ksadk-web`.
  This repository may consume reviewed static build output, but the editable UI
  source belongs to the Web UI repository.
- Prefer small pull requests with tests and documentation updates when behavior
  changes.
- Run the focused public checks before submitting changes:

```bash
uv run --extra dev pytest tests/test_open_source_audit.py tests/test_runtime_common_packaging.py -q
uv run --extra dev python -m mkdocs build --strict
make open-source-audit
```

## Public Boundary

Do not commit credentials, private endpoints, kubeconfig files, local absolute
paths, generated `.zread` output, internal deployment manifests, private
registry details, or PyPI/TestPyPI upload configuration.

Public documentation lives in `public-docs/`. Internal planning documents,
temporary scripts, local generated data, and private deployment notes do not
belong in the public repository.

## Release Notes

The first public Python release is `0.6.1`. Release notes should link:

- Documentation: `https://kingsoftcloud.github.io/ksadk-python/`
- Repository: `https://github.com/kingsoftcloud/ksadk-python`
- Web UI repository: `https://github.com/kingsoftcloud/ksadk-web`
- PyPI: `https://pypi.org/project/ksadk/`
