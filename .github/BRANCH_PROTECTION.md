# Branch Protection Baseline

Apply this baseline to the public `main` branch after the one-time `new-main`
overwrite push is complete.

## Required branch rules

- Require a pull request before merging.
- Require approvals from maintainers before merge.
- Require status checks to pass before merging.
- Require branches to be up to date before merging.
- Do not allow force pushes after the one-time bootstrap push.
- Do not allow deletions.
- Require conversation resolution before merging.

## Required status checks

- `CI / test`
- `Secret Pattern Audit / scan`
- `CodeQL / analyze`
- `Release Check / artifacts` when package, manifest, workflow, or release-audit
  paths change.

## Publish protection

- The `pypi environment` must require maintainer approval before deployment.
- `Publish PyPI` must remain limited to GitHub Release `published` and manual
  `workflow_dispatch` events. It must not run on plain `push`.
- PyPI publishing must pass `make public-preflight` and `make public-publish-gate`
  before `pypa/gh-action-pypi-publish`.
