"""Keyless multi-engine search with reciprocal-rank fusion."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import time as _time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit, urlunsplit

from . import trace
from .dom import parse, select, select_one
from .extract import norm_date
from .fetch import Fetcher
from .page import domain_of
from .rank import tokens

_TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|msclkid|mc_cid|mc_eid|igshid|ref_src|_hsenc|_hsmi|yclid)$")
_TIME = {"day": "d", "week": "w", "month": "m", "year": "y", "d": "d", "w": "w", "m": "m", "y": "y"}
_DAYS = {"day": 1, "week": 7, "month": 31, "year": 366, "d": 1, "w": 7, "m": 31, "y": 366}
_SEARCH_TTL = 900


@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""
    date: str = ""
    extra: str = ""
    engines: list = field(default_factory=list)
    score: float = 0.0

    @property
    def domain(self) -> str:
        return domain_of(self.url)


def canonical(url: str) -> str:
    """Key for spotting the same page across engines (tracking params, www, slashes removed)."""
    s = urlsplit(url)
    host = (s.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    query = "&".join(p for p in s.query.split("&") if p and not _TRACKING.match(p.split("=")[0]))
    return urlunsplit(("", host, s.path.rstrip("/"), query, ""))


def clean_url(url: str) -> str:
    s = urlsplit(url)
    query = "&".join(p for p in s.query.split("&") if p and not _TRACKING.match(p.split("=")[0]))
    return urlunsplit((s.scheme, s.netloc, s.path, query, ""))


def _in_domains(url: str, domains) -> bool:
    d = domain_of(url)
    return any(d == x or d.endswith("." + x) for x in (y.lower().removeprefix("www.") for y in domains))


def _clean(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def _query_terms(query: str) -> set:
    """The words a result should be about: operators (site:, -word) are not among them."""
    return set(tokens(re.sub(r"(?:^|\s)-\S+|\b\w+:\S+", " ", query)))


def coverage(terms: set, r: "Result") -> float:
    """Share of the query's words found in a result's title, snippet and URL (1.0 for one-word queries)."""
    if len(terms) < 2:
        return 1.0
    have = set(tokens(f"{r.title} {r.snippet} {re.sub(r'[/_.+%-]+', ' ', r.url)}"))
    return len(terms & have) / len(terms)


def off_topic(query: str, results: list) -> bool:
    """True when an engine answered a different question, e.g. only the first word of the query (seen
    with scraped backends). The sign: hardly any result has two of the query's words in it."""
    terms = _query_terms(query)
    if len(terms) < 3 or not results or any(ord(c) >= 0x0E00 for c in query):
        return False                                 # too few words to judge, or a script written without spaces
    close = sum(1 for r in results if coverage(terms, r) * len(terms) >= 2)
    return close * 4 < len(results)


_OFF_TOPIC = "results did not match the query (the engine answered a different search); other engines are unaffected"


# ------------------------------------------------------------------ engines

def _json(f: Fetcher, url: str, **kw):
    """JSON from a search API. Raises with the real reason (rate limit, HTTP status) so the report shows it,
    and gives up quickly: a slow engine must not hold the others back."""
    kw.setdefault("timeout", 10)
    r = f.fetch(url, robots=False, retries=1, **kw)
    if not r.ok:
        raise RuntimeError(r.error or f"HTTP {r.status}")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError("reply was not JSON") from None


def _ddg_unwrap(href: str) -> str:
    if "duckduckgo.com/y.js" in href:
        return ""                                   # ad click-through
    if "uddg=" in href:
        q = parse_qs(urlsplit(href if "://" in href else "https:" + href).query)
        return unquote(q.get("uddg", [""])[0])
    if href.startswith("//"):
        return "https:" + href
    return href if href.startswith("http") else ""


def parse_ddg_html(html: str) -> list:
    doc, out = parse(html), []
    for div in select(doc, ".result"):
        if "result--ad" in div.classes:
            continue
        a = select_one(div, "a.result__a")
        if a is None:
            continue
        url = _ddg_unwrap(a.get("href"))
        if not url:
            continue
        sn = select_one(div, ".result__snippet")
        out.append(Result(a.text(), url, sn.text() if sn else "", "ddg"))
    if not out:                                     # layout changed: fall back to any wrapped link
        for a in doc.find_all("a"):
            if "uddg=" in a.get("href") and a.text():
                url = _ddg_unwrap(a.get("href"))
                if url:
                    out.append(Result(a.text(), url, "", "ddg"))
    return out


