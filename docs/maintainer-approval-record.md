# KsADK Public Release Approval Record

This record must be filled after maintainer review and before any external
write action, including GitHub release tags, GitHub Releases, TestPyPI, or
PyPI publication.

## Required Approval Decisions

| Decision | Approved value |
| --- | --- |
| License | Apache-2.0 |
| Python repository | kingsoftcloud/ksadk-python |
| Web UI repository | kingsoftcloud/ksadk-web |
| Python package version | 0.6.4 |
| Public docs URL | https://kingsoftcloud.github.io/ksadk-python/ |
| Package metadata repository URL | https://github.com/kingsoftcloud/ksadk-python |
| Package metadata documentation URL | https://kingsoftcloud.github.io/ksadk-python/ |
| Security contact | security@kingsoft.com |

## Publication Strategy

Record exactly one approved source publication strategy:

| Strategy | Approved |
| --- | --- |
| Reviewed GitHub pull request | No |
| Clean export from reviewed candidate | No |
| Rewritten Git history after secret scan | No |

The approved strategy must name the reviewed commit, tag, pull request, or
export archive used for:

- `ksadk-python`: TBD
- `ksadk-web`: TBD

Both approved source references must include the current commit SHA at approval
time. This prevents a stale approval record from passing after candidate
changes.

## Required Evidence Before Approval

- `make public-preflight` exits successfully.
- `make public-publish-check PUBLIC_PUBLISH_PHASE=pre-publish V=0.6.4` confirms
  the target version is not already on PyPI.
- GitHub PR checks are green on the reviewed commit.
- Release notes and `CHANGELOG.md` were reviewed.
- Public README and docs were reviewed for sensitive environment names,
  internal endpoints, tokens, customer data, and inaccurate competitor claims.
- PyPI/TestPyPI credentials stay outside the repository.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer |  |  |  |
| Security reviewer |  |  |  |
| Release owner |  |  |  |
