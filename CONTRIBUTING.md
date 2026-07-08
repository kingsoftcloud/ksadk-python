# Contributing

Thanks for helping improve `ksadk`.

## Development Setup

```bash
git clone https://github.com/kingsoftcloud/ksadk-python.git
cd ksadk-python
uv sync --extra dev
```

## Local Checks

Run focused checks before sending a change:

```bash
uv run --extra dev pytest -q
make open-source-audit
make docs-site-build
make public-audit
uv build
uv run --extra dev python -m twine check dist/*
```

`docs-site-build` builds the Fumadocs GitHub Pages candidate from
`docs-site/`. `public-audit` checks that public repository candidates do not
publish `.zread/wiki`, `.zread/site`, internal deployment notes, or private
generated snapshots.

`open-source-audit` checks the current public repository candidate for files
that should not enter the open-source surface.

Do not push, publish, or create a release before maintainer review.

## Pull Requests

- Keep changes focused.
- Include tests for behavior changes.
- Update public docs when user-facing behavior changes.
- Avoid committing generated caches, local virtual environments, local zread
  output, internal deployment assets, or private configuration.
- Mention any release, packaging, or documentation impact in the PR body.

## Security

Do not put vulnerability details in a public issue. Follow `SECURITY.md`.
