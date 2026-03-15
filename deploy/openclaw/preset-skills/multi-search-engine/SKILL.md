---
name: multi-search-engine
description: >
  Multi-engine web search helper. Use when users ask for web/news search,
  cross-checking sources, or better recall with no paid search API.
  Supports CN + global engines by generating precise query URLs.
---

# Multi Search Engine

Use this skill to perform broad web search across multiple engines and then
cross-check results before answering.

## Default tool path

Prefer the built-in no-key commands first:

```bash
web-safe search "query" --limit 5
web-safe read "https://example.com/page"
```

`web-safe search` gives a fast public-web result set without paid API keys.
`web-safe read` fetches a readable markdown version of a public page.

## When to use

- User asks for latest news, hot topics, or trend snapshots
- One engine gives sparse/noisy results and needs fallback
- You need CN + global viewpoints for the same query

## Engine URLs

Replace `{q}` with URL-encoded query text.

- Baidu: `https://www.baidu.com/s?wd={q}`
- Bing CN: `https://cn.bing.com/search?q={q}&ensearch=0`
- Bing Global: `https://cn.bing.com/search?q={q}&ensearch=1`
- Sogou: `https://www.sogou.com/web?query={q}`
- 360: `https://www.so.com/s?q={q}`
- Google: `https://www.google.com/search?q={q}`
- DuckDuckGo: `https://duckduckgo.com/?q={q}`
- Brave: `https://search.brave.com/search?q={q}`
- Kagi: `https://kagi.com/search?q={q}`

## Recommended workflow

1. Start with `web-safe search "query" --limit 5`.
2. If recall is weak, try a second query with a site filter or engine-specific wording.
3. Open top results with `web-safe read "URL"` and keep authoritative links.
4. If facts conflict, run targeted follow-up queries with dates or `site:` filters.
5. Return answer with source links and date context.

## Query tips

- Time-sensitive: add year/month or `site:` filter
- Official docs first: `site:docs.xxx.com {topic}`
- News check: combine media + official release sources
