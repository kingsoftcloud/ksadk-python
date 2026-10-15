# KsADK Public Release Approval Record

This record binds the reviewed source for KsADK `0.8.6` and its companion Web package. The release task author authorized preparation and publication after verification. Codex performed the recorded automated reviews and tests; this is not a claim that the author personally reviewed each diff or that an independent human security review occurred.

Source approval does not waive the maintainer pull-request approval, required status checks, or publish-environment approval required by `.github/BRANCH_PROTECTION.md`. Those remote controls must be verified before merging or publishing.

## Required Approval Decisions

| Decision | Approved value |
| --- | --- |
| License | Apache-2.0 |
| Python repository | kingsoftcloud/ksadk-python |
| Web UI repository | kingsoftcloud/ksadk-web |
| Python package version | 0.8.6 |
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

- `ksadk-python`: Clean public export of reviewed source commit `5fb0e8c82b14032c0451653aace1f654daaed7e2`; publish both `ksadk==0.8.6` and `agentengine-sdk-python==0.8.6` through the reviewed public pull request and GitHub Trusted Publishing. The final approval-record commit only records this decision.
- `ksadk-web`: `@kingsoftcloud/ksadk-web@0.3.13`, published from GitHub merge commit `b434f83a048f808fb9e4bf5c1c67e3b81f09ff6f`; source provenance `d65c31a42ee92aeb0f6b157084d7194a5d93226f`; npm integrity `sha512-cKLJxdIVpDic9ef3W9yqS/IH+LgqWbPcrbl74zLJlYx5d6kTP8Bd7mA3uvjoarg1b+gatCiLLmEgjPZ7X4KvEQ==`; Python consumer binding reviewed in source commit `5fb0e8c82b14032c0451653aace1f654daaed7e2`.

Both approved source references include the reviewed Python source commit SHA so a changed candidate cannot reuse this record.

## Recorded Evidence for Approval

- Official npm metadata and the downloadable tarball agree on version `0.3.13`, Git source, SHA-1 `ea3f4f80f525b279accfd5f412902a777247b43d`, and SHA-256 `7f3b7143397a798e1432917f3f342def4785000b7365355aec486c82483a3291`. The publish workflow completed with provenance; Python's lockfile uses the registry integrity above.
- Standards and specification reviews covered the frozen implementation, dependency closure of the public tests, source-export safety, runtime compatibility fixes, and bounded retry/cancellation behavior. The final Studio fixes preserve drafts across asynchronous workspace/session initialization and preserve the confirmed local session in the URL for refresh. Deterministic regressions cover agent ownership, failed bootstrap refresh, cleanup, deep links, empty selections, and history re-selection. Browser fixtures use the current settings page and a non-secret model catalog without weakening assertions or runtime limits.
- Earlier reviewed candidates passed the complete local `make public-preflight`: public-source audit, production frontends, documentation export, compatibility, Native/Codex, official managed DSH toolchain, five browser gates, artifact audit, clean wheel/sdist installation, and `twine check`. Subsequent candidates were blocked by browser failures and are not accepted as complete passes. The final candidate must pass the entire gate again before publication.
- Python 3.12 Native regression: 3518 passed; 39 explicitly opted-in live-model cases were skipped. Separate boundary regression passed 49 cases including real SIGKILL and the original 30-second lease. Final Studio frontend validation passed 77 Node tests and all 518 UI tests on Node 22, plus type checking and the production build. The double-window approval browser fixture explicitly opens the confirmed agent/session URL instead of a bare new-draft entry; both windows still assert the same approval revision, idempotent receipts, and a single provider resume. This fixture correction passed its complete targeted browser scenario. The affected ADK runner/database regressions passed 95 tests at each supported matrix anchor (1.34.3 and 2.5.0); CI asserts the actual version immediately before running each matrix leg.
- Final frozen-source real-model browser acceptance completed three repetitions with the built UI, actual HTTP, SQLite durability, live refresh, cold service recovery, and follow-up recall. Product/source/static digests remained unchanged during acceptance. Model credentials and private evidence remain outside the public tree.
- Two controlled browser fault scenarios passed real SIGKILL recovery for approval and child runs with unchanged 30-second leases, preserved event prefixes, and unique terminal events. Their model gateway was scripted; they are not real-model quality evidence.
- Linux sidecar shutdown regression uses real isolated process trees: zombie-only members do not consume the original five-second budget; a live child that ignores SIGTERM still consumes that budget and is terminated. Hidden, stacked, unreadable, or namespace-mismatched process views remain conservative. UI test cleanup closes the real Radix popover and awaits focus restoration before jsdom teardown; it does not suppress delayed errors or change timeouts.
- Both independently named Python distributions are built and audited before the first PyPI upload. The alias rewrites its own extras to its own distribution name, so selecting `[all]` does not also install `ksadk` over the shared import namespace. Final artifact metadata and resolver checks must confirm this binding.
- Wheel and sdist exclude the test tree. Static Web payload verification checks all 265 files against the pinned npm package. Package hashes and source provenance are recorded by the build and release-preflight reports.
- Cloud and pre-production deployment, Native multi-replica recovery, and target-service authorization are outside this local/source-and-package evidence and require their own environment acceptance.
- Required final remote check: **Branch protection and publish environment are configured** according to `.github/BRANCH_PROTECTION.md`. This source record cannot substitute for those controls or authorize bypassing a review.
- PyPI publication uses the GitHub environment and Trusted Publishing. Neither package upload credentials nor model credentials belong in repository files.

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | Release task author (conditional release authorization) | Approved | 2026-10-15 |
| Security reviewer | Codex automated security review (not independent human sign-off) | Approved | 2026-10-15 |
| Release owner | Release task author; execution delegated to Codex | Approved | 2026-10-15 |
