# KsADK Public Release Approval Record

Candidate only: this record does not authorize PyPI publication. This is the pending approval record for the 0.8.8 source candidate. It does not authorize publication. No reviewer sign-off from 0.8.7 is carried forward. The Web npm version remains 0.3.18.

## Required Approval Decisions

| Decision | Approved value |
| --- | --- |
| License | Apache-2.0 |
| Python repository | kingsoftcloud/ksadk-python |
| Web UI repository | kingsoftcloud/ksadk-web |
| Python package version | 0.8.8 |
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

The 0.8.8 candidate starts from the current internal baseline and retains reviewed public fixes.
This candidate is not a publication approval. See the candidate PR for its final source commit and fresh verification.

- `ksadk-python`: synchronization baseline `e1eaa877bd6904b14e4a7e33129db5efee1eb0d7`; the final reviewed public commit is pending.
- `ksadk-web`: published `@kingsoftcloud/ksadk-web@0.3.18`, source `3a1dde1f0b86ebd547d34a4787cc5fa57ea36319`; no npm publication is requested.

## Recorded Evidence for Approval

Branch protection and publish environment must be verified against `.github/BRANCH_PROTECTION.md` before merge/publication.

Pending. Fresh source, protocol, database, clean-install, artifact and documentation evidence must be recorded on the final candidate. Previous 0.8.7 results are archived and do not approve 0.8.8.

## Known Limitations and Security Disposition

The existing Web 0.3.18 dependency findings and unsupported A2UI `Card.theme` behavior remain subject to review. This source synchronization does not claim to resolve them or validate every cloud deployment. SDK release evidence schema v3 excludes the retired Studio browser check; browser acceptance belongs to the separate Studio repository.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | Pending | Pending | Pending |
| Security reviewer | Pending | Pending | Pending |
| Release owner | Pending | Pending | Pending |