def parse_ddg_lite(html: str) -> list:
    doc = parse(html)
    anchors = select(doc, "a.result-link")
    snippets = select(doc, "td.result-snippet")
    out = []
    for i, a in enumerate(anchors):
        url = _ddg_unwrap(a.get("href"))
        if url:
            out.append(Result(a.text(), url, snippets[i].text() if i < len(snippets) else "", "ddg"))
    return out


def ddg(f: Fetcher, q: str, n: int, o: dict) -> list:
    for mod in ("ddgs", "duckduckgo_search"):      # use the ddgs library when present: it rotates backends
        try:
            DDGS = __import__(mod, fromlist=["DDGS"]).DDGS
            # "wt-wt" (no region) is DuckDuckGo's own code; the library's other backends misread it
            rows = DDGS(timeout=int(trace.clamp(8, 2))).text(q, max_results=n, region=o.get("region") or "us-en",
                                                             timelimit=_TIME.get(o.get("time")))
            out = [Result(r.get("title", ""), r.get("href", ""), r.get("body", ""), "ddg") for r in rows or []]
            if out and not off_topic(q, out):
                return out
        except ImportError:
            continue
        except Exception:
            break
    params = {"q": q, "kl": o.get("region") or "wt-wt"}
    if _TIME.get(o.get("time")):
        params["df"] = _TIME[o["time"]]
    r = f.fetch("https://html.duckduckgo.com/html/?" + urlencode(params), robots=False, max_age=_SEARCH_TTL,
                headers={"Referer": "https://html.duckduckgo.com/"}, timeout=8, retries=0)
    out = parse_ddg_html(r.text) if r.ok else []
    if not out:
        r2 = f.fetch("https://lite.duckduckgo.com/lite/?" + urlencode(params), robots=False, max_age=_SEARCH_TTL,
                     timeout=8, retries=0)
        out = parse_ddg_lite(r2.text) if r2.ok else []
        if not out and not r.ok and not r2.ok:
            if 202 in (r.status, r2.status):
                raise RuntimeError("DuckDuckGo is throttling this network (HTTP 202); `pip install ddgs` adds "
                                   "more backends, or configure Brave or SearXNG")
            raise RuntimeError(r.error or r2.error or "no results")
    if off_topic(q, out):
        raise RuntimeError(_OFF_TOPIC)
    return out[:n]


def parse_mojeek(html: str) -> list:
    doc, out = parse(html), []
    for li in select(doc, "ul.results-standard li"):
        a = select_one(li, "a.title") or select_one(li, "h2 a")
        if a is None or not a.get("href").startswith("http"):
            continue
        sn = select_one(li, "p.s")
        out.append(Result(a.text(), a.get("href"), sn.text() if sn else "", "mojeek"))
    return out


def mojeek(f, q, n, o):
    """Opt-in only (engines=["mojeek"]): Mojeek refuses many automated clients with HTTP 403."""
    r = f.fetch("https://www.mojeek.com/search?" + urlencode({"q": q}), robots=False, max_age=_SEARCH_TTL)
    if not r.ok:
        if r.status == 403:
            raise RuntimeError("HTTP 403: Mojeek refused this client")
        raise RuntimeError(r.error)
    return parse_mojeek(r.text)[:n]


def wikipedia(f, q, n, o):
    lang = (o.get("lang") or "en").split("-")[0]
    q = re.sub(r"\bsite:\S+", "", q).strip()
    data = _json(f, f"https://{lang}.wikipedia.org/w/api.php?" + urlencode(
        {"action": "query", "list": "search", "srsearch": q, "srlimit": min(n, 10), "format": "json"}),
        max_age=_SEARCH_TTL)
    return [Result(h["title"], f"https://{lang}.wikipedia.org/wiki/" + quote(h["title"].replace(" ", "_")),
                   _clean(h.get("snippet")), "wikipedia", (h.get("timestamp") or "")[:10])
            for h in data.get("query", {}).get("search", [])]


