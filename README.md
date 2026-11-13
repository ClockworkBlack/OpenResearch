# OpenResearch

A web research tool built for AI agents: search, read, crawl, extract, and one-call research.
Pure Python standard library, no API keys, no account, nothing to pay for.

It runs as an **MCP server** (so Claude and other agents can call it as a connector), a
**command line tool**, and a **Python library**.

```
python -m openresearch research "how do heat pumps perform below freezing" -q "heat pump cold climate COP" -q "heat pump efficiency -15C field study"
python -m openresearch fetch https://example.com/long-report --query "what changed in 2026"
python -m openresearch mcp            # MCP server over stdio
```

## Why this instead of a hosted scraper

| | OpenResearch | Hosted scrape APIs |
| --- | --- | --- |
| Cost / keys | Free, no key, no account | Paid credits, API key |
| Where it runs | Your machine or your server; nothing is sent to a third party | Vendor's cloud |
| Install | `git clone`; zero required dependencies | SDK + key |
| Tokens returned | Only the passages that match your question (`query`), with a budget and paging | Whole page |
| Research in one call | `research`: multi-query search, parallel reads, ranked passages under numbered, dated sources | Separate search and scrape calls, or a paid agent |
| Search | Several engines fused, duplicates merged, per-engine failures reported | One index |
| Dead or blocked pages | Says why (browser check, rate limit, login), tries the Internet Archive once, and says so | Varies |
| Structured data | Deterministic: CSS selectors, tables as records, JSON-LD, embedded app state | LLM extraction (flexible, but can invent values) |
| Caching | Local SQLite with ETag revalidation | Vendor-side |

Where hosted services are still stronger: they run large proxy pools and anti-bot tooling, so they
get through to sites that block ordinary clients. OpenResearch identifies itself honestly, obeys
`robots.txt`, and does not attempt to defeat bot protection or CAPTCHAs. LLM-driven "extract
anything by description" is also not built in; the calling model does that from the Markdown.

## The six tools

| Tool | What it does |
| --- | --- |
| `research` | Runs your queries across engines, reads the best pages in parallel, returns the most relevant passages grouped under numbered sources (title, domain, date, URL), then lists gaps: question terms nothing covered, pages that failed (with why, and their search snippet), and unread leads. A page that cannot be read gives its slot to the next result. |
| `search` | Multi-engine search with reciprocal-rank fusion. Verticals: `web`, `news` (Bing News feed, DuckDuckGo, Hacker News), `papers` (OpenAlex, arXiv, Crossref), `code` (GitHub, Stack Overflow), `discussions` (Hacker News). Filters: `site`, `include_domains`, `exclude_domains`, `time`. |
| `fetch` | One or many URLs to clean Markdown: HTML, PDF, JSON, RSS/Atom, sitemaps, plain text. Pass `query` to get only the relevant passages. `offset` pages through long documents; `render` loads the page in a headless browser first. |
| `crawl` | Crawls a site within its domain. With `query` it is best-first: the links most related to your question are followed first and each page is trimmed to what matters. Glob `include` / `exclude`, depth and page limits, optional sitemap seeding. |
| `map` | Lists a site's URLs from sitemaps and the start page without reading them; `search` orders them by relevance. |
| `extract` | Structured JSON: your CSS selectors (`css::text`, `css::attr(href)`, nested `fields`), tables as rows and records, JSON-LD, metadata, links, and `__NEXT_DATA__`-style embedded state. |

## Install

Nothing is required beyond Python 3.10+.

```
git clone <this repository URL> OpenResearch
cd OpenResearch
python -m openresearch doctor        # live-checks every engine and feature from your machine
```

Optional extras, each picked up automatically when present:

| Package | Adds |
| --- | --- |
| `ddgs` | More search backends behind the `ddg` engine (recommended on servers and CI, where a single engine is more likely to rate-limit you) |
| `pypdf` | PDF text (falls back to `pdfminer` or the `pdftotext` binary) |
| `playwright` | Renders JavaScript-only pages (`playwright install chromium`) |
| `brotli` | Brotli-compressed responses |

`pip install ".[all]"` installs them and adds an `openresearch` command.

## Use it from Claude

### Claude Code (local)

This repo ships a `.mcp.json`, so opening the folder in Claude Code offers the `openresearch`
server automatically. To make it available in every project:

```
claude mcp add openresearch -- python3 -m openresearch mcp
```

(run from the repo folder, or after `pip install .`). A skill at
`.claude/skills/openresearch/SKILL.md` tells Claude when to use which tool.

### Claude app (web, desktop, mobile) as a custom connector

Custom connectors are reached from Anthropic's cloud, so the server has to be on the public
internet over HTTPS. Run the HTTP transport anywhere that gives you an HTTPS URL:

```
docker build -t openresearch .
docker run -p 8765:8765 -e OPENRESEARCH_TOKEN=<long random secret> openresearch
```

Then add a custom connector in Claude with the URL `https://<your-host>/mcp/<long random secret>`.
The secret in the path is the only thing protecting the server, so treat the URL like a password.
The server refuses to listen on a public address without a token, and private-network addresses
stay blocked so it cannot be used to reach into the host's network.

#### Running it in a GitHub Codespace

The repo includes a Codespace setup that installs the optional extras and starts the server on
port 8765 in its own terminal tab when the Codespace opens. Keep that tab open; if it is missing,
run `python -m openresearch mcp --http --host 0.0.0.0` yourself.

1. Add a Codespaces secret named `OPENRESEARCH_TOKEN` (GitHub Settings, Codespaces, Secrets) with a
   long random value, and give this repository access to it. The server refuses to start without it.
