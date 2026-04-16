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
hub.kce.ksyun.com/agentengine-public/hermes-agent:v2026.4.13-ks16
```

When we need to refresh that shared image, do it from the `ksadk-python` repo root:

```bash
make hermes-build
make hermes-push HERMES_TAG=v2026.4.13-ks16
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
- `/_ksadk/terminal/ws` -> native remote TUI, remote `hermes gateway setup`, restricted `hermes exec`, and `hermes pairing`
- `/health` -> wrapper health probe that checks both API and dashboard upstreams

The wrapper must proxy `/v1/*` with a real streaming response for SSE. Do not read `upstream.content` into memory before returning, or hosted `/chat` will degrade into burst output after a long stall.

## Hosted Gateway Behavior

AgentEngine-hosted Hermes treats the messaging gateway as a container-managed
runtime process, not a desktop daemon:

- `entrypoint.sh` starts `hermes gateway run --replace` automatically
- the container supervises and restarts the gateway locally when it exits
- if local restart attempts are exhausted, the entrypoint terminates the main
  process so Kubernetes can recreate the pod
- hosted `agentengine hermes connect` configures Feishu / Weixin inside the pod
  and skips `systemd` / `launchd` installation flows

In other words: hosted Hermes does not rely on `systemd`, `launchd`, `loginctl`,
or sudo to keep the gateway alive.

## Persistent Directory Layout

The runtime now assumes a single persistent directory rooted at `~/.hermes`.
The entrypoint explicitly pins the mutable runtime state under that one tree:

```bash
HOME=/home/node
HERMES_HOME=/home/node/.hermes
HERMES_WORKDIR=/home/node/.hermes/workspace
AGENT_BROWSER_HOME=/usr/local/lib/node_modules/agent-browser
AGENT_BROWSER_STATE_DIR=/home/node/.hermes/browser
AGENT_BROWSER_SOCKET_DIR=/home/node/.hermes/browser/run
AGENT_BROWSER_SESSION_DIR=/home/node/.hermes/browser/sessions
MCPORTER_HOME=/home/node/.hermes/mcporter
```

If you deploy this image with a PVC or hostPath mount and only get one persistent path, mount that storage at `/home/node/.hermes`.
That keeps Hermes config, sessions, workspace state, `mcporter` config, and browser sockets/sessions in one place and avoids split-brain behavior after restarts.
`AGENT_BROWSER_HOME` points at the installed package root so the CLI can find its bundled daemon assets; browser runtime state still stays under `~/.hermes/browser`.

The entrypoint also precreates:

- `/home/node/.hermes/run`
- `/home/node/.hermes/sessions`
- `/home/node/.hermes/browser/run`
- `/home/node/.hermes/browser/sessions`

That layout is the recommended default for both local container runs and cluster deployments, and it matches the OpenClaw-style “single state directory” model more closely than mounting a separate workspace root.
The bundled kdocs skill also assumes this remote-pod-friendly layout: `mcporter` is preinstalled in the image, and token acquisition prints a login URL by default instead of trying to auto-open a browser inside the pod.
Hosted runtime also defaults `TERM=xterm-256color` so interactive Hermes setup
menus keep curses / arrow-key navigation instead of degrading to numeric input.

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
