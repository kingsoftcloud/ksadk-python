# KsADK Public Release Approval Record

This record approves the public `0.8.3` release candidate described below. It is the evidence consumed by the release
gate before GitHub tags, GitHub Releases, PyPI publication, or GitHub Pages
deployment.

## Required Approval Decisions

| Decision | Approved value |
| --- | --- |
| License | Apache-2.0 |
| Python repository | kingsoftcloud/ksadk-python |
| Web UI repository | kingsoftcloud/ksadk-web |
| Python package version | 0.8.3 |
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

- `ksadk-python`: Reviewed source commit `4234cd183e0d8c6599618ae9769d2327849408a5` (release head after the Phase 2 composer/session-switch fix, public-export gate alignment, and PyPI publish workflow chromium fix); publication uses a clean public export of that reviewed candidate plus release-only evidence updates.
- `ksadk-web`: GitHub tag `v0.3.4` at `63b30782e9771357185406cb99b504ac3d48a165`; npm integrity `sha512-IudZCNnWAWYJOb/s/lbr02qg17KWQ0s/419StDVZxcEcbJOVVKE4GkbGtGs/5X+WkzbXE9eOUvIEydN5QEV4LQ==`; consumer binding reviewed at `4234cd183e0d8c6599618ae9769d2327849408a5`.

Both approved source references include the reviewed Python source commit SHA.
This prevents a stale approval record from passing after candidate changes.

## Recorded Evidence for Approval

- The published `@kingsoftcloud/ksadk-web@0.3.4` package passed source tests,
  browser E2E and registry-backed consumer rebuild. Its registry tarball
  SHA-256 is `0d88fb37506bae77ba863b3986b2fde4546cd74cbd3f3021eed1ecd05f15c596`.
- The Phase 2 compatibility, Codex native host, DSH lifecycle, browser, clean
  wheel install and clean sdist rebuild gates passed on the interim Python
  candidate. Wheel/sdist path and content audits reported zero violations.
- The docs static build rendered 205 routes. Public source export and secret
  audits must pass again after the final registry-backed rebuild.
- `make public-publish-check PUBLIC_PUBLISH_PHASE=pre-publish V=0.8.3` must pass
  on the exported public candidate before external publication; neither public
  Python package may already contain version `0.8.3` at approval time.
- Branch protection and publish environment are configured according to
  `.github/BRANCH_PROTECTION.md`.
- Web 0.3.4 source tests, lint, build, npm pack, audit, Pages demo E2E and browser E2E are green;
  npm publication, registry verification and the final registry-backed consumer
  rebuild are complete.
- Real browser E2E for 0.3.4 passed against a Studio-created Codex Agent and a
  historical 0.8.2 Agent: multi-turn context, reasoning, refresh replay and
  final-message de-duplication bind to Hosted UI image digest
  `sha256:d629384e44a2e35f5dd5f7788ea16097cb49d79c582206d5fe453911fe20d66d`.
- Release notes, `CHANGELOG.md`, public README and docs were reviewed for the
  complete 0.8.3 summary, sensitive environment names, internal endpoints,
  tokens, customer data and inaccurate claims.
- PyPI/TestPyPI credentials stay outside the repository.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | @AgentArcLab | Approved | 2026-09-01 |
| Security reviewer | @AgentArcLab | Approved | 2026-09-01 |
| Release owner | @AgentArcLab | Approved | 2026-09-01 |
