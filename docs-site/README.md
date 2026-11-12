# KsADK documentation site

This directory contains the public KsADK documentation built with Fumadocs and Next.js static export.

## Local development

Requirements: Node.js 22 and pnpm 10.33.2.

```bash
pnpm install --frozen-lockfile
pnpm dev
```

Build the same static site used by repository checks:

```bash
NEXT_PUBLIC_BASE_PATH=/ksadk-python pnpm build:static
```

From the repository root, `make docs-site-build` installs dependencies and runs the static build.

## Content and i18n

Documentation lives under `content/docs/`.

| Language | File convention | Route prefix |
| --- | --- | --- |
| Chinese | `page.mdx` | `/cn/` |
| English | `page.en.mdx` | `/en/` |

Every public page must have both files with aligned headings, tables, examples, links, and asset coverage. Navigation is defined by `meta.json` and `meta.en.json` in each content directory.

## Architecture assets

Source SVG files and their PNG fallbacks live in `public/assets/`. Localized diagrams use the same base name with an `.en` suffix for English.

The main runtime architecture assets are:

- `ksadk-runtime-architecture.svg` and `.png`
- `ksadk-runtime-architecture.en.svg` and `.en.png`

Keep SVG text inside its boxes, preserve readable connectors, remove branch-specific metadata, and regenerate both PNG files whenever an SVG changes.

## Verification

Before committing documentation changes:

```bash
make docs-site-build
git diff --check
```

Architecture changes should also validate SVG syntax and visually inspect both language variants.


## Public documentation ownership

Maintain public content in this repository through reviewed GitHub PRs. `README.md` and `README.en.md`
are short entry points; `content/docs/**` contains the bilingual integration manuals and references.
GitHub Pages renders the same source. API/package examples must match the reviewed SDK revision;
mark candidate-only features explicitly until release. DeepWiki is a supplementary code explanation,
not the authoritative configuration or release documentation. Avoid maintaining a second independent
Wiki or documentation copy that can drift from the repository.

The current Pages deployment is part of `publish-pypi.yml`. Do not dispatch that workflow merely to
preview documentation: it also contains package publishing jobs. Validate and review candidate docs
locally and in PR CI; deploy Pages only through the approved trusted publishing flow. A source PR
update does not mean the live Pages site has changed.
