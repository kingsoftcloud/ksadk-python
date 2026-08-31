# KsADK Public Release Approval Record

This record tracks the public `0.8.3` release candidate and remains unapproved
until the final Python and Web artifacts below are fixed. It is the evidence consumed by the release
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

- `ksadk-python`: Pending final reviewed internal source commit and clean public export.
- `ksadk-web`: Pending Trusted Publishing of `@kingsoftcloud/ksadk-web@0.3.3`, registry integrity, and final Python source binding.

Both approved source references include the reviewed Python source commit SHA.
This prevents a stale approval record from passing after candidate changes.

## Recorded Evidence for Approval

- The reviewed `@kingsoftcloud/ksadk-web@0.3.3` source candidate passed its
  source tests and browser E2E; the Python candidate verified all 265 embedded
  static files from the explicit reviewed tarball. This does not substitute for
  the registry-backed rebuild required after Trusted Publishing.
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
- Web 0.3.3 source tests, lint, build, npm pack, audit and browser E2E are green;
  npm publication, registry verification and the final registry-backed consumer
  rebuild are still pending approval.
- Real browser E2E on the reviewed candidate covered a Studio-created Codex
  Agent and a historical 0.8.2 Agent: streaming text, reasoning/tool cards,
  multi-turn context and final-message de-duplication.
- Release notes, `CHANGELOG.md`, public README and docs were reviewed for the
  complete 0.8.3 summary, sensitive environment names, internal endpoints,
  tokens, customer data and inaccurate claims.
- PyPI/TestPyPI credentials stay outside the repository.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | Pending | Pending | Pending |
| Security reviewer | Pending | Pending | Pending |
| Release owner | Pending | Pending | Pending |