def brave(f, q, n, o):
    if not f.cfg.brave_api_key:
        raise RuntimeError("no BRAVE_API_KEY set")
    params = {"q": q, "count": min(n, 20)}
    if _TIME.get(o.get("time")):
        params["freshness"] = "p" + _TIME[o["time"]]                     # pd, pw, pm, py
    data = _json(f, "https://api.search.brave.com/res/v1/web/search?" + urlencode(params),
                 headers={"X-Subscription-Token": f.cfg.brave_api_key, "Accept": "application/json"},
                 max_age=_SEARCH_TTL)
    return [Result(_clean(r.get("title")), r.get("url", ""), _clean(r.get("description")), "brave",
                   (r.get("page_age") or "")[:10]) for r in (data.get("web") or {}).get("results", [])]


_TAVILY_URL = "https://api.tavily.com/search"


def tavily(f, q, n, o):
    """Tavily's search API (needs a key; it has a free monthly allowance). One credit per query."""
    import json as _json_mod
    if not f.cfg.tavily_api_key:
        raise RuntimeError("no TAVILY_API_KEY set")
    sites = re.findall(r"\bsite:(\S+)", q)
    body = {"query": re.sub(r"\bsite:\S+", "", q).strip(), "max_results": min(n, 20), "search_depth": "basic"}
    if sites:
        body["include_domains"] = sites
    if o.get("time") in ("day", "week", "month", "year"):
        body["time_range"] = o["time"]
    data = _json(f, _TAVILY_URL, data=_json_mod.dumps(body).encode("utf-8"), max_age=_SEARCH_TTL,
                 headers={"Authorization": "Bearer " + f.cfg.tavily_api_key.strip(), "Content-Type": "application/json"})
    trace.note(tavily_credits=1)
    return [Result(_clean(r.get("title")), r.get("url", ""), _clean(r.get("content"))[:500], "tavily",
                   norm_date(r.get("published_date") or ""))
            for r in data.get("results", []) if isinstance(r, dict)]


def searxng(f, q, n, o):
    if not f.cfg.searxng_url:
        raise RuntimeError("no searxng_url configured")
    params = {"q": q, "format": "json"}
    if o.get("time") in ("day", "week", "month", "year"):
        params["time_range"] = o["time"]
    data = _json(f, f.cfg.searxng_url.rstrip("/") + "/search?" + urlencode(params), max_age=_SEARCH_TTL)
    return [Result(r.get("title", ""), r.get("url", ""), r.get("content", ""), "searxng",
                   (r.get("publishedDate") or "")[:10]) for r in data.get("results", [])[:n]]


def arxiv(f, q, n, o):
    # "all:a b c" matches any one word; every word must be required for relevant results
    words = [w for w in re.findall(r"[\w-]+", re.sub(r"\bsite:\S+", "", q)) if len(w) > 1][:10]
    query = " AND ".join("all:" + w for w in words) or "all:" + q
    r = f.fetch("https://export.arxiv.org/api/query?" + urlencode(
        {"search_query": query, "max_results": n, "sortBy": "relevance"}), robots=False, max_age=_SEARCH_TTL,
        timeout=10, retries=1)
    if not r.ok:
        raise RuntimeError(r.error)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(r.body).findall("a:entry", ns):
        authors = [a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)]
        out.append(Result(_clean(e.findtext("a:title", "", ns)), (e.findtext("a:id", "", ns) or "").strip(),
                          _clean(e.findtext("a:summary", "", ns))[:500], "arxiv",
                          (e.findtext("a:published", "", ns) or "")[:10],
                          ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else "")))
    return out


def openalex(f, q, n, o):
    params = {"search": q, "per-page": min(n, 25)}
    if f.cfg.openalex_api_key:               # keyless requests share one small daily budget per IP address
        params["api_key"] = f.cfg.openalex_api_key
    data = _json(f, "https://api.openalex.org/works?" + urlencode(params), max_age=_SEARCH_TTL)
    out = []
    for w in data.get("results", []):
        inv = w.get("abstract_inverted_index") or {}
        words = sorted((p, t) for t, ps in inv.items() for p in ps)
        url = ((w.get("open_access") or {}).get("oa_url") or ((w.get("primary_location") or {}).get("landing_page_url"))
               or w.get("doi") or w.get("id") or "")
        authors = [(a.get("author") or {}).get("display_name", "") for a in w.get("authorships", [])[:4]]
        out.append(Result(w.get("title") or "", url, " ".join(t for _, t in words)[:500], "openalex",
                          str(w.get("publication_date") or w.get("publication_year") or ""),
                          f"{', '.join(a for a in authors if a)} | cited by {w.get('cited_by_count', 0)}"))
    return out


