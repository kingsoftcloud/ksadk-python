# Release Process

The first public release should be prepared from an independent branch and reviewed before any public push, PR, release, package upload, or GitHub Pages deployment.

## Release Order

1. Prepare the candidate on an independent branch.
2. Run local tests, packaging checks, artifact audits, and docs build.
3. Push the candidate pull request for maintainer review.
4. After approval, merge the reviewed source into GitHub `main`.
5. Enable GitHub Pages only after public docs CI passes.
6. Create release tags, GitHub release assets, and TestPyPI/PyPI uploads only
   from the reviewed GitHub `main` commit after public CI passes.
7. Before publication, verify the external state with
   `make public-publish-check PUBLIC_PUBLISH_PHASE=pre-publish V=0.6.6`.
8. After publication, verify the external state with
   `make public-publish-check PUBLIC_PUBLISH_PHASE=post-publish V=0.6.6`.

Public release assets must not be created directly from an unsynced candidate
branch. `make publish`, `make publish-test`, and `make public-release-tag`
require `docs/maintainer-approval-record.md` to name the reviewed commit,
publication strategy, and maintainer sign-offs.

## Required Evidence

- Secret scan report.
- License and SBOM report.
- Public repository file list.
- sdist file list.
- wheel file list.
- GitHub Pages file list.
- CI run links.
- E2E or smoke-test results.
- Release notes.

## Local Commands

```bash
make open-source-review
make open-source-review-bundle
python3 scripts/audit_public_history_paths.py --json --allow-violations
git diff --check
```

The release gate builds and audits the wheel and source distribution, checks
metadata with Twine, verifies the clean-export candidate, and checks the public
docs output.

The history path audit is review evidence for the publication strategy. It is
not a content-level secret scan. If it reports blocked historical paths, the
release owner must choose a reviewed clean export or a reviewed history rewrite
before importing source into GitHub.

## Version Alignment

The release tag, GitHub release, package version, and documentation version
should refer to the same reviewed GitHub `main` commit.
The Python release notes should mention the `ksadk-web` commit or tag used to
generate the embedded static UI. After package publication, the publication
state check verifies that PyPI reports the reviewed version for both public
package names.

## Credential Boundary

PyPI and TestPyPI credentials must stay outside the repository. Prefer PyPI
Trusted Publishing with GitHub OIDC. If a temporary token is required, keep it
in release-system secrets or a maintainer local environment after approval.

Never commit:

- `.pypirc`
- API tokens
- kubeconfig files
- private registry credentials
- customer data or private traces

## Public Import Approval

Before importing real source into GitHub, the approval record must name:

- the exact private branch or commit reviewed.
- whether publication uses clean export, rewritten history, or full history.
- the export manifests or history scan evidence.
- the reviewer who approved the sensitive-data boundary.
- the release owner responsible for GitHub, Pages, and PyPI actions.
