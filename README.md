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

## The tools

| Tool | What it does |
| --- | --- |
| `research` | Runs your queries across engines, reads the best pages in parallel, returns the most relevant passages grouped under numbered sources (title, domain, date, URL), then lists gaps: question terms nothing covered, pages that failed (with why, and their search snippet), and unread leads. A page that cannot be read gives its slot to the next result. |
| `search` | Multi-engine search with reciprocal-rank fusion. Verticals: `web`, `news` (Bing News feed, DuckDuckGo, Hacker News), `papers` (OpenAlex, arXiv, Crossref), `code` (GitHub, Stack Overflow), `discussions` (Hacker News). Filters: `site`, `include_domains`, `exclude_domains`, `time`. |
| `fetch` | One or many URLs to clean Markdown: HTML, PDF, JSON, RSS/Atom, sitemaps, plain text. Pass `query` to get only the relevant passages. `offset` pages through long documents; `render` loads the page in a headless browser first. |
| `crawl` | Crawls a site within its domain. With `query` it is best-first: the links most related to your question are followed first and each page is trimmed to what matters. Glob `include` / `exclude`, depth and page limits, optional sitemap seeding. |
| `map` | Lists a site's URLs from sitemaps and the start page without reading them; `search` orders them by relevance. |
| `extract` | Structured JSON: your CSS selectors (`css::text`, `css::attr(href)`, nested `fields`), tables as rows and records, JSON-LD, metadata, links, and `__NEXT_DATA__`-style embedded state. |
| `feeds` | Latest items from RSS, Atom, RSS 1.0 and JSON Feed sources, newest first. Give sites or feed URLs: a site's feeds are found from its pages or the usual addresses (`/feed`, `/rss.xml`, ...). Several sources merge into one list; filter by `query` and `days`, cap each source with `per_feed`, and `read` the newest articles. |

An eighth tool, `guide`, is a playbook for the calling model: how to write precise queries, which
engine suits which job (listing the engines this server actually uses), recipes for common research
tasks, and what to do when results disappoint. Claude reads it when it needs it, so it costs nothing
otherwise.

