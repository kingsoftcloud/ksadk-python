# KsADK Public Release Approval Record

The 0.8.8 role sign-offs are recorded for the current runtime, including the component-routing update from internal master. The account holder explicitly authorized the current-source confirmation linked below; earlier candidate decisions remain historical evidence. Web remains 0.3.18. Final independent PR approval, merge, release verification and the existing PyPI environment approval are still required; recording these decisions does not trigger publication.

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

- `ksadk-python`: runtime source `c6f9071bcfe1e2d75ac6986f99ab0373317e2f62`, incorporating internal master `3f55fcbdd84bd2e5645914ed8ec848c56fc9ada4`; the candidate adds environment registration, public regression coverage and documentation without changing the upstream component-routing implementation. Final export metadata and verification remain subject to PR #124 review.
- `ksadk-web`: published `@kingsoftcloud/ksadk-web@0.3.18`, source `3a1dde1f0b86ebd547d34a4787cc5fa57ea36319`, bundled with Python runtime source `c6f9071bcfe1e2d75ac6986f99ab0373317e2f62`; no npm publication is requested.

## Verification and Review Scope

Branch protection and publish environment must be verified against `.github/BRANCH_PROTECTION.md` before merge or publication.

All public changes remain in [PR #124](https://github.com/kingsoftcloud/ksadk-python/pull/124). Review the final head, clean-export parity, complete `make public-preflight`, package provenance, cold installs, documentation build and current CI. Reuse the same three preproduction Agents for chat, AG-UI/session history, A2UI, separate A2A and OAuth MCP phases, API key MCP, Tunnel, the RunAgent preference and an ordinary Agent without Kernel. Do not carry forward results from an older wheel as a new deployment test.

The added component route is opt-in through `KSADK_COMPONENT_ENV=pre`; Knowledge/KSmem inject `X-KSC-CUSTOM-SOURCE: pre` before signing, and resource Workers receive a parent snapshot. This does not select the environment of other services. Business IDs, existing local Memory namespaces, database schemas and the outbound Memory user-ID repair are retained.

## Known Limitations and Disposition

- Generated mem0 configuration now contains `extraHeaders`. The checked legacy `openclaw-mem0` 1.0.6 schema rejects it even without the pre route. The requester explicitly excluded legacy-plugin compatibility from this task and asked to validate ordinary Agents; compatible plugin consumption is a requirement, not a passed legacy OpenClaw test.
- The mem0-specific source resolver does not yet apply the core SDK's printable-ASCII validation. Invalid configured header values are outside this validation; do not claim that parity is fixed.
- No dedicated preproduction Memory collection is available. Live cloud Memory CRUD was explicitly waived; retain protocol-boundary and legacy persistence checks without describing them as cloud CRUD.
- OAuth MCP still requires Kernel. Ordinary no-Kernel Agents must retain chat, API key tools and persistent sessions without enabling Kernel to hide this requirement.
- Existing Web 0.3.18 findings (9 high, 3 moderate, 5 low) and unsupported A2UI `Card.theme` remain known limitations. Studio browser acceptance belongs to its separate repository.

## Current Candidate Sign-Off

On 2026-11-20, @lijiafu618-alt explicitly authorized submitting the [current-source confirmation](https://github.com/kingsoftcloud/ksadk-python/pull/124#issuecomment-7037274696) through the account and confirmed all three roles below. It covers public candidate `dccb4c63c0aec405dbab5671138646c127b21172`, runtime source `c6f9071bcfe1e2d75ac6986f99ab0373317e2f62`, its recorded verification and the known limitations above. The confirmation explicitly identifies the delegated submission; it is not represented as an independent code review.

Subsequent sign-off archival and clean-export metadata updates do not expand this runtime scope. The same PR must receive an independent final-head approval from a non-author with Write access after its checks pass. A runtime change requires renewed verification and an actual decision for that changed source.

## Previous Candidate Sign-Off

The [actual earlier comment](https://github.com/kingsoftcloud/ksadk-python/pull/124#issuecomment-7030218229) recorded @lijiafu618-alt's three role decisions for `c67e1fce8f82c2695f750d97a9172c7c0a9c3e6b` on 2026-11-20. Those historical decisions are preserved as evidence and are not asserted to approve the new routing source or its added limitations.

For a later authorized publication, `approved_source_commit` must match the runtime source above; the repository variable has been prepared with that value. Use the final independently reviewed GitHub main commit for the v0.8.8 tag/Release and Trusted Publishing, and keep the PyPI environment approval. This sign-off archival task does not authorize automatic publication, production operations, master deletion or a new PR.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | @lijiafu618-alt | Approved | 2026-11-20 |
| Security reviewer | @lijiafu618-alt | Approved | 2026-11-20 |
| Release owner | @lijiafu618-alt | Approved | 2026-11-20 |
