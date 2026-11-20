# KsADK Public Release Approval Record

Candidate review signed off: this record does not authorize PyPI publication. It records the actual 0.8.8 candidate decisions submitted on 2026-11-20; no reviewer sign-off from 0.8.7 is carried forward. Independent approval of the final PR head is still required before merge. The Web npm version remains 0.3.18.

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
This candidate is not a publication authorization. The recorded sign-off reviews candidate `c67e1fce8f82c2695f750d97a9172c7c0a9c3e6b`; the record/export metadata commit must receive final PR review and fresh verification.

- `ksadk-python`: reviewed runtime source `bb33d27ba7bc28b78502939ccf4041f102d9e204`, exported from `e281013fbe4789c7b3a669a5ef85af6b09ee7fa8`. Candidate `c67e1fce8f82c2695f750d97a9172c7c0a9c3e6b` was signed off with the same runtime. PR #124 must review its final record/export metadata commit; runtime changes after this anchor require a new recorded anchor and fresh verification.
- `ksadk-web`: published `@kingsoftcloud/ksadk-web@0.3.18`, source `3a1dde1f0b86ebd547d34a4787cc5fa57ea36319`, bundled with Python runtime review anchor `bb33d27ba7bc28b78502939ccf4041f102d9e204`; no npm publication is requested.

## Recorded Evidence for Approval

Branch protection and publish environment must be verified against `.github/BRANCH_PROTECTION.md` before merge/publication.

Verification is attached to [PR #124](https://github.com/kingsoftcloud/ksadk-python/pull/124). The recorded candidate sign-off accepts the following evidence; the final PR head still requires independent review:

- Matching SDK/runtime sources in the clean export and both candidate branches; no direct master/main merge.
- Complete `make public-preflight`, package provenance, cold installs, alias distribution and documentation build for the final public candidate.
- PostgreSQL 9.6–16 matrix and legacy Memory retention, including real 0.8.7-to-candidate SQLite/PostgreSQL checks.
- Real preproduction verification of the same three Agents, including the ordinary Agent with Kernel disabled. OAuth and A2A must use separate test phases.
- Memory identity tests cover the 68-character trusted user key, preserve internal/session identities and existing valid cloud IDs, and use one encoding for save/query/status/update/forget.

Earlier 0.8.7 or earlier 0.8.8 candidate decisions are not carried forward. In the [actual sign-off comment](https://github.com/kingsoftcloud/ksadk-python/pull/124#issuecomment-7030218229), `@lijiafu618-alt` explicitly accepted all three roles, the verification scope and the known limitations for candidate `c67e1fce8f82c2695f750d97a9172c7c0a9c3e6b` on 2026-11-20. The comment does not authorize immediate publication. Its role sign-offs do not replace the separate GitHub review required from a non-author for the final PR head.

## Known Limitations and Security Disposition

The Security reviewer explicitly accepted retaining the existing Web 0.3.18 dependency findings and unsupported A2UI `Card.theme` behavior for this candidate in the linked sign-off comment. They are not claimed as fixed, and this source synchronization does not validate every cloud deployment. SDK release evidence schema v3 excludes the retired Studio browser check; browser acceptance belongs to the separate Studio repository.

The production-dependency audit of the unchanged Studio/Web lock on 2026-11-20 reported 17 findings: 9 high, 3 moderate and 5 low. These are dependency findings, not proof that every advisory is reachable in a prebuilt Hosted UI. The recorded decision is to accept the existing findings for the 0.8.8 candidate rather than require a Web change in this PR; it is the reviewer's decision in the linked comment, not an automated security acceptance.

This release limits the additional identity repair to outbound Memory `AgentUserId`. It does not shorten trusted identity keys or change local Memory namespaces, business SessionIds, KnowledgeBaseIds, database schemas, Server/Gateway or the Web version. Custom cloud Memory SessionIds must continue to satisfy the service's existing limit. No-Kernel OAuth MCP remains unsupported by its existing contract.

No dedicated preproduction Memory collection is configured for this validation. The requester and the recorded sign-off accepted protocol-boundary tests for `AgentUserId`, actual 0.8.7 legacy persistence checks and the three preproduction Agents' feature verification in place of a live cloud Memory CRUD test. Do not report cloud Memory CRUD as passed. The sign-off also acknowledges that no-Kernel OAuth MCP retains its Kernel requirement.

The release owner must use the recorded runtime anchor `bb33d27ba7bc28b78502939ccf4041f102d9e204` as `approved_source_commit` for the trusted publish workflow, or use the repository variable configured to that value. Do not reuse an earlier release's value. A future Release/tag and package build must originate from the final reviewed GitHub main commit. This record and a passing approval gate do not trigger or authorize immediate publication; the current task remains limited to candidate preparation and final PR review.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | @lijiafu618-alt | Approved | 2026-11-20 |
| Security reviewer | @lijiafu618-alt | Approved | 2026-11-20 |
| Release owner | @lijiafu618-alt | Approved | 2026-11-20 |