Every result ends with a short note such as `[call 3f2a9c1e: 12.3s, 2 problem(s)]`. A seventh tool,
`log`, takes that id and shows what the call did. See [Seeing what happened](#seeing-what-happened).

## Install

Nothing is required beyond Python 3.10+.

```
git clone https://github.com/ClockworkBlack/OpenResearch
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

### On your phone, in any Claude session

Claude's apps (and Claude Code sessions started from them) reach MCP servers through **connectors**,
which live on your claude.ai account, so one connector works in every chat and every session.
A connector needs the server on the public internet, so host it once:

1. Tap **[Deploy to Render](https://render.com/deploy?repo=https://github.com/ClockworkBlack/OpenResearch)**,
   sign in, and approve the blueprint (`render.yaml`). Render builds the Dockerfile and generates a
   secret `OPENRESEARCH_TOKEN`.
2. When it's live, open the service: copy its address (`https://openresearch-xxxx.onrender.com`) and,
   under **Environment**, the value of `OPENRESEARCH_TOKEN`.
3. Open <https://claude.ai/customize/connectors>, choose **Add custom connector**, name it OpenResearch,
   and paste `https://openresearch-xxxx.onrender.com/mcp/<token>` as the URL. Treat it like a password.
4. Start a new chat or session and turn the connector on. Check it with `…/health` in a browser.

The free plan sleeps after 15 idle minutes, so the first request after a pause can take a minute
while it wakes; the paid "starter" plan stays awake. Pushes to `main` redeploy automatically.

### Sharing it with colleagues, or with anyone

Each person runs their own copy; a connector URL is a password and is not meant to be passed around.
Send them this repository. They tap
**[Deploy to Render](https://render.com/deploy?repo=https://github.com/ClockworkBlack/OpenResearch)**
and follow the four steps above, which gives them their own server, their own secret and their own
free-plan allowance. To run it on their own machine instead:
`pip install "git+https://github.com/ClockworkBlack/OpenResearch"`, then
`claude mcp add openresearch -- openresearch mcp`.

Without hosting anything, Claude Code cloud sessions on this repository get the server from
`.mcp.json`, and the `openresearch` skill falls back to `python3 -m openresearch` on the command
line wherever the package is installed.

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
| `TAVILY_API_KEY` | | Add Tavily as an engine |
| `OPENRESEARCH_OPENALEX_API_KEY` | | Your own OpenAlex key for paper search (keyless requests share a small daily budget per IP address) |
| `OPENRESEARCH_TOOL_TIMEOUT` | `50` | Seconds a tool call may take before it returns what it has (`0` = no limit) |
| `OPENRESEARCH_ENGINE_TIMEOUT` | `12` | Seconds to wait for any one search engine |
| `OPENRESEARCH_LOG_KEEP` | `300` | How many finished calls the log remembers |
| `OPENRESEARCH_LOG_STDERR` | `0` | Also print one JSON line per call to stderr |
| `OPENRESEARCH_LOG_GITHUB_REPO` | | Repository (`owner/name` or URL) that receives a copy of the call log |
| `OPENRESEARCH_LOG_GITHUB_TOKEN` | | Token allowed to write that repository's contents |
| `OPENRESEARCH_LOG_GITHUB_DIR` | `openresearch` | Folder in that repository |
| `OPENRESEARCH_CACHE_TTL` | `3600` | Seconds a cached page stays fresh |
| `OPENRESEARCH_CACHE_DIR` | `~/.cache/openresearch` | Cache location |
| `OPENRESEARCH_RESPECT_ROBOTS` | `1` | Obey `robots.txt` |
| `OPENRESEARCH_ALLOW_PRIVATE` | `0` | Allow `localhost` and private-network URLs (needed to crawl a site on your own machine or LAN) |
| `OPENRESEARCH_MIN_DELAY` | `0.3` | Seconds between requests to one host |
| `OPENRESEARCH_WAYBACK_FALLBACK` | `1` | Use the Internet Archive when a page is blocked or gone |
| `OPENRESEARCH_RENDER` | `auto` | `auto`, `never` or `always` for JavaScript rendering |
| `OPENRESEARCH_USER_AGENT` | identifies as OpenResearch | Request User-Agent |

## Search backends

Out of the box, web search uses DuckDuckGo plus Wikipedia, with no keys. Once a keyed engine is set up
(Tavily, Brave or SearXNG), it takes DuckDuckGo's place and DuckDuckGo becomes a fallback: it is only
asked when the other engines fail or find next to nothing, because it is slow and often throttled from
hosting providers. `/health` lists the engines in use (`web_engines`) and the fallbacks (`web_fallbacks`). DuckDuckGo throttles busy
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

**3. Tavily (key, free monthly allowance).** A search API made for AI tools. Sign up at
<https://app.tavily.com>, copy the API key (it starts with `tvly-`), then:

```bash
export TAVILY_API_KEY=tvly-your-key-here
```

Each search query uses one credit; a `research` call usually runs two to four queries.

**4. SearXNG (your own server, no key).** Point OpenResearch at an instance you run; it needs the
JSON format turned on (`search: formats: [html, json]` in its `settings.yml`).

```bash
docker run -d -p 8888:8080 --name searxng searxng/searxng
export OPENRESEARCH_SEARXNG_URL=http://localhost:8888
export OPENRESEARCH_ALLOW_PRIVATE=1      # only because this instance is on localhost
```

To keep settings without environment variables, put the same names, lower-case and without the
`OPENRESEARCH_` prefix, in `~/.config/openresearch/config.json`:

```json
{"brave_api_key": "your-key-here", "tavily_api_key": "tvly-your-key-here"}
```

**In a Claude Code cloud session**, keep keys out of the repository and out of chat: save the Brave key
as a network secret for `api.search.brave.com` on the session's environment (header
`X-Subscription-Token`), and set `BRAVE_API_KEY` to any placeholder so the engine switches on; the
proxy puts the real key on each request.

## Seeing what happened

Every tool call is recorded: which search engines answered, failed or were given up on, each page
read or not read and why, how long every step took, and the traceback if something crashed.

- **From Claude:** ask it to "check the OpenResearch log" (the `log` tool). With no arguments it lists
  recent calls; with a call's id it shows that call's timeline; `problems_only` shows just what went wrong.
- **In a browser** (hosted server): `https://<your-host>/log/<token>` lists recent calls;
  add `?id=<call id>` for one call, or `?problems=1` for only the calls that had trouble.
- **Command line:** `python -m openresearch log`, `python -m openresearch log <id>`, `--problems`, `--clear`.
- **Statistics:** `log` with `stats` (or `/log/<token>?stats=1`, or `python -m openresearch log --stats`)
  measures many calls together and lists what to change. See below.
- **Hosted logs:** with `OPENRESEARCH_LOG_STDERR=1` (set in the Dockerfile) each call also prints one
  JSON line, so it shows up in your host's log viewer (Render: the service's **Logs** tab).

