# KsADK Public Release Approval Record

This record approves the public `0.8.0` release from the reviewed GitHub
sources below. It is the evidence consumed by the release gate before GitHub
tags, GitHub Releases, PyPI publication, or GitHub Pages deployment.

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
| Reviewed GitHub pull request | Yes |
| Clean export from reviewed candidate | No |
| Rewritten Git history after secret scan | No |

The approved strategy must name the reviewed commit, tag, pull request, or
export archive used for:

- `ksadk-python`: reviewed GitHub `main` source commit `a76f2de7565ffe34d44a9d17257401fa805de0de`, merged through PR #43 and README hotfix PR #44.
- `ksadk-web`: trusted npm package `@kingsoftcloud/ksadk-web@0.3.0`, source commit `a35ee0411ee0c2a3d64730be4c8ababe4712c59a`, integrity `sha512-AHs2blwhZiMf1AtwMMsjIkzEe09GbQJwvKgLiA0k4rkqECWdPaDF5QnEUOrbdWCLnHZwz5HQp/NCAkG00zwdzA==`; approval is bound to Python source commit `a76f2de7565ffe34d44a9d17257401fa805de0de`.

Both approved source references must include the current commit SHA at approval
time. This prevents a stale approval record from passing after candidate
changes.

## Recorded Evidence for Approval

- `@kingsoftcloud/ksadk-web@0.3.0` is published from the source and integrity
  recorded above; the Python preflight verified all 251 embedded static files.
- `make public-preflight` passed with source, secret, docs, wheel, sdist,
  static-resource and package-metadata audits.
- `make public-publish-check PUBLIC_PUBLISH_PHASE=pre-publish V=0.8.0` passed;
  neither public Python package already contains version `0.8.0`.
- Branch protection and publish environment are configured according to
  `.github/BRANCH_PROTECTION.md`.
- GitHub PR checks are green on the reviewed commit, including CI, CodeQL,
  Secret Pattern Audit, artifact checks, two Google ADK versions, and Codex
  native smoke on macOS, Windows, and Linux.
- Local browser E2E covered Codex init, native Web startup and a real Codex
  response. ManagedRuntime cloud rollout remains separately gated by the
  environment Runtime catalog; this public SDK release makes no production
  deployment claim.
- Release notes, `CHANGELOG.md`, public README and docs were reviewed for the
  complete 0.8.0 summary, sensitive environment names, internal endpoints,
  tokens, customer data and inaccurate claims.
- PyPI/TestPyPI credentials stay outside the repository.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | @AgentArcLab | Approved | 2026-07-29 |
| Security reviewer | @AgentArcLab | Approved after CI, CodeQL and secret audit | 2026-07-29 |
| Release owner | @AgentArcLab | Approved for Trusted Publishing | 2026-07-29 |
