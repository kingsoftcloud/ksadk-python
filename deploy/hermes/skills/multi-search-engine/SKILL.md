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

In mainland China deployments, prefer CN-accessible engines first: Bing CN,
Sogou, and 360. Baidu is a fallback only, because it often presents CAPTCHA or
anti-bot interstitials during automated retrieval. Use Google or DuckDuckGo
only when the topic clearly needs international sources and the network is
reachable.

## How to search

Use `curl` to retrieve search results from public engines. No API key is
needed. If this runtime has explicitly enabled the built-in `fetch` tool, you
may use it as an optional shortcut, but do not assume it is available by
default.

```bash
tmp="/tmp/openclaw-search.html"
curl -sS -L "https://cn.bing.com/search?q=QUERY&ensearch=0" -o "$tmp" && head -200 "$tmp"
curl -sS -L "https://www.sogou.com/web?query=QUERY" -o "$tmp" && head -200 "$tmp"
```

Avoid `curl ... | head -200`: it often triggers `curl: (23) Failure writing output to destination`
because the downstream command closes the pipe early.

If a page requires JavaScript rendering, cookies, or interactive navigation,
prefer the built-in `browser` tool first in this runtime. Switch to
`agent-browser` only when you need more deterministic CLI-style automation or
session isolation.

```bash
agent-browser open "https://cn.bing.com/search?q=QUERY&ensearch=0"
agent-browser snapshot -i --json
```

## When to use

- User asks for latest news, hot topics, or trend snapshots
- One engine gives sparse/noisy results and needs fallback
- You need CN + global viewpoints for the same query

## Engine URLs

Replace `{q}` with URL-encoded query text.

- Bing CN: `https://cn.bing.com/search?q={q}&ensearch=0`
- Bing Global: `https://cn.bing.com/search?q={q}&ensearch=1`
- Sogou: `https://www.sogou.com/web?query={q}`
- 360: `https://www.so.com/s?q={q}`
- Baidu: `https://www.baidu.com/s?wd={q}`
- Google: `https://www.google.com/search?q={q}`
- DuckDuckGo: `https://duckduckgo.com/?q={q}`

## Recommended workflow

1. In CN runtimes, start with Bing CN / Sogou / 360 before trying Baidu or global engines.
2. Fetch search result pages and extract top links.
3. Read top results with `curl` first; only switch to `agent-browser` when the site blocks plain HTTP retrieval or needs dynamic rendering.
4. If facts conflict, run targeted follow-up queries with dates or `site:` filters.
5. Return answer with source links and date context.

## Query tips

- Time-sensitive: add year/month or `site:` filter
- Official docs first: `site:docs.xxx.com {topic}`
- News check: combine media + official release sources
