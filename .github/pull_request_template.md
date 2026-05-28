## Summary

-

## Validation

- [ ] `uv run --extra dev pytest -q`
- [ ] `make open-source-audit`
- [ ] `make public-docs-audit`
- [ ] `uv build`
- [ ] `uv run --extra dev python -m twine check dist/*`

## Public Surface

- [ ] README or public docs updated if user-facing behavior changed.
- [ ] Package metadata, extras, or release artifacts reviewed if packaging changed.
- [ ] GitHub Pages impact reviewed if docs changed.

## Security impact

- [ ] No tokens, credentials, private URLs, customer data, internal deployment notes, or `.zread/` output are introduced.
- [ ] Security-sensitive changes are described without exploit details.

## Release

- [ ] Release notes impact is documented.
- [ ] Maintainer review is required before public release or publish actions.
