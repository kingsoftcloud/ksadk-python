# Security Policy

## Supported Versions

Security fixes are planned for the latest public minor release of `ksadk`.
Pre-release builds, local development branches, and internal deployment assets
are not covered by the public security support policy unless a maintainer says
otherwise in the release notes.

## Reporting a Vulnerability

Please do not report security vulnerabilities in public issues.

Send reports to `security@kingsoft.com` with:

- Affected version, commit, or package artifact.
- Reproduction steps and expected impact.
- Any proof-of-concept code, logs, or screenshots that are safe to share.
- Whether the report may involve credentials, private endpoints, or customer
  data.

Maintainers will acknowledge receipt, triage severity, and coordinate a fix or
disclosure timeline. Do not publish exploit details until maintainers confirm
that affected users have a reasonable upgrade path.

## Scope

In scope:

- `ksadk` Python SDK and CLI.
- Local `agentengine web` server behavior.
- Public package artifacts, GitHub Pages docs, and public examples.

Out of scope for the public repo:

- Internal AgentEngine control-plane services.
- Private Kubernetes, Helm, gateway, registry, or object-storage deployment
  details.
- Credentials, tokens, or customer data discovered outside the public
  repository.
