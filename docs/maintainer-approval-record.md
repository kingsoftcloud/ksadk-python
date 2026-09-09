# KsADK Public Release Approval Record

This record approves the public `0.8.4` release candidate described below. It is the evidence consumed by the release gate before GitHub tags, GitHub Releases, PyPI publication, or GitHub Pages deployment.

## Required Approval Decisions

| Decision | Approved value |
| --- | --- |
| License | Apache-2.0 |
| Python repository | kingsoftcloud/ksadk-python |
| Web UI repository | kingsoftcloud/ksadk-web |
| Python package version | 0.8.4 |
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

The approved strategy names the reviewed source used for both release artifacts:

- `ksadk-python`: Clean public export of reviewed source commit `e3a9a3633bb2f9e990479585daf96b8eec588254`; publication uses the corresponding reviewed public pull request and GitHub Trusted Publishing.
- `ksadk-web`: GitHub source commit `6a2814e17f361f452396c7265049190f2e668a0c` published as `@kingsoftcloud/ksadk-web@0.3.7`; npm integrity `sha512-Js1Tk9Fo19QUgth1gXRBxZNEbtk5p81bMkX1ncdPRN4llldpfi3apAcaMxprsfI8V23cqf7pIKZIkeSDSAMZ8w==`; Python consumer binding reviewed in source commit `e3a9a3633bb2f9e990479585daf96b8eec588254`.

Both approved source references include the reviewed Python source commit SHA so a changed candidate cannot reuse this record.

## Recorded Evidence for Approval

- The published `@kingsoftcloud/ksadk-web@0.3.7` package has npm shasum `5b6a297cb9516c9de9af06883e4cb7695c60a83e` and registry tarball SHA-256 `1a5127acbcf7b413efb3b460dc1b1c162059a05c5eba70c1aa25a074b6f5867b`.
- Direct OpenAI-compatible Responses traffic retains the established public executor route, while Server JWKS permits remain scoped to `/agent-kernel/v1/*`.
- Studio binds official knowledge base, long-term memory, and Skill Center resources through Agent Revision, immutable Build references, and Runtime Activation authorization.
- Studio deployment Build rows reserve bounded columns for state, Build ID, and creation time. The observability Trace list uses ten-row cursor pages inside a scrollable workbench.
- Public README files use current Studio captures and keep version-specific notes in GitHub Releases. The Fumadocs static export rendered and audited 205 routes.
- Frontend contract tests, focused deployment and observability component tests, the production Studio build, public positioning tests, and public source audit passed on the reviewed candidate.
- The complete `make public-preflight` and publication-state checks must pass on the clean public pull request before external publication.
- Branch protection and publish environment are configured according to `.github/BRANCH_PROTECTION.md`.
- PyPI publication uses the protected GitHub environment and Trusted Publishing; repository files contain no publication token.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | @AgentArcLab | Approved | 2026-09-09 |
| Security reviewer | @AgentArcLab | Approved | 2026-09-09 |
| Release owner | @AgentArcLab | Approved | 2026-09-09 |