```
Call 3f2a9c1e: research - OK in 12.3s (limit 50s), 2 problem(s), 9120 chars returned
Timeline (seconds from start; ! = problem):
  +   0.9s  engine  wikipedia: 10 results | query "reciprocal rank fusion" | 0.9s
! +  12.0s  timeout engine=ddg error=no answer within 12s (gave up waiting)
  +  12.6s  page    https://example.org/rrf -> read, 18234 chars | 0.5s
! +  13.1s  page    https://example.com/x -> FAILED - HTTP 403 (the site blocks automated readers with a browser check)
```

The log keeps the last 300 calls in `log.db` beside the page cache. On a host with a temporary disk
(Render's free plan) it starts empty after each restart, so keep a copy on GitHub:

**Keeping the log on GitHub.** Give the server a repository and a token, and each finished call is also
written there about 15 seconds later (calls close together share one file), as
`openresearch/<date>/<time>-<tool>-<call id>.md`: the readable timeline, with the raw JSON underneath.
Whatever is still waiting is sent when the server shuts down.

1. Use a **private** repository: the log contains your questions and the pages read.
2. On GitHub: Settings, Developer settings, Personal access tokens, Fine-grained tokens, Generate new
   token. Repository access: *Only select repositories*, the log repository. Permissions:
   *Contents: Read and write*. Nothing else.
3. On the server set `OPENRESEARCH_LOG_GITHUB_REPO` (for example `yourname/LogView`) and
   `OPENRESEARCH_LOG_GITHUB_TOKEN` (the token). On Render: the service's **Environment** tab.

`log` then ends with a line such as `Copy on GitHub - yourname/LogView/openresearch: 12 call(s) saved`,
or the reason an upload failed. A failed upload never affects a tool call; it is retried with the next one.

**What each entry measures.** Besides the timeline, every call carries a summary and suggestions:

- time split into searching, reading and ranking, and the share of the time limit used;
- per search engine: queries, results, seconds per query, failures and time-outs, and how many of the
  pages it found ended up as research sources (the real measure of an engine's worth);
- pages read and failed, with each failure sorted by cause (bot check, forbidden, not found, rate
  limited, time limit, no readable text...) and the site it came from;
- for `research`: queries run and whether they had to be widened, results found, pages picked and
  backfilled, sources and passages used, tokens returned against the budget, question words with no evidence;
- network: requests, cache hits, kilobytes downloaded, time spent on robots.txt, the slowest requests;
- the server version and deployed commit, whether the call woke the server (cold start), Tavily credits used.

**Statistics over many calls** add these up per tool (calls, time-outs, average and 90th-percentile
time) and per engine (how often it answers, how fast, how many sources it supplies), list the sites
that fail most and why, and turn it into suggestions, for example "ddg answered only 40% of 25 queries"
or "stackoverflow.com failed 6 times (bot check): add it to OPENRESEARCH_EXCLUDE_DOMAINS". The server only
remembers its last 300 calls (fewer after a restart); for everything kept on GitHub, clone the log
repository and run `python -m openresearch log --stats --dir LogView/openresearch`.

**Time limits.** A tool call returns within `OPENRESEARCH_TOOL_TIMEOUT` seconds (default 50) with whatever
it has, and says what it skipped. No search engine is waited for longer than
`OPENRESEARCH_ENGINE_TIMEOUT` (default 12): a slow one is reported as timed out, and if the usual
engines are slow or nearly empty, backup engines (DuckDuckGo when a keyed engine is set, Bing News,
Hacker News) fill in. An engine that gives
no answer twice in a row is left out for three minutes, so later calls do not wait for it again.

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
