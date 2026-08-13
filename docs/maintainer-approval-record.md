# KsADK Public Release Approval Record

This record approves the public `0.8.1` release from the reviewed clean-export
candidate and release-gate fix below. It is the evidence consumed by the release
gate before GitHub tags, GitHub Releases, PyPI publication, or GitHub Pages
deployment.

## Required Approval Decisions

| Decision | Approved value |
| --- | --- |
| License | Apache-2.0 |
| Python repository | kingsoftcloud/ksadk-python |
| Web UI repository | kingsoftcloud/ksadk-web |
| Python package version | 0.8.1 |
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

- `ksadk-python`: reviewed public candidate commit `f17cf2d06985cabac456825391e37999c95dc8d0`, prepared from clean-export candidate `f14d5faafdb6e76dd6616a951cabe28ba3708075` using the repository's public export policy and updated only with the reviewed release-gate fixes.
- `ksadk-web`: trusted npm package `@kingsoftcloud/ksadk-web@0.3.1`, source commit `b4e9f938828ef669347dadb7f0eb3f0a01747a6a`, integrity `sha512-p+PzgC/0ZcQXoEpoI5VezAB4FQkddstXiW1OQtfH/bPYOBAv4xyGMwBylEegae1IBcGlq9inUNuQRFez/IRRgQ==`; approval is bound to reviewed Python public candidate commit `f17cf2d06985cabac456825391e37999c95dc8d0`.

Both approved source references include the reviewed public candidate SHA
`f17cf2d06985cabac456825391e37999c95dc8d0`. This prevents a stale approval
record from passing after candidate changes.

## Recorded Evidence for Approval

- `@kingsoftcloud/ksadk-web@0.3.1` was resolved from the public npm registry;
  the public preflight verified all 251 embedded static files with
  SHA-256 `33534137fdd48c8a44ce65640457f294bc04fe254212fae13178a7e3c89e6ad4`.
- `make public-preflight` passed for the candidate: release-version, secret,
  public-source, docs, wheel, sdist, static-resource and package-metadata
  audits passed; the public test set reported `80 passed` and the docs build
  generated 197 static pages.
- `make public-publish-check PUBLIC_PUBLISH_PHASE=pre-publish V=0.8.1` passed;
  neither public Python package already contains version `0.8.1`.
- The protected GitHub `main` branch requires its configured `test`, `scan` and
  `analyze` checks before merge; the release proceeds only after those checks
  pass on the public pull request.
- Release notes, `CHANGELOG.md`, public README and docs were included in the
  clean export and covered by the public source and secret audits. PyPI
  credentials remain outside the repository.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | @AgentArcLab | Approved | 2026-08-13 |
| Security reviewer | @AgentArcLab | Approved after public secret and package audits | 2026-08-13 |
| Release owner | @AgentArcLab | Approved for Trusted Publishing after required GitHub checks | 2026-08-13 |
