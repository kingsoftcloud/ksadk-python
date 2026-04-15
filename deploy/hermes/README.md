# Hermes Runtime Assets

This directory contains the shared Hermes runtime assets used by KsADK:

- `Dockerfile`
- `entrypoint.sh`
- `runtime/app.py`
- `agentengine.yaml.template`
- `.env.example`

## Public Image Workflow

For this phase, `agentengine hermes deploy` does not build locally. It deploys a published runtime image through `CreateAgentProduct` / `UpdateAgent`.

The default public image is:

```text
hub.kce.ksyun.com/agentengine-public/hermes-agent:v2026.4.13-ks8
```

When we need to refresh that shared image, do it from the `ksadk-python` repo root:

```bash
make hermes-build
make hermes-push HERMES_TAG=v2026.4.13-ks8
make hermes-size
```

The Dockerfile installs Hermes from the official GitHub release ref by default:

```bash
make hermes-build HERMES_AGENT_REF=v2026.4.13
```

During build, the Dockerfile also compiles the Hermes dashboard frontend and copies the resulting `web_dist` assets into the installed `hermes_cli` package so `/` can serve the UI.
The runtime image also preinstalls `chromium`, `agent-browser`, the CN-first search baseline skills, and the bundled `kdocs` skill so browser/search capability works without additional image customization.

The `Makefile` also tags and pushes the matching VPC registry image:

```text
hub-vpc-cn-beijing-6.kce.ksyun.com/agentengine-public/hermes-agent:<tag>
```

## Runtime Contract

The published image serves all Hermes runtime surfaces from one port:

- `/` -> Hermes dashboard WebUI
- `/chat` -> handled by the platform router / hosted chat
- `/v1/*` -> Hermes OpenAI-compatible API
- `/_ksadk/terminal/ws` -> native remote TUI, restricted `hermes exec`, and `hermes pairing`
- `/health` -> wrapper health probe that checks both API and dashboard upstreams

The wrapper must proxy `/v1/*` with a real streaming response for SSE. Do not read `upstream.content` into memory before returning, or hosted `/chat` will degrade into burst output after a long stall.

## Generated Project Relationship

`agentengine init -f hermes` copies these assets into the generated project as a container-first reference implementation.

By default that generated project still uses the shared public image:

```bash
agentengine hermes deploy
```

If we later need a custom runtime image, we can publish it separately and override at deploy time:

```bash
agentengine hermes deploy --image <custom-image>
```

When the deploy input contains the public KSPMAS gateway `http://kspmas.ksyun.com/v1`, the Hermes CLI rewrites it to `http://kspmas-internal.sdns.ksyun.com/v1` before injecting it into the cloud runtime. This keeps preprod / online pods on the reachable internal model gateway.

For `glm-5.1`, the CLI/runtime set `HERMES_CONTEXT_LENGTH=200000` and write `model.context_length` into `~/.hermes/config.yaml` so Hermes does not fall back to the 128K endpoint metadata. The fallback model can be controlled with `HERMES_FALLBACK_MODEL`; KSPMAS / `glm-5.1` deployments default it to `kimi-k2.5`.
