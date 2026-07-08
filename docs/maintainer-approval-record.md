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

Record exactly one approved source publication strategy.

| Strategy | Approved |
| --- | --- |
| Reviewed GitHub pull request | No |
| Clean export from reviewed candidate | Yes |
| Rewritten Git history after secret scan | No |

The approved strategy must name the reviewed commit, tag, pull request, or
export archive used for:

- `ksadk-python`: clean export candidate from reviewed internal commit `eb76b17d7d3c176f7cf6126ceb001ab00f5d651d`; local candidate directory `/tmp/ksadk-python-export-candidate-0.6.9`; verified on 2026-07-08 with public source audit, registry-bundled `make public-preflight` in the public candidate worktree, Fumadocs static build, wheel/sdist build, twine check, and source/dist package audits.
- `ksadk-web`: npm package `@kingsoftcloud/ksadk-web@0.2.18` from commit `24551d0f290e5a4efc5b5d60d02fa298cccd2efa`; Python candidate commit `eb76b17d7d3c176f7cf6126ceb001ab00f5d651d`; published by the trusted GitHub npm workflow on 2026-07-08 and consumed from the npm registry during `make public-preflight`.

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
| Maintainer | xiayu | Approved clean export candidate for ksadk 0.6.9 | 2026-07-08 |
| Security reviewer | automated public audit | Passed source, wheel, and sdist audits with 0 violations | 2026-07-08 |
| Release owner | xiayu | Approved trusted GitHub PyPI and Pages workflow for 0.6.9 | 2026-07-08 |