2. On the repository page: Code, Codespaces, Create codespace on main.
3. In the Ports tab, right-click port 8765, Port Visibility, Public. Or, in the terminal:
   `gh codespace ports visibility 8765:public -c $CODESPACE_NAME`
4. Your connector URL is printed by:
   `echo https://$CODESPACE_NAME-8765.$GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN/mcp/$OPENRESEARCH_TOKEN`

A Codespace stops after a period of inactivity (30
minutes by default) and uses your Codespaces hours while running; the connector works only while it
is up. For an always-on connector, run the Dockerfile on any host instead.

### Any other MCP client

```json
{ "mcpServers": { "openresearch": { "command": "python3", "args": ["-m", "openresearch", "mcp"], "cwd": "/path/to/OpenResearch" } } }
```

## Python

```python
from openresearch import Fetcher, read, search, research, crawl, map_site, extract

f = Fetcher()
page = read(f, "https://example.com/report", query="rainfall totals", max_tokens=800)
print(page.title, page.published, page.content)

results, engines = search(f, "city council ordinance short-term rentals", include_domains=["municode.com"])
pack = research(f, "who regulates short-term rentals in the city", depth="deep")
print(pack["text"])
```

## Configuration

Environment variables (or the same keys, lower-case, in `~/.config/openresearch/config.json`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENRESEARCH_EXCLUDE_DOMAINS` | | Comma list never returned by search or research |
| `OPENRESEARCH_PREFER_DOMAINS` | | Comma list ranked higher |
| `OPENRESEARCH_ENGINES` | `ddg,wikipedia` | Web engines to use (`mojeek` is opt-in: it refuses many automated clients) |
| `OPENRESEARCH_SEARXNG_URL` | | Add your own SearXNG instance as an engine |
| `BRAVE_API_KEY` | | Add Brave Search as an engine |
| `OPENRESEARCH_OPENALEX_API_KEY` | | Your own OpenAlex key for paper search (keyless requests share a small daily budget per IP address) |
| `OPENRESEARCH_CACHE_TTL` | `3600` | Seconds a cached page stays fresh |
| `OPENRESEARCH_CACHE_DIR` | `~/.cache/openresearch` | Cache location |
| `OPENRESEARCH_RESPECT_ROBOTS` | `1` | Obey `robots.txt` |
| `OPENRESEARCH_ALLOW_PRIVATE` | `0` | Allow `localhost` and private-network URLs (needed to crawl a site on your own machine or LAN) |
| `OPENRESEARCH_MIN_DELAY` | `0.3` | Seconds between requests to one host |
| `OPENRESEARCH_WAYBACK_FALLBACK` | `1` | Use the Internet Archive when a page is blocked or gone |
| `OPENRESEARCH_RENDER` | `auto` | `auto`, `never` or `always` for JavaScript rendering |
| `OPENRESEARCH_USER_AGENT` | identifies as OpenResearch | Request User-Agent |

## Search backends

Out of the box, web search uses DuckDuckGo plus Wikipedia, with no keys. DuckDuckGo throttles busy
IP addresses (cloud machines and CI especially), so add at least one backup. Every option below is
picked up automatically and fused with the others; `python -m openresearch doctor` shows which work.

**1. `ddgs` (no key, recommended first step).** Puts several search backends behind the `ddg` engine.

```bash
pip install ddgs
```

**2. Brave Search API (key, free tier).** Sign up at <https://brave.com/search/api/>, create a
subscription, copy the key from the dashboard's *API keys* page, then:

```bash
export BRAVE_API_KEY=your-key-here
```

**3. SearXNG (your own server, no key).** Point OpenResearch at an instance you run; it needs the
JSON format turned on (`search: formats: [html, json]` in its `settings.yml`).

```bash
docker run -d -p 8888:8080 --name searxng searxng/searxng
export OPENRESEARCH_SEARXNG_URL=http://localhost:8888
export OPENRESEARCH_ALLOW_PRIVATE=1      # only because this instance is on localhost
```

To keep settings without environment variables, put the same names, lower-case and without the
`OPENRESEARCH_` prefix, in `~/.config/openresearch/config.json`:

```json
{"brave_api_key": "your-key-here", "searxng_url": "https://searx.example.org"}
```

**In a Claude Code cloud session**, keep keys out of the repository and out of chat: save the Brave key
as a network secret for `api.search.brave.com` on the session's environment (header
`X-Subscription-Token`), and set `BRAVE_API_KEY` to any placeholder so the engine switches on; the
proxy puts the real key on each request.

## How it behaves

- **Polite by default.** Honest User-Agent, `robots.txt` and `Crawl-delay` respected, per-host
  rate limiting, retries with backoff on 429/5xx.
- **Safe to point an agent at.** Only `http`/`https`; private, loopback and link-local addresses
  are refused (including after redirects) unless you opt in; response size is capped. Tool
  descriptions tell the model that page text is data, not instructions.
- **Never raises on a bad page.** A failed URL comes back as an error line next to the pages that
  worked.
- **Honest provenance.** Each page reports whether it was live, cached, JavaScript-rendered, or an
  archived copy and from what date.

## Tests

```
python -m unittest discover -s tests
```

The suite runs offline against a local test site and covers extraction, ranking, fetching (cache,
ETag, redirects, retries, charsets, robots, private-network guard, PDF, feeds), crawling, structured
extraction, search fusion, the research pipeline, both MCP transports, and the CLI.

The search-engine result parsers are tested against markup modelled on each engine, not live
captures. Engines change their pages; run `python -m openresearch doctor` to see what works today
from where you are.

## License

MIT
