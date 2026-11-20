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

- `ksadk-python`: runtime source proposed for review `bb33d27ba7bc28b78502939ccf4041f102d9e204`, exported from `e281013fbe4789c7b3a669a5ef85af6b09ee7fa8`. The final candidate also contains verification/documentation metadata. PR #124 must review its final head; runtime changes after this anchor require a new recorded anchor and fresh verification.
- `ksadk-web`: published `@kingsoftcloud/ksadk-web@0.3.18`, source `3a1dde1f0b86ebd547d34a4787cc5fa57ea36319`, bundled with Python runtime review anchor `bb33d27ba7bc28b78502939ccf4041f102d9e204`; no npm publication is requested.

## Recorded Evidence for Approval

Branch protection and publish environment must be verified against `.github/BRANCH_PROTECTION.md` before merge/publication.

Verification is attached to the final head of [PR #124](https://github.com/kingsoftcloud/ksadk-python/pull/124). Before sign-off, reviewers must confirm:

- Matching SDK/runtime sources in the clean export and both candidate branches; no direct master/main merge.
- Complete `make public-preflight`, package provenance, cold installs, alias distribution and documentation build for the final public candidate.
- PostgreSQL 9.6–16 matrix and legacy Memory retention, including real 0.8.7-to-candidate SQLite/PostgreSQL checks.
- Real preproduction verification of the same three Agents, including the ordinary Agent with Kernel disabled. OAuth and A2A must use separate test phases.
- Memory identity tests cover the 68-character trusted user key, preserve internal/session identities and existing valid cloud IDs, and use one encoding for save/query/status/update/forget.

Earlier 0.8.7 or earlier 0.8.8 candidate results do not approve this final candidate. The source references above identify the proposed runtime, not a reviewer decision. The role decisions below remain Pending until the actual reviewers sign.

## Known Limitations and Security Disposition

The existing Web 0.3.18 dependency findings and unsupported A2UI `Card.theme` behavior remain subject to review. This source synchronization does not claim to resolve them or validate every cloud deployment. SDK release evidence schema v3 excludes the retired Studio browser check; browser acceptance belongs to the separate Studio repository.

The production-dependency audit of the unchanged Studio/Web lock on 2026-11-20 reported 17 findings: 9 high, 3 moderate and 5 low. These are dependency findings, not proof that every advisory is reachable in a prebuilt Hosted UI. The Security reviewer must record whether to accept the existing exposure for 0.8.8 or require an upstream Web update; this record does not make that decision.

This release limits the additional identity repair to outbound Memory `AgentUserId`. It does not shorten trusted identity keys or change local Memory namespaces, business SessionIds, KnowledgeBaseIds, database schemas, Server/Gateway or the Web version. Custom cloud Memory SessionIds must continue to satisfy the service's existing limit. No-Kernel OAuth MCP remains unsupported by its existing contract.

No dedicated preproduction Memory collection is configured for this validation. The requester accepted protocol-boundary tests for `AgentUserId` in place of a live cloud Memory CRUD test. Do not report cloud Memory CRUD as passed; legacy local persistence and the other deployed Agent features have separate verification evidence.

After sign-off, the release owner must use the recorded runtime anchor as `approved_source_commit` for the trusted publish workflow (or update the repository variable to that approved value). Do not reuse an earlier release's value. The Release/tag and package build must still originate from the final reviewed GitHub main commit. Do not trigger publication while any sign-off is Pending.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | Pending | Pending | Pending |
| Security reviewer | Pending | Pending | Pending |
| Release owner | Pending | Pending | Pending |
