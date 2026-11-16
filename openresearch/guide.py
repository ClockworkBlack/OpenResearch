"""How to use OpenResearch well: a playbook the calling model can read on demand (the `guide` tool).

Kept out of the tool descriptions so it costs nothing until it is asked for. The `engines` topic
is built from the server's own settings, so it lists the engines that are actually in use.
"""
from __future__ import annotations

TOPICS = ("overview", "queries", "engines", "recipes", "troubleshooting")

OVERVIEW = """\
# OpenResearch guide

Pick the tool by the job:
- A question that needs several sources: `research` with 2-5 queries of your own, each from a
  different angle. One call searches, reads the best pages and returns only relevant passages.
- One fact, or one page you already know: `search`, then `fetch` the best result with `query` set.
- A page somewhere on a known site: `map` with `search`, then `fetch`.
- A whole section of a site (docs, a blog): `crawl` with `query` and `include` globs.
- Tables, product lists, metadata as JSON: `extract`.
- A call was slow, empty or odd: `log` with the call id printed at the end of the result.

Five rules that matter most:
1. Write queries as distinctive keywords (names, versions, error text), not as a polite sentence.
2. Several narrow queries beat one broad one; give `research` different angles, not rephrasings.
3. Pass `query` to `fetch` on long pages, so only the relevant passages come back.
4. Use `time` for anything recent, `vertical` for news, papers, code or discussions.
5. Check dates and cite sources by their [number]; say when a source is undated or an archived copy.

More: `guide` with topic queries, engines, recipes or troubleshooting.
"""

QUERIES = """\
# Writing queries

- Keywords, not sentences: `ultracode Claude Code effort` beats "can you tell me what ultracode is".
  `research` adds a keyword version of the question by itself, so spend your queries on other angles.
- Be specific: product and version names, people, places, years, model numbers, exact error text.
- One idea per query. For a comparison, search each side and then the comparison:
  `Tavily pricing`, `Brave Search API pricing`, `Tavily vs Brave search API`.
- Angles that find different pages: the official source (`site:docs.example.com topic`), a definition
  or explainer, recent news (`time: month`), criticism or problems (`topic issues`, `topic limitations`),
  numbers (`topic statistics 2026`), and discussion (vertical `discussions`).
- "Exact phrase" in quotes: engines that support it match it, and `research` ranks passages that
  contain it higher.
- `site:example.com` (or `include_domains`) keeps to one site; `-word` drops results with a word
  (DuckDuckGo, SearXNG); `exclude_domains` drops sites that block reading or are low quality.
- Recent events: `time` day/week/month/year. Without it, older well-linked pages tend to win.
- Other languages: write the query in the language the sources are written in.
- Thin results: drop a word, use the common name instead of a nickname, or try the vertical that
  fits (papers, code, news) before rewriting everything.
"""

_ENGINES = {
    "tavily": "general web search built for AI tools; understands plain questions as well as keywords; "
              "honours site: and time. Each query uses one credit, so prefer a few good queries over many.",
    "brave": "independent general web index; keywords; honours time.",
    "searxng": "your own meta-search server; combines several engines.",
    "ddg": "DuckDuckGo, largely Bing's index; keywords, \"phrases\", site:, -word. Slow or throttled from "
           "some hosts.",
    "wikipedia": "encyclopedia articles; matches names of things (people, places, concepts). Poor for news, "
                 "prices or how-to.",
    "bingnews": "news articles with publication dates, mostly the last few weeks. Good for events and "
                "announcements; not for background.",
    "hackernews": "tech community links and discussion (product launches, reception, opinions). Shows points "
                  "and comment counts.",
    "arxiv": "research preprints. Every word must appear, so use 2-4 technical terms "
             "(`retrieval augmented generation evaluation`), not a sentence.",
    "openalex": "scholarly works with citation counts; titles, topics, authors.",
    "crossref": "published papers by title, author or DOI; best for finding the exact paper.",
    "github": "repositories. Supports GitHub qualifiers: `language:python`, `stars:>500`, `topic:mcp`, "
              "`in:readme`, `pushed:>2026-01-01`.",
    "stackexchange": "Stack Overflow questions; paste the key part of an error message, or name the library "
                     "and the task.",
    "mojeek": "independent index; opt-in, often refuses automated clients.",
}


