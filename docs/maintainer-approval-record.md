# KsADK Public Release Approval Record

This is the 0.8.0 release-candidate record. It must be completed after
maintainer review and before any external release write action, including
GitHub release tags, GitHub Releases, TestPyPI, or PyPI publication.

Candidate preparation is not approval. Blank or pending sign-offs below must
remain blocking until the reviewed public candidate and staging evidence exist.

## Required Approval Decisions

| Decision | Approved value |
| --- | --- |
| License | Apache-2.0 |
| Python repository | kingsoftcloud/ksadk-python |
| Web UI repository | kingsoftcloud/ksadk-web |
| Python package version | 0.8.0 |
| Public docs URL | https://kingsoftcloud.github.io/ksadk-python/ |
| Package metadata repository URL | https://github.com/kingsoftcloud/ksadk-python |
| Package metadata documentation URL | https://kingsoftcloud.github.io/ksadk-python/ |
| Security contact | security@kingsoft.com |

## Publication Strategy

Record exactly one approved source publication strategy.

| Strategy | Approved |
| --- | --- |
| Reviewed GitHub pull request | Pending |
| Clean export from reviewed candidate | Pending |
| Rewritten Git history after secret scan | No |

The approved strategy must name the reviewed commit, tag, pull request, or
export archive used for:

- `ksadk-python`: candidate branch `feat/0.8-runtime-foundation`; the reviewed commit, clean-export archive and public-candidate commit are pending review.
- `ksadk-web`: candidate branch `feat/0.8-agui-a2ui-web`, prepared as `@kingsoftcloud/ksadk-web@0.3.0`; trusted npm publication and the exact source commit are pending review.

Both approved source references must include the current commit SHA at approval
time. This prevents a stale approval record from passing after candidate
changes.

## Required Evidence Before Approval

- `make public-preflight` exits successfully after the reviewed `ksadk-web@0.3.0`
  package is available in npm.
- `make public-publish-check PUBLIC_PUBLISH_PHASE=pre-publish V=0.8.0` confirms
  the target version is not already on PyPI.
- Branch protection and publish environment are configured according to
  `.github/BRANCH_PROTECTION.md`.
- Staging E2E covers the reviewed runtime images and control-plane candidate,
  including AG-UI/A2UI Hosted interaction and the Codex provider path when an
  approved credential is available.
- GitHub PR checks are green on the reviewed commit.
- Release notes and `CHANGELOG.md` are reviewed, including the complete 0.8.0 summary.
- Public README and docs were reviewed for sensitive environment names,
  internal endpoints, tokens, customer data, and inaccurate competitor claims.
- PyPI/TestPyPI credentials stay outside the repository.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer |  | Pending review |  |
| Security reviewer |  | Pending review |  |
| Release owner |  | Pending review |  |
