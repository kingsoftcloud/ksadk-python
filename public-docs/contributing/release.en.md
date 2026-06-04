# Release Process

The first public release should be prepared from an independent branch and reviewed before any public push, PR, release, package upload, or GitHub Pages deployment.

## Release Order

1. Prepare the candidate on an independent branch.
2. Run local tests, packaging checks, artifact audits, and docs build.
3. Push to the internal ezone repository for company review.
4. After approval, import the reviewed source into GitHub `main`.
5. Enable GitHub Pages only after public docs CI passes.
6. Create release tags, GitHub release assets, and TestPyPI/PyPI uploads only
   from the reviewed GitHub `main` commit after public CI passes.
7. Verify published releases and tags with
   `python3 scripts/check_publication_state.py --phase published --check-release --check-pages`.

GitHub source import must not happen before the internal review gate. The
candidate branch is for review and local gates only; public release assets must
not be created directly from an internal `master` branch or an unsynced
`release/public-*` candidate branch.
The published-state check also verifies that the imported GitHub repositories
contain the expected public source, CI workflow, and Pages workflow marker
files after the placeholder file is removed.
Release-state checks also verify that the Python GitHub release includes the
reviewed sdist and wheel assets and that release notes record the pinned
`ksadk-web` release.

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
The Python release notes must mention the `ksadk-web` tag used to generate the
embedded static UI, and the release assets should include both
`ksadk-0.6.2.tar.gz` and `ksadk-0.6.2-py3-none-any.whl`.
After package publication, the publication-state check verifies that PyPI
reports package version `0.6.2`, exposes both sdist and wheel files for that
version, and links metadata to the public GitHub repository and GitHub Pages
documentation.

## Credential Boundary

PyPI and TestPyPI credentials must stay outside the repository. Use local
`.pypirc`, environment variables, or GitHub Actions secrets after approval.

Never commit:

- `.pypirc`
- API tokens
- kubeconfig files
- private registry credentials
- customer data or private traces

## Public Import Approval

Before importing real source into GitHub, the approval record must name:

- the exact internal branch or commit reviewed.
- whether publication uses clean export, rewritten history, or full history.
- the export manifests or history scan evidence.
- the reviewer who approved the sensitive-data boundary.
- the release owner responsible for GitHub, Pages, and PyPI actions.
