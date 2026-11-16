# OpenResearch

Web research tool for AI agents. Python standard library only: do not add required dependencies.
Optional ones (`ddgs`, `pypdf`, `playwright`, `brotli`) must be imported lazily and degrade cleanly.

## Layout

- `openresearch/dom.py` HTML tree + CSS selectors; `markdown.py` DOM to Markdown; `extract.py` main-content and metadata
- `fetch.py` HTTP, cache, robots, private-network guard; `page.py` URL to `Page` (PDF, feeds, JS render, Wayback)
- `search.py` engines + rank fusion; `rank.py` chunking and BM25; `research.py` the one-call pipeline
- `crawl.py` map and crawl; `structured.py` selector/table/JSON-LD extraction; `feeds.py` RSS/Atom/JSON Feed discovery and reading
- `tools.py` the tool schemas (seven web tools plus `log` and `guide`) and dispatcher, shared by `mcp_server.py` and `cli.py`
- `guide.py` the usage playbook served by the `guide` tool; keep it in step with the engines and tools
- `logship.py` copies finished log entries to a GitHub repository (optional, off by default)
- `trace.py` per-call log and time limit: `trace.event(...)` records a step, `trace.note(...)` records a number about the call's work, `trace.remaining()` is the time left
- `insights.py` per-call summary, statistics across calls and suggestions, also from LogView files

## Rules

- Run `python -m unittest discover -s tests` before committing; tests must stay offline.
- A new tool or parameter goes in `tools.py` once; the MCP server and CLI both read it from there.
- Fetching never raises for a bad page: return a `Response`/`Page` with `.error` set.
- Keep the private-network guard and robots.txt check on by default.
- Work handed to a thread pool must be wrapped in `trace.bind(...)`, or its events and time limit are lost.
- New search engines: add a function to `ENGINES` in `search.py`, raise on failure so the report shows it.