def crossref(f, q, n, o):
    data = _json(f, "https://api.crossref.org/works?" + urlencode({"query": q, "rows": min(n, 20)}),
                 max_age=_SEARCH_TTL)
    out = []
    for w in data.get("message", {}).get("items", []):
        parts = ((w.get("issued") or {}).get("date-parts") or [[]])[0]
        date = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts) if p)
        authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in w.get("author", [])[:4]]
        venue = (w.get("container-title") or [""])[0]
        out.append(Result(_clean((w.get("title") or [""])[0]), w.get("URL", ""), _clean(w.get("abstract"))[:500],
                          "crossref", date,
                          " | ".join(x for x in (", ".join(authors), venue,
                                                 f"cited by {w.get('is-referenced-by-count', 0)}") if x)))
    return out


def hackernews(f, q, n, o):
    params = {"query": q, "hitsPerPage": min(n, 30)}
    days = _DAYS.get(o.get("time"))
    if days:
        import time as _time
        params["numericFilters"] = f"created_at_i>{int(_time.time()) - days * 86400}"
    data = _json(f, "https://hn.algolia.com/api/v1/search?" + urlencode(params), max_age=_SEARCH_TTL)
    out = []
    for h in data.get("hits", []):
        thread = f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        out.append(Result(h.get("title") or h.get("story_title") or "", h.get("url") or thread,
                          _clean(h.get("story_text") or h.get("comment_text"))[:300], "hackernews",
                          (h.get("created_at") or "")[:10],
                          f"{h.get('points') or 0} points, {h.get('num_comments') or 0} comments | {thread}"))
    return [r for r in out if r.title]


def parse_bingnews(xml: bytes, days: int | None = None) -> list:
    import datetime
    from email.utils import parsedate_to_datetime
    try:
        items = list(ET.fromstring(xml).iter("item"))
    except ET.ParseError:
        raise RuntimeError("reply was not an RSS feed") from None
    out = []
    for it in items:
        link = (it.findtext("link") or "").strip()
        real = parse_qs(urlsplit(link).query).get("url", [""])[0] if "apiclick" in link else link
        try:
            when = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            when = None
        if days and when is not None and (datetime.datetime.now(when.tzinfo) - when).days > days:
            continue
        source = next((c.text or "" for c in it if c.tag.endswith("}Source")), "")
        out.append(Result(_clean(it.findtext("title")), real, _clean(it.findtext("description"))[:400], "bingnews",
                          when.strftime("%Y-%m-%d") if when else "", source.strip()))
    return out


def bingnews(f, q, n, o):
    """Bing's public news RSS feed: recent articles with publication dates and direct links."""
    r = f.fetch("https://www.bing.com/news/search?" + urlencode({"q": q, "format": "rss"}), max_age=_SEARCH_TTL,
                timeout=10, retries=1)
    if not r.ok:
        raise RuntimeError(r.error)
    return parse_bingnews(r.body, _DAYS.get(o.get("time")))[:n]


def github(f, q, n, o):
    headers = {"Accept": "application/vnd.github+json"}
    if f.cfg.github_token:
        headers["Authorization"] = "Bearer " + f.cfg.github_token
    data = _json(f, "https://api.github.com/search/repositories?" + urlencode({"q": q, "per_page": min(n, 30)}),
                 headers=headers, max_age=_SEARCH_TTL)
    return [Result(r.get("full_name", ""), r.get("html_url", ""), r.get("description") or "", "github",
                   (r.get("pushed_at") or "")[:10],
                   f"{r.get('stargazers_count', 0)} stars | {r.get('language') or 'n/a'}")
            for r in data.get("items", [])]


def stackexchange(f, q, n, o):
    data = _json(f, "https://api.stackexchange.com/2.3/search/advanced?" + urlencode(
        {"order": "desc", "sort": "relevance", "q": q, "site": o.get("se_site") or "stackoverflow",
         "pagesize": min(n, 30)}), max_age=_SEARCH_TTL)
    import html as _html
    return [Result(_html.unescape(i.get("title", "")), i.get("link", ""), "", "stackexchange", "",
                   f"score {i.get('score', 0)}, {i.get('answer_count', 0)} answers"
                   + (", answered" if i.get("is_answered") else ""))
            for i in data.get("items", [])]


