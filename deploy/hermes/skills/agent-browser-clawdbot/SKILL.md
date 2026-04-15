---
name: agent-browser-clawdbot
description: Headless browser automation CLI optimized for AI agents with accessibility tree snapshots and ref-based element selection. Use when the user needs deterministic browser automation for multi-step web workflows, form filling, screenshots, scraping, or session-isolated site interactions.
metadata: {"clawdbot":{"emoji":"🌐","requires":{"commands":["agent-browser"]},"homepage":"https://github.com/vercel-labs/agent-browser"}}
---

# Agent Browser Clawdbot

Fast browser automation using accessibility tree snapshots with refs for deterministic element selection.

## Why Use This Alongside Built-in Browser

Use `agent-browser` when:

- Automating multi-step workflows
- Need deterministic element selection
- Performance is critical
- Working with complex SPAs
- Need session isolation
- The user explicitly asks for `agent-browser`

Use the built-in browser tool first when:

- You need ordinary search, JS rendering, reading, or light interaction
- You want the default native OpenClaw browser path in this runtime
- You need built-in browser screenshots/PDFs specifically

## Runtime Notes

- This image preinstalls the `agent-browser` CLI.
- Built-in `browser` is enabled by default in this image.
- `agent-browser` can reuse a system Chrome or Chromium automatically.
- This runtime already prefers the system Chromium path, so you usually do not need to download a browser again.
- If you need to force a specific browser binary, set `AGENT_BROWSER_EXECUTABLE_PATH=/path/to/browser`.
- In remote OpenClaw gateway deployments, use `agent-browser` when you specifically need its CLI workflow or more deterministic session control. Normal search/retrieval can stay on the built-in `browser` tool.

## Search Workflow In This Image

- For open-ended web search in CN deployments, use the built-in `browser` first.
- Start with `Bing CN`, `Bing News`, or a target site's own search page. If Bing is sparse, fall back to `Sogou` or `360`.
- Open the result page, snapshot it, and then open the actual source links. Switch to `agent-browser` only when you need more controllable CLI-style automation.
- Use `web-safe search` / `web-safe read` only when a cheap read-only fallback is enough and you do not need JavaScript rendering.

```bash
agent-browser open "https://cn.bing.com/search?q=AI+%E6%96%B0%E9%97%BB&ensearch=0"
agent-browser snapshot -i --json

agent-browser open "https://www.bing.com/news/search?q=AI&mkt=zh-CN"
agent-browser snapshot -i --json
```

## Core Workflow

```bash
# 1. Navigate and snapshot
agent-browser open "https://cn.bing.com/search?q=QUERY&ensearch=0"
agent-browser snapshot -i --json

# 2. Parse refs from JSON, then interact
agent-browser click @e2
agent-browser fill @e3 "text"

# 3. Re-snapshot after page changes
agent-browser snapshot -i --json
```

## Key Commands

### Navigation

```bash
agent-browser open <url>
agent-browser back | forward | reload | close
```

### Snapshot

```bash
agent-browser snapshot -i --json
agent-browser snapshot -i -c -d 5 --json
agent-browser snapshot -s "#main" -i
```

### Interactions

```bash
agent-browser click @e2
agent-browser fill @e3 "text"
agent-browser type @e3 "text"
agent-browser hover @e4
agent-browser check @e5
agent-browser uncheck @e5
agent-browser select @e6 "value"
agent-browser press "Enter"
agent-browser scroll down 500
agent-browser drag @e7 @e8
```

### Get Information

```bash
agent-browser get text @e1 --json
agent-browser get html @e2 --json
agent-browser get value @e3 --json
agent-browser get attr @e4 "href" --json
agent-browser get title --json
agent-browser get url --json
agent-browser get count ".item" --json
```

### Wait

```bash
agent-browser wait @e2
agent-browser wait 1000
agent-browser wait --text "Welcome"
agent-browser wait --url "**/dashboard"
agent-browser wait --load networkidle
agent-browser wait --fn "window.ready === true"
```

### Sessions

```bash
agent-browser --session admin open site.com
agent-browser --session user open site.com
agent-browser session list
```

### State Persistence

```bash
agent-browser state save auth.json
agent-browser state load auth.json
```

### Screenshots And PDFs

```bash
agent-browser screenshot page.png
agent-browser screenshot --full page.png
agent-browser pdf page.pdf
```

## Best Practices

1. Prefer `snapshot -i --json` for AI-readable output.
2. Re-snapshot after any DOM-changing action.
3. Use explicit waits for SPA transitions.
4. Save auth state if login is expensive.
5. Use sessions to isolate parallel accounts.
6. Use `--headed` for debugging only.
7. For latest/news queries, prefer search pages or news verticals that already expose publish time and source before opening individual articles.

## Example: Search And Extract

```bash
agent-browser open https://cn.bing.com/search?q=AI+agents&ensearch=0
agent-browser snapshot -i --json
agent-browser fill @e1 "AI agents"
agent-browser press Enter
agent-browser wait --load networkidle
agent-browser snapshot -i --json
agent-browser get text @e3 --json
agent-browser get attr @e4 "href" --json
```

## Example: Multi-Session Testing

```bash
agent-browser --session admin open app.com
agent-browser --session admin state load admin-auth.json
agent-browser --session admin snapshot -i --json

agent-browser --session user open app.com
agent-browser --session user state load user-auth.json
agent-browser --session user snapshot -i --json
```

## Installation

This image already bundles `agent-browser`, so skip re-installation here.
If you are in another environment and must install it manually, prefer a domestic npm mirror:

```bash
NPM_CONFIG_REGISTRY=https://registry.npmmirror.com npm install -g agent-browser
```
