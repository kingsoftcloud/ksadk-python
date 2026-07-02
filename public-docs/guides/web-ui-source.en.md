# Web UI Repository

The KsADK Web UI source lives in the independent `kingsoftcloud/ksadk-web`
repository and is published as the npm package `@kingsoftcloud/ksadk-web`.
`ksadk-python` no longer builds the UI source locally; instead it synchronizes
the `dist-ksadk` output from the npm package into `ksadk/server/static` via
`make sync-ksadk-web-static`, for the local `agentengine web` server.

!!! info "0.6.7 policy"
    Since 0.6.5 `ksadk-python` removed the local `npm ci` / `npm run build:ksadk`
    build chain and no longer pulls GitHub latest release at wheel build time.
    All static assets come from the `@kingsoftcloud/ksadk-web` npm package.

## Repository Roles

| Repository / Package | Role |
| --- | --- |
| `kingsoftcloud/ksadk-web` | Editable React/Vite source, tests, Pages demo, npm publishing |
| `@kingsoftcloud/ksadk-web` (npm) | Distribution channel for `dist-ksadk` / `dist-hosted` build outputs |
| `ksadk-python` | Python SDK, CLI, runner, local server, embedded `ksadk/server/static` |
| hosted UI | Production deployment shell, gateway, image and environment injection; also consumes the npm package |

The `ksadk-python` public clean export does not contain `ksadk/server/web-ui`
source, nor `node_modules`, `dist/`, or `dist-hosted/` intermediate UI artifacts.
The editable UI source is maintained only in the `kingsoftcloud/ksadk-web`
repository.

## Sync Rules

`make sync-ksadk-web-static` synchronizes the `dist-ksadk` directory of the
`@kingsoftcloud/ksadk-web` npm package into `ksadk/server/static`:

```bash
# Default: pull npm latest
make sync-ksadk-web-static

# Pin a concrete version for a release candidate (0.6.7 maps to 0.2.15)
make sync-ksadk-web-static KSADK_WEB_VERSION=0.2.15
```

!!! warning "Do not pull GitHub latest release at wheel build time"
    `latest` is not reproducible, introduces network and supply-chain risk, and
    cannot guarantee wheel contents match the release record. Release candidates
    must pin `KSADK_WEB_VERSION` to a concrete version. To fall back in a
    network-restricted environment, use `KSADK_WEB_RELEASE_URL` to point at an
    audited tarball.

### make build / build-wheel auto-sync

`make build` and `make build-wheel` run `sync-ksadk-web-static` before building
the wheel, so the embedded static assets always come from the controlled npm
version:

```bash hl_lines="2"
make build
# 1. sync-ksadk-web-static -> ksadk/server/static
# 2. python -m build  /  uv build
```

If the static assets are already synced and you want to skip sync, use
`make build-only` (it verifies that `ksadk/server/static/index.html` exists).

### Sync Variables

| Variable | Default | Description |
| --- | --- | --- |
| `KSADK_WEB_VERSION` | `latest` | npm package version; pin to a concrete version for release candidates, e.g. `0.2.15` |
| `KSADK_WEB_PACKAGE` | `@kingsoftcloud/ksadk-web` | npm package name |
| `KSADK_WEB_TARBALL_NAME` | `kingsoftcloud-ksadk-web-<version>.tgz` | Tarball filename, derived from the version |
| `KSADK_WEB_RELEASE_URL` | empty | Explicit tarball URL fallback; takes precedence over `npm pack` |
| `KSADK_WEB_CACHE_DIR` | `.cache/ksadk-web` | Local cache directory for extraction |
| `KSADK_WEB_REGISTRY` | `https://registry.npmjs.org` | npm registry; used to resolve the tarball URL only when no `npm` CLI is available |

Sync precedence: `KSADK_WEB_RELEASE_URL` explicit tarball > `npm pack` > registry
resolution. Regardless of the path taken, the `dist-ksadk` directory is verified
to exist before it is copied over `ksadk/server/static`.

## Release Record

Each `ksadk-python` release note should record:

- The `ksadk-python` version (e.g. `0.6.7`).
- The `KSADK_WEB_VERSION` / npm package version (e.g. `0.6.7` maps to `@kingsoftcloud/ksadk-web@0.2.15`).
- The sync command (e.g. `make sync-ksadk-web-static KSADK_WEB_VERSION=0.2.15`).
- The wheel / sdist audit result (`make public-build-check` / `twine check dist/*`).

!!! example "0.6.7 release record example"
    - `ksadk-python`: `0.6.7`
    - npm package: `@kingsoftcloud/ksadk-web@0.2.15`
    - sync: `make sync-ksadk-web-static KSADK_WEB_VERSION=0.2.15`
    - audit: `make public-build-check` passed, `twine check dist/*` passed
