# Branch Protection Baseline

Apply this baseline to the public `main` branch. Update it through reviewed
public candidate pull requests; preserve public history and keep the internal
`master` branch in the internal repository.

## Required branch rules

- Require a pull request before merging.
- Require approvals from maintainers before merge.
- Require status checks to pass before merging.
- Require branches to be up to date before merging.
- Do not allow force pushes.
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
- The public candidate must pass the complete local `make public-preflight`
  and required PR checks before merge. `Publish PyPI` rechecks
  `make public-preflight-publish` and `make public-publish-gate`, then builds
  and audits both Python distributions before the first upload.
