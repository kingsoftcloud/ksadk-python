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

## How to search

Use the built-in fetch tool or curl to retrieve search results from public engines.
No API key is needed.

```bash
curl -sS "https://www.baidu.com/s?wd=QUERY" | head -200
curl -sS "https://cn.bing.com/search?q=QUERY" | head -200
```

Or use the browser tool for richer results:

```text
browser navigate https://www.baidu.com/s?wd=QUERY
```

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

## Recommended workflow

1. Pick 1-2 engines best suited for the query language and topic.
2. Fetch search result pages and extract top links.
3. Read top results with fetch or browser and keep authoritative links.
4. If facts conflict, run targeted follow-up queries with dates or `site:` filters.
5. Return answer with source links and date context.

## Query tips

- Time-sensitive: add year/month or `site:` filter
- Official docs first: `site:docs.xxx.com {topic}`
- News check: combine media + official release sources
