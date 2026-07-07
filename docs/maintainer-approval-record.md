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
| Python package version | 0.6.9 |
| Public docs URL | https://kingsoftcloud.github.io/ksadk-python/ |
| Package metadata repository URL | https://github.com/kingsoftcloud/ksadk-python |
| Package metadata documentation URL | https://kingsoftcloud.github.io/ksadk-python/ |
| Security contact | security@kingsoft.com |

## Publication Strategy

Record exactly one approved source publication strategy. This candidate has
not been approved for external writes yet.

| Strategy | Approved |
| --- | --- |
| Reviewed GitHub pull request | No |
| Clean export from reviewed candidate | No |
| Rewritten Git history after secret scan | No |

The approved strategy must name the reviewed commit, tag, pull request, or
export archive used for:

- `ksadk-python`: pending clean export candidate from internal commit `dd220443a3ae0b608129c83c446104c6017f784b`; local candidate directory `/tmp/ksadk-python-export-candidate-0.6.9`; verified with `KSADK_WEB_VERSION=0.2.18 KSADK_WEB_RELEASE_URL=file:///tmp/ksadk-web-pack.T9pE2L/kingsoftcloud-ksadk-web-0.2.18.tgz make public-preflight` on 2026-07-08.
- `ksadk-web`: pending npm package `@kingsoftcloud/ksadk-web@0.2.18` from commit `e3eba982c4aeb3289ff2b4620e348868a9fdebd7`; current Python candidate commit `dd220443a3ae0b608129c83c446104c6017f784b`; npm registry still reports `0.2.17`, so official registry-bundled preflight is still pending.

Both approved source references must include the current commit SHA at approval
time. This prevents a stale approval record from passing after candidate
changes.

## Required Evidence Before Approval

- `make public-preflight` exits successfully.
- `make public-publish-check PUBLIC_PUBLISH_PHASE=pre-publish V=0.6.9` confirms
  the target version is not already on PyPI.
- Branch protection and publish environment are configured according to
  `.github/BRANCH_PROTECTION.md`.
- Staging E2E for the reviewed runtime images and control-plane candidate exits
  successfully before GitHub Release, PyPI, or npm workflows are approved.
- Hosted workspace zip export, model policy defaults, fallback behavior,
  Hermes/OpenClaw default images, long-task resume, and terminal reconnect are
  covered by the staging E2E evidence.
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
