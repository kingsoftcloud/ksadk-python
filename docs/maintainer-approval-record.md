# KsADK Public Release Approval Record

Candidate only: this record prepares public `0.8.7` and **does not authorize PyPI publication**, tags, GitHub Releases, npm publication, or Pages deployment. The task author authorized code alignment, version/documentation updates and a reviewable public candidate only. Historical approval for 0.8.5 or 0.8.6 cannot authorize this candidate.

## Required Approval Decisions

| Decision | Approved value |
| --- | --- |
| License | Apache-2.0 |
| Python repository | kingsoftcloud/ksadk-python |
| Web UI repository | kingsoftcloud/ksadk-web |
| Python package version | 0.8.7 |
| Public docs URL | https://kingsoftcloud.github.io/ksadk-python/ |
| Package metadata repository URL | https://github.com/kingsoftcloud/ksadk-python |
| Package metadata documentation URL | https://kingsoftcloud.github.io/ksadk-python/ |
| Security contact | security@kingsoft.com |

## Publication Strategy

| Strategy | Approved |
| --- | --- |
| Reviewed GitHub pull request | No |
| Clean export from reviewed candidate | Yes |
| Rewritten Git history after secret scan | No |

The strategy above selects the candidate preparation method. It grants no external publication permission.

- `ksadk-python`: Candidate based on internal source commit `ebf95157cee3b939f5edfd06af38e8d60cc9313f`, aligned with public main commit `82ff427cb33bfa82186aac66ff9436d21669c1df`. The final aligned source is attested by the generated export manifest; final source review remains required.
- `ksadk-web`: Published `@kingsoftcloud/ksadk-web@0.3.18`, source commit `3a1dde1f0b86ebd547d34a4787cc5fa57ea36319`, archive SHA-256 `da5ab06448e57c33c351d405d39bd38b173e7b7f6f24bfda6f3b6aa1ac101aff`. Consumer baseline: `ebf95157cee3b939f5edfd06af38e8d60cc9313f`.

## Recorded Evidence for Approval

- Preserved internal runtime behavior and the public main fixes using focused patches; no source paths or branches are deleted by candidate preparation.
- Reviewed companion assets retain their published archive and build-tree digests; generated static payloads belong in distributions, not Git source.
- Candidate tests, browser checks, PostgreSQL checks, clean installation and full `make public-preflight` must complete on the frozen source. Only actual completed results are accepted as evidence.
- Both Python distribution names use version 0.8.7 and must pass artifact and dependency checks before any upload. The alias owns its own extras expansion.
- Branch protection and publish environment must be verified against `.github/BRANCH_PROTECTION.md` before merge/publication. Repository documentation is not evidence of remote configuration.
- Formal maintainer, security and release-owner decisions are pending. `make public-publish-gate` is expected to reject this record until a later explicitly authorized publication task records real sign-offs and final source identity.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | Pending | Pending | — |
| Security reviewer | Pending | Pending | — |
| Release owner | Pending | Pending | — |