def engines_topic(f) -> str:
    from .search import BACKUP, engines_for
    lines = ["# Search engines", ""]
    for vertical in ("web", "news", "papers", "code", "discussions"):
        names = engines_for(f, vertical)
        backup = [] if f.cfg.engines else [e for e in BACKUP.get(vertical, []) if e not in names]
        lines.append(f"- vertical `{vertical}` asks: {', '.join(names) or 'none'}"
                     + (f"; fallbacks when those fail or find little: {', '.join(backup)}" if backup else ""))
    keyed = [k for k, v in (("tavily", f.cfg.tavily_api_key), ("brave", f.cfg.brave_api_key),
                            ("searxng", f.cfg.searxng_url)) if v]
    lines += ["", "Keyed engines configured on this server: " + (", ".join(keyed) or "none"), "",
              "What each engine is good at (name them in `engines` on `search` to use only those):"]
    for name, text in _ENGINES.items():
        lines.append(f"- `{name}`: {text}")
    lines += ["", "The default mix is usually right. Name engines when one fits the job exactly: `crossref` "
              "for a paper title, `github` for a repository, `stackexchange` for an error, `bingnews` for "
              "this week's news.", ""]
    return "\n".join(lines)


RECIPES = """\
# Recipes

**What's new with X / latest version**
`research` with `time: month`, queries like `X release notes`, `X changelog 2026`, `X new features`;
then `fetch` the official changelog with `query`. For an event, vertical `news`.

**How does X work / explain X**
`research` with queries for the official docs (`site:` the vendor), an explainer, and `X limitations`.
Prefer passages from [official] and vendor sources; use blogs for examples.

**Compare A and B**
`research` with `A pricing`, `B pricing`, `A vs B`, and `A B benchmark`. Make a table only from numbers
you saw in the passages, with each number's source.

**Check a claim**
`research` with the claim's key terms plus `include_domains` for primary sources (government,
standards bodies, the company). Report what each source says and its date; flag disagreements.

**Find a specific page on a site**
`map` with `url` = the site and `search` = what you want; then `fetch` the best URL with `query`.

**Read documentation in depth**
`crawl` from the docs index with `query` and `include: ["/docs/*"]`, `max_pages` 10-20.

**Academic question**
vertical `papers` with technical terms; `fetch` the arXiv or publisher PDF with `query` for the method or
result you need; quote numbers with the paper and year.

**Code problem or error**
vertical `code` (GitHub, Stack Overflow); quote the stable part of the error message; `fetch` the
accepted answer or the repository README with `query`.

**Opinions and reception**
vertical `discussions` (Hacker News) plus `research` with `X review`, `X problems`.

**Data in tables**
`extract` with `want: ["tables"]`, or `selectors` for repeated items; it returns JSON without guessing.
"""

TROUBLESHOOTING = """\
# When results disappoint

Every result ends with `[call <id>: <seconds>, <n> problem(s)]`. `log` with that id shows which engine
failed, which pages could not be read and why, and suggestions. `log` with `stats` covers many calls.

- **Nothing relevant**: the engines answered a different question. Use fewer, more distinctive words; add
  the product or field name; try another vertical; or name a fitting engine.
- **Pages failed with "bot check" or "forbidden"**: the site refuses automated readers. Do not retry it;
  add it to `exclude_domains` and use other sources. The search snippet in "Gaps and leads" often holds
  the fact anyway.
- **"time limit" in the gaps**: the call ran out of time. Ask again with `depth: quick`, fewer queries,
  or `fetch` the two or three pages you need.
- **Truncated page**: `fetch` again with the `offset` given, or better, with a `query`.
- **Old information**: add `time`, put the year in the query, and check each source's date.
- **An engine keeps failing**: the call note and `log` say which; the others still run. A keyed engine
  (Tavily, Brave) on the server makes search dependable.
- **Empty page or "loading"**: `fetch` with `render: true` (if the server has a browser), or look for
  the same content elsewhere.
"""


def guide(f, topic: str = "") -> str:
    topic = (topic or "overview").strip().lower()
    if topic == "all":
        return "\n\n".join(guide(f, t) for t in TOPICS)
    if topic == "engines":
        return engines_topic(f)
    text = {"overview": OVERVIEW, "queries": QUERIES, "recipes": RECIPES,
            "troubleshooting": TROUBLESHOOTING}.get(topic)
    if text is None:
        raise ValueError(f"unknown guide topic: {topic} (choose from {', '.join(TOPICS)} or all)")
    return text
