# KsADK documentation site

This directory contains the public KsADK documentation built with Fumadocs and Next.js static export.

## Local development

Requirements: Node.js 22 and pnpm 9.

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