ENGINES = {"tavily": tavily, "ddg": ddg, "bingnews": bingnews, "mojeek": mojeek, "wikipedia": wikipedia, "brave": brave, "searxng": searxng,
           "arxiv": arxiv, "openalex": openalex, "crossref": crossref, "hackernews": hackernews,
           "github": github, "stackexchange": stackexchange}
WEIGHT = {"wikipedia": 0.5, "mojeek": 0.7}
VERTICALS = {
    "web": ["ddg", "wikipedia"],            # mojeek is opt-in: it 403s automated clients
    "news": ["bingnews", "ddg", "hackernews"],
    "papers": ["openalex", "arxiv", "crossref"],
    "code": ["github", "stackexchange", "ddg"],
    "discussions": ["hackernews", "ddg"],
}


# Started only when the usual engines are slow or return next to nothing (results weighted lower).
# DuckDuckGo heads the list: with a keyed engine configured it is a fallback, not a default, because it
# is slow and often throttled from hosting providers. Without one it stays a default (see engines_for).
BACKUP = {"web": ["ddg", "bingnews", "hackernews"], "news": ["ddg"], "code": ["hackernews"],
          "discussions": ["bingnews"]}
KEYED = ("tavily", "brave", "searxng")      # engines with their own key or server: dependable


def engines_for(f: Fetcher, vertical: str, engines=None) -> list:
    if engines:
        return [e for e in engines if e in ENGINES]
    names = list(VERTICALS.get(vertical) or VERTICALS["web"])
    if vertical in ("web", "news") or vertical not in VERTICALS:
        if f.cfg.engines:
            names = [e for e in f.cfg.engines if e in ENGINES]
        if f.cfg.brave_api_key and "brave" not in names:
            names.insert(0, "brave")
        if f.cfg.tavily_api_key and "tavily" not in names:
            names.insert(0, "tavily")
        if f.cfg.searxng_url and "searxng" not in names:
            names.insert(0, "searxng")
        if not f.cfg.engines and any(k in names for k in KEYED):
            names = [e for e in names if e != "ddg"]   # demoted to a fallback (BACKUP)
    return names


