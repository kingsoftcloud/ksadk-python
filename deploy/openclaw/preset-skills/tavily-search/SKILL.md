---
name: tavily-search
description: >
  Tavily search skill for high-quality web retrieval and quick fact grounding.
  Requires Tavily API key. Preferred for deep web search and source-backed answers.
---

# Tavily Search

Use Tavily API when the user asks for web research that needs better precision
and citations.

If Tavily is not configured, fall back to:

```bash
web-safe search "query" --limit 5
web-safe read "https://example.com/page"
```

## Required key

- `tavily_api_key` in `${OPENCLAW_STATE_DIR:-~/.openclaw}/.env`, or
- `TAVILY_API_KEY` / `OPENCLAW_TAVILY_API_KEY` environment variable

Bootstrap will sync `OPENCLAW_TAVILY_API_KEY`/`TAVILY_API_KEY` to `.env`
as `tavily_api_key`.

## API endpoint

- `POST https://api.tavily.com/search`

## Example request

```bash
curl -sS https://api.tavily.com/search \
  -H 'Content-Type: application/json' \
  -d '{
    "api_key": "'"${TAVILY_API_KEY}"'",
    "query": "OpenClaw 2026.3.12 release notes",
    "search_depth": "advanced",
    "max_results": 5,
    "include_answer": true
  }'
```

## Recommended workflow

1. Build a focused query with product/version/date.
2. Request `max_results` 5-8 and inspect returned sources.
3. Cross-check critical facts with primary docs or release pages.
4. Answer with links and concrete dates.

## Safety rules

- Do not expose API keys in chat output.
- Prefer official docs/repos for technical claims.
