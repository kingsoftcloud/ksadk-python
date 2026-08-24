# KsADK Public Release Approval Record

This record approves the public `0.8.2` release from the reviewed internal
candidate and Web sources below. It is the evidence consumed by the release
gate before GitHub tags, GitHub Releases, PyPI publication, or GitHub Pages
deployment.

## Required Approval Decisions

| Decision | Approved value |
| --- | --- |
| License | Apache-2.0 |
| Python repository | kingsoftcloud/ksadk-python |
| Web UI repository | kingsoftcloud/ksadk-web |
| Python package version | 0.8.2 |
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

- `ksadk-python`: clean public export from reviewed internal candidate `6825af6354eb8f37723992023c44c6ee4201e812`.
- `ksadk-web`: trusted npm package `@kingsoftcloud/ksadk-web@0.3.2`, source commit `2136448e038b4d8c475fa20e4722252b1ddb2ebc`, GitHub merge `4854be4fcb5584a799538536372d38b80447f81e`, integrity `sha512-Ytjd3pIgy6LfHCmguXUDQr/wy9ClqKjbv+J+NAzH/+UIJjhVl3y1SA2eR7WwsWSn42zxBFme/xniUZMNBV53Aw==`; approval is bound to Python source commit `6825af6354eb8f37723992023c44c6ee4201e812`.

Both approved source references include the reviewed Python source commit SHA.
This prevents a stale approval record from passing after candidate changes.

## Recorded Evidence for Approval

- `@kingsoftcloud/ksadk-web@0.3.2` is published from the source and integrity
  recorded above; the Python build gate verified all 265 embedded static files.
- Internal release tests passed 79/79. The focused AgentKernel, packaging, and
  Studio cloud chat regression passed 247 tests with 24 live-PostgreSQL cases
  explicitly skipped when no optional DSN was supplied.
- The docs static build rendered 201 routes. Wheel/sdist metadata, twine, and
  artifact audits passed with 0 violations across 777 wheel and 992 sdist entries.
- `make public-publish-check PUBLIC_PUBLISH_PHASE=pre-publish V=0.8.2` must pass
  again on the exported public candidate before external publication; neither
  public Python package contains version `0.8.2` at approval time.
- Branch protection and publish environment are configured according to
  `.github/BRANCH_PROTECTION.md`.
- Web 0.3.2 tests, lint, build, npm pack, audit, interaction E2E, AG-UI E2E,
  reconnect E2E, npm publication, GitHub Release, and Pages deployment are green.
- Real browser E2E covered Studio build/deploy, an existing CLI high-code Agent,
  foreground streaming and multi-turn chat, approvals, evaluation, traces,
  version rollback, deletion, and cleanup of test resources.
- Release notes, `CHANGELOG.md`, public README and docs were reviewed for the
  complete 0.8.2 summary, sensitive environment names, internal endpoints,
  tokens, customer data and inaccurate claims.
- PyPI/TestPyPI credentials stay outside the repository.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | @AgentArcLab | Approved | 2026-08-25 |
| Security reviewer | @AgentArcLab | Approved after source, artifact and secret gates | 2026-08-25 |
| Release owner | @AgentArcLab | Approved for clean export and Trusted Publishing | 2026-08-25 |