def search(f: Fetcher, query: str, n: int = 10, *, vertical: str = "web", engines=None, site: str = "",
           include_domains=None, exclude_domains=None, time: str = "", region: str = "",
           per_domain: int = 3) -> tuple:
    """Search several engines at once and fuse the rankings.

    Returns (results, report) where report maps each engine to a result count or an error string.
    """
    include = [d for d in (include_domains or []) if d]
    exclude = list(exclude_domains or []) + list(f.cfg.exclude_domains)
    names = engines_for(f, vertical, engines)
    opts = {"time": time if time else ("week" if vertical == "news" else ""), "region": region}
    scoped = [site] if site else (include if 0 < len(include) <= 4 else [])
    want = min(max(n * 2, 10), 30)

    def plan(engine_names) -> list:
        jobs = []
        for name in engine_names:
            if scoped and name in ("tavily", "ddg", "bingnews", "mojeek", "brave", "searxng"):
                jobs += [(name, f"{query} site:{d}") for d in scoped]
            else:
                jobs.append((name, query))
        return jobs

    def run(job):
        name, q = job
        t0, rows, err = _time.monotonic(), [], ""
        try:
            rows = ENGINES[name](f, q, want, opts)
        except Exception as e:                       # one engine failing must not sink the search
            err = f"{type(e).__name__}: {e}"[:200]
        trace.event("engine", engine=name, query=q, ms=int((_time.monotonic() - t0) * 1000), results=len(rows),
                    error=err)
        return rows, err

    counts, errors, merged = {}, {}, {}
    terms = _query_terms(query)

    def absorb(name, rows, err, weight=1.0):
        counts[name] = counts.get(name, 0) + len(rows)
        if err:
            errors[name] = err
        for rank, r in enumerate(rows):
            if not r.url.startswith("http") or not r.title:
                continue
            r.url = clean_url(r.url)
            if _in_domains(r.url, exclude) or (site and not _in_domains(r.url, [site])):
                continue
            if include and not _in_domains(r.url, include):
                continue
            key = canonical(r.url)
            cur = merged.get(key)
            gain = weight * WEIGHT.get(name, 1.0) / (60 + rank)
            if cur is None:
                r.engines, r.score = [name], gain
                merged[key] = r
            else:
                cur.score += gain
                if name not in cur.engines:
                    cur.engines.append(name)
                if len(r.snippet) > len(cur.snippet):
                    cur.snippet = r.snippet
                cur.date = cur.date or r.date
                cur.extra = cur.extra or r.extra

    # Engines run side by side and none is waited for beyond the time limit: a slow or hung engine is
    # reported as timed out instead of holding up the answer. If the usual engines are slow or return
    # next to nothing, backup engines are started as well.
    limit = trace.clamp(f.cfg.engine_timeout or 12.0)
    backups = [] if (engines or f.cfg.engines) else [e for e in BACKUP.get(vertical, []) if e not in names]
    started = _time.monotonic()
    health = getattr(f, "engine_health", {})
    if not engines:
        # An engine that gave no answer twice running is left out for a few minutes, so that every
        # call does not wait the full time limit for it again. Engines named by the caller always run.
        resting = [e for e in names if health.get(e, [0, 0])[1] > started]
        if resting and (len(resting) < len(names) or backups):
            for name in resting:
                msg = f"skipped: no answer the last 2 times, trying again in {health[name][1] - started:.0f}s"
                counts[name], errors[name] = 0, msg
                trace.event("engine", engine=name, query=query, error=msg)
            names = [e for e in names if e not in resting]
    pool = ThreadPoolExecutor(max_workers=8)
    try:
        pending = {pool.submit(trace.bind(run), job): (job, 1.0) for job in plan(names)}

        def collect(timeout):
            done, _ = wait(list(pending), timeout=max(timeout, 0))
            for fut in [x for x in pending if x in done]:      # in the order asked, not the order answered
                (name, _q), weight = pending.pop(fut)
                health.pop(name, None)
                rows, err = fut.result()
                absorb(name, rows, err, weight)

        collect(min(limit, 4.0) if backups else limit)
        if backups and len(merged) < 3:
            for job in plan(backups):
                pending[pool.submit(trace.bind(run), job)] = (job, 0.6)
        if pending:
            collect(limit - (_time.monotonic() - started))
        for (name, q), _w in pending.values():
            waited = _time.monotonic() - started
            counts.setdefault(name, 0)
            errors.setdefault(name, f"no answer within {waited:.0f}s (gave up waiting)")
            if waited >= 3:                              # not a call that simply ran out of its own time
                strikes = health.get(name, [0, 0])[0] + 1
                health[name] = [strikes, _time.monotonic() + 180 if strikes >= 2 else 0]
            trace.event("timeout", engine=name, query=q, ms=int(waited * 1000),
                        error=f"no answer within {waited:.0f}s (gave up waiting)")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    order = {name: i for i, name in enumerate(names + backups)}
    for r in merged.values():                        # same listing whichever engine happened to answer first
        r.engines.sort(key=lambda e: order.get(e, 99))
    for r in merged.values():
        r.score *= 0.4 + 0.6 * coverage(terms, r)    # a result about one word of the query ranks below one about all
        if f.cfg.prefer_domains and _in_domains(r.url, f.cfg.prefer_domains):
            r.score *= 1.5
    report = {name: (counts[name] if counts[name] or name not in errors else errors[name]) for name in counts}
    out, per = [], {}
    for r in sorted(merged.values(), key=lambda r: -r.score):
        if per_domain and per.get(r.domain, 0) >= per_domain:
            continue
        per[r.domain] = per.get(r.domain, 0) + 1
        out.append(r)
        if len(out) >= n:
            break
    t = trace.current()
    if t is not None and t.tool == "search":         # credit the engines whose results were shown
        shown = {}
        for r in out:
            for name in r.engines:
                shown[name] = shown.get(name, 0) + 1
        trace.note(source_engines=shown, results_shown=len(out))
    return out, report


def format_results(results: list, report: dict | None = None, start: int = 1) -> str:
    lines = []
    for i, r in enumerate(results, start):
        meta = " | ".join(x for x in (r.domain, r.date, r.extra) if x)
        lines.append(f"{i}. [{r.title}]({r.url})\n   {meta}" + (f"\n   {r.snippet[:320]}" if r.snippet else ""))
    if not results:
        lines.append("No results.")
    if report:
        bits = [f"{k}: {v}" if isinstance(v, int) else f"{k}: FAILED ({v})" for k, v in report.items()]
        lines.append("\nEngines - " + "; ".join(bits))
    return "\n".join(lines)
