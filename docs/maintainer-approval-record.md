# KsADK Public Release Approval Record

This record authorizes the reviewed public 0.8.7 release through the protected GitHub publishing workflow. It records the actual PR review and the release task owner's authorization. The Web npm version remains 0.3.18. Production Agent changes and branch deletion are outside this release.

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
| Reviewed GitHub pull request | Yes |
| Clean export from reviewed candidate | No |
| Rewritten Git history after secret scan | No |

The candidate was prepared through clean export and reviewed in [PR #116](https://github.com/kingsoftcloud/ksadk-python/pull/116). AgentArcLab approved candidate `f86bbefa379912195e8ae86c48751be9b5dfc0f9`; the reviewed code was merged into public main on 2026-11-12. The release metadata follow-up changes documentation and approval evidence only. The release tag must point to the final main commit containing this record.

- `ksadk-python`: Reviewed public main code commit `5482528ce166f1c7e8d96f1f3865dc884b88e2fc`, merged from the approved PR #116. The final tag/main commit and workflow run must be recorded with the release.
- `ksadk-web`: Published `@kingsoftcloud/ksadk-web@0.3.18`, source commit `3a1dde1f0b86ebd547d34a4787cc5fa57ea36319`, archive SHA-256 `da5ab06448e57c33c351d405d39bd38b173e7b7f6f24bfda6f3b6aa1ac101aff`. Approved consumer code commit: `5482528ce166f1c7e8d96f1f3865dc884b88e2fc`.

## Recorded Evidence for Approval

- PR #116 approval by AgentArcLab is recorded in GitHub against the final candidate. Release owner lijiafu618-alt authorized publication of 0.8.7 after review and confirmed approval on 2026-11-12. The same recorded PR review supplies the maintainer and security-review decisions for this reviewed scope; no separate unrecorded security assessment is asserted.
- The SDK/runtime trees are unchanged from tested commit `60263097`; the final candidate adds the reviewed local OAuth fixture redirect fix. Release metadata changes no runtime, dependency, database schema, workflow permissions, or package version.
- Prior candidate `33b00f2c` passed all 25 GitHub checks, the complete local public preflight, PostgreSQL 9.6.21 through 16 checks, separate wheel/sdist installations, and documentation audits. Final metadata/main verification is recorded in the release PR and trusted workflow; earlier results are not relabeled as new executions.
- Both Python distributions use 0.8.7. Build, artifact, alias and approval gates must pass before the first upload. Tests remain in the repository and are excluded from Python distributions.
- Branch protection and publish environment must be verified against `.github/BRANCH_PROTECTION.md` before merge/publication. Public main is protected; the PyPI environment requires AgentArcLab approval. These protections remain in force.

## Known Limitations and Security Disposition

- The 2026-11-12 production npm audit reported 1 critical and 9 high findings, including transitive dependency aggregation. Official Web 0.3.18 includes DOMPurify 3.4.11 and 3.4.14; upstream advisories have configuration-specific prerequisites. This release retains the reviewed official Web archive and the disclosed findings. PR approval is not a claim that these dependencies were upgraded or that every finding is unreachable.
- Complex-theme A2UI generation can emit an unsupported `Card.theme` property. The registered catalog correctly rejects it. Basic and dynamic forms and resume were verified; arbitrary model-generated styling is not guaranteed and validation is not relaxed.
- Sandbox and all possible multi-replica/provider configurations are outside the recorded environment evidence. No production Agent is deployed by this release.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | AgentArcLab | Approved | 2026-11-12 |
| Security reviewer | AgentArcLab | Approved | 2026-11-12 |
| Release owner | lijiafu618-alt | Approved | 2026-11-12 |
