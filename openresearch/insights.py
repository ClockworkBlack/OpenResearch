"""Make the call log useful for tuning: a summary of each call, statistics across many calls, and
plain suggestions about what to change.

`summarize(entry)` turns one call's timeline into numbers (where the time went, what each engine
contributed, why pages failed, cache use). `aggregate(entries)` adds those up across calls, and
`suggest(...)` reads them and says what is worth changing. `load_dir(path)` reads the copies kept in a
GitHub log repository, so statistics can cover far more than the server's own short memory.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

# Why a page could not be read, from its error text. Order matters: the first match wins.
_CAUSES = (
    ("time limit", r"time limit|skipped: .*asks for"),
    ("bot check", r"browser check|captcha|challenge"),
    ("rate limited", r"HTTP 429|rate limit"),
    ("forbidden", r"HTTP 40[13]\b|login required"),
    ("not found", r"HTTP 40[4]|HTTP 410"),
    ("robots.txt", r"robots\.txt"),
    ("no readable text", r"no readable text|JavaScript"),
    ("timed out", r"timed out|timeout"),
    ("server error", r"HTTP 5\d\d"),
    ("refused", r"HTTP 4\d\d"),
    ("too large", r"larger than"),
    ("unsupported", r"unsupported|binary content|no extractable text"),
    ("network", r"URLError|reset|refused|Name or service|SSL|ssl"),
)


def cause(error: str) -> str:
    for name, pattern in _CAUSES:
        if re.search(pattern, error or "", re.I):
            return name
    return "other" if error else ""


def _domain(url: str) -> str:
    host = (urlsplit(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _pct(part, whole) -> int:
    return round(100 * part / whole) if whole else 0


# ------------------------------------------------------------------ one call

def summarize(entry: dict) -> dict:
    """Numbers for one call, from its events and the metrics the tool recorded."""
    events = entry.get("events") or []
    duration = float(entry.get("duration") or 0)
    fetches = [e for e in events if e["kind"] == "fetch"]
    robots = [e for e in fetches if (e.get("url") or "").endswith("/robots.txt")]
    live = [e for e in fetches if not e.get("cached")]
    engines: dict = {}
    for e in events:
        if e["kind"] == "engine" or (e["kind"] == "timeout" and e.get("engine")):
            s = engines.setdefault(e["engine"], {"queries": 0, "results": 0, "ms": 0, "failed": 0, "timeouts": 0,
                                                 "skipped": 0})
            if e["kind"] == "timeout":
                s["timeouts"] += 1
                s["ms"] += e.get("ms") or 0
                continue
            s["queries"] += 1
            s["results"] += e.get("results") or 0
            s["ms"] += e.get("ms") or 0
            if (e.get("error") or "").startswith("skipped"):
                s["skipped"] += 1
            elif e.get("error"):
                s["failed"] += 1
    pages = [e for e in events if e["kind"] == "page"]
    page_failures = Counter(cause(e["error"]) for e in pages if e.get("error"))
    page_failures.update(cause(e.get("error")) for e in events if e["kind"] == "timeout" and e.get("url"))
    phases = {e.get("name"): e["t"] for e in events if e["kind"] == "phase"}
    slow = sorted((e for e in live if e.get("ms")), key=lambda e: -e["ms"])[:3]
    metrics = entry.get("metrics") or {}
    out = {
        "seconds": duration,
        "limit_used_pct": _pct(duration, entry.get("budget") or 0) if entry.get("budget") else None,
        "fetches": len(fetches),
        "cache_hits": len(fetches) - len(live),
        "downloaded_kb": round(sum(e.get("bytes") or 0 for e in live) / 1024),
        "robots_fetches": len([e for e in robots if not e.get("cached")]),
        "robots_seconds": round(sum(e.get("ms") or 0 for e in robots) / 1000, 1),
        "slowest_fetches": [{"url": e["url"], "seconds": round(e["ms"] / 1000, 1)} for e in slow],
        "engines": engines,
        "pages_read": sum(1 for e in pages if not e.get("error")),
        "pages_failed": sum(1 for e in pages if e.get("error")) + sum(1 for e in events
                                                                       if e["kind"] == "timeout" and e.get("url")),
        "page_failures": dict(page_failures),
        "failed_domains": sorted({_domain(e["url"]) for e in pages if e.get("error")}),
        "failed_sites": [{"domain": _domain(e["url"]), "why": cause(e["error"])} for e in pages if e.get("error")][:20],
        "rendered": sum(1 for e in events if e["kind"] == "render"),
        "archived_copies": sum(1 for e in pages if e.get("via") == "wayback"),
        "result_tokens": round((entry.get("result_chars") or 0) / 4),
    }
    if "search done" in phases:
        out["search_seconds"] = phases["search done"]
        if "reading done" in phases:
            out["read_seconds"] = round(phases["reading done"] - phases["search done"], 1)
            out["rank_seconds"] = round(max(duration - phases["reading done"], 0), 1)
    out.update(metrics)
    return out


def suggest_call(entry: dict, summary: dict | None = None) -> list:
    """What to change, judging from one call."""
    s = summary or summarize(entry)
    tips = []
    for name, e in s["engines"].items():
        if e["timeouts"]:
            tips.append(f"{name} gave no answer in time; if this keeps happening, lower its weight or drop it "
                        "from OPENRESEARCH_ENGINES")
        elif e["failed"] and e["failed"] == e["queries"]:
            tips.append(f"{name} failed on every query (see the timeline for why)")
    used = s.get("source_engines") or {}
    if used and entry.get("tool") == "research":
        idle = [n for n, e in s["engines"].items() if e["results"] and not used.get(n)]
        if idle:
            tips.append("no source came from " + ", ".join(idle) + " this time")
    if s["limit_used_pct"] and s["limit_used_pct"] >= 90:
        tips.append("the call used most of its time limit; a lower depth or fewer queries would leave headroom")
    if s["robots_seconds"] >= max(3.0, 0.2 * s["seconds"]):
        tips.append(f"{s['robots_seconds']}s went on robots.txt checks for new sites")
    blocked = s["page_failures"].get("bot check", 0) + s["page_failures"].get("forbidden", 0)
    if blocked >= 2:
        tips.append(f"{blocked} pages refused automated reading ({', '.join(s['failed_domains'][:4])}); "
                    "exclude_domains skips them up front")
    if s.get("widened"):
        tips.append("the first searches found too little and were widened; more specific queries help")
    if s.get("missing_terms"):
        tips.append("question words with no evidence: " + ", ".join(s["missing_terms"][:6]))
    if s["pages_read"] == 0 and entry.get("tool") in ("research", "fetch", "crawl"):
        tips.append("nothing was read; the answer rests on search snippets at best")
    return tips


# ------------------------------------------------------------------ many calls

def aggregate(entries: list) -> dict:
    """Totals and rates across calls (newest first or any order)."""
    entries = [e for e in entries if e.get("tool")]
    by_tool: dict = defaultdict(lambda: {"calls": 0, "ok": 0, "timeout": 0, "error": 0, "seconds": []})
    engines: dict = defaultdict(lambda: Counter())
    domains: dict = defaultdict(Counter)
    causes, totals = Counter(), Counter()
    for e in entries:
        s = e.get("summary") or summarize(e)
        t = by_tool[e["tool"]]
        t["calls"] += 1
        t[e.get("status") if e.get("status") in ("ok", "timeout", "error") else "error"] += 1
        t["seconds"].append(float(e.get("duration") or 0))
        for name, x in (s.get("engines") or {}).items():
            engines[name].update({k: v for k, v in x.items() if isinstance(v, (int, float))})
            engines[name]["calls"] += 1
        for name, n in (s.get("source_engines") or {}).items():
            engines[name]["sources"] += n
        for c, n in (s.get("page_failures") or {}).items():
            causes[c] += n
        for x in s.get("failed_sites") or [{"domain": d, "why": "other"} for d in s.get("failed_domains") or []]:
            domains[x["domain"]][x["why"]] += 1
        totals.update(fetches=s.get("fetches", 0), cache_hits=s.get("cache_hits", 0),
                      pages_read=s.get("pages_read", 0), pages_failed=s.get("pages_failed", 0),
                      robots_ms=int((s.get("robots_seconds") or 0) * 1000), widened=int(bool(s.get("widened"))),
                      near_limit=int((s.get("limit_used_pct") or 0) >= 90), cold=int(bool(e.get("cold_start"))),
                      tavily_credits=s.get("tavily_credits", 0))
    tools = {}
    for name, t in by_tool.items():
        secs = sorted(t.pop("seconds"))
        t["avg_seconds"] = round(sum(secs) / len(secs), 1) if secs else 0
        t["p90_seconds"] = secs[min(len(secs) - 1, int(0.9 * len(secs)))] if secs else 0
        tools[name] = t
    eng = {}
    for name, c in engines.items():
        q = c["queries"] or 0
        eng[name] = {"queries": q, "timeouts": c["timeouts"], "failed": c["failed"], "skipped": c["skipped"],
                     "answered_pct": _pct(q - c["failed"] - c["skipped"], q + c["timeouts"]),
                     "avg_seconds": round(c["ms"] / 1000 / max(q + c["timeouts"], 1), 1),
                     "results": c["results"], "sources": c["sources"]}
    return {"calls": len(entries), "tools": tools, "engines": eng, "page_failures": dict(causes.most_common()),
            "worst_domains": [{"domain": d, "failures": sum(c.values()), "why": c.most_common(1)[0][0]}
                              for d, c in sorted(domains.items(), key=lambda kv: -sum(kv[1].values()))[:10]],
            "cache_hit_pct": _pct(totals["cache_hits"], totals["fetches"]),
            "pages_read": totals["pages_read"], "pages_failed": totals["pages_failed"],
            "page_success_pct": _pct(totals["pages_read"], totals["pages_read"] + totals["pages_failed"]),
            "robots_seconds": round(totals["robots_ms"] / 1000, 1), "widened_searches": totals["widened"],
            "near_time_limit": totals["near_limit"], "cold_starts": totals["cold"],
            "tavily_credits": totals["tavily_credits"]}


def suggest(stats: dict) -> list:
    """What to change, judging from many calls."""
    tips = []
    n = stats["calls"]
    if not n:
        return tips
    for name, e in stats["engines"].items():
        asked = e["queries"] + e["timeouts"]
        if asked >= 3 and e["answered_pct"] < 60:
            tips.append(f"{name} answered only {e['answered_pct']}% of {asked} queries: consider removing it "
                        "from OPENRESEARCH_ENGINES or adding a keyed engine (Tavily, Brave)")
        elif asked >= 3 and e["avg_seconds"] >= 6:
            tips.append(f"{name} averages {e['avg_seconds']}s per query; it sets the pace of every search")
        research_calls = stats["tools"].get("research", {}).get("calls", 0)
        if research_calls >= 3 and e["results"] >= 20 and e["sources"] == 0:
            tips.append(f"{name} returned {e['results']} results but none became a research source")
    if stats["page_success_pct"] < 70 and stats["pages_read"] + stats["pages_failed"] >= 10:
        top = ", ".join(f"{c} ({k})" for c, k in list(stats["page_failures"].items())[:3])
        tips.append(f"only {stats['page_success_pct']}% of pages could be read; main causes: {top}")
    for d in stats["worst_domains"][:5]:
        if d["failures"] >= 3:
            tips.append(f"{d['domain']} failed {d['failures']} times ({d['why']}): add it to "
                        "OPENRESEARCH_EXCLUDE_DOMAINS")
    for name, t in stats["tools"].items():
        if t["calls"] >= 3 and t["timeout"] + t["error"] >= max(1, t["calls"] // 5):
            tips.append(f"{name}: {t['timeout']} timed out and {t['error']} failed out of {t['calls']} calls")
        if t["calls"] >= 3 and t["p90_seconds"] >= 40:
            tips.append(f"{name}: 1 in 10 calls takes {t['p90_seconds']:.0f}s or more; clients may give up first")
    if stats["near_time_limit"] >= max(2, n // 5):
        tips.append(f"{stats['near_time_limit']} calls ran close to the time limit")
    if stats["cold_starts"] >= max(2, n // 4):
        tips.append(f"{stats['cold_starts']} calls woke the server from sleep (slow first answer); an always-on "
                    "plan removes that")
    if stats["widened_searches"] >= max(2, n // 4):
        tips.append(f"{stats['widened_searches']} research calls found too little at first and had to widen "
                    "their searches")
    return tips


# ------------------------------------------------------------------ text

def format_summary(s: dict) -> list:
    """Summary lines for one call's timeline view."""
    lines = []
    if "search_seconds" in s:
        lines.append(f"Time: search {s['search_seconds']}s, reading {s.get('read_seconds', '?')}s, "
                     f"ranking {s.get('rank_seconds', '?')}s" + (f" ({s['limit_used_pct']}% of the limit)"
                                                                   if s.get("limit_used_pct") is not None else ""))
    elif s.get("limit_used_pct") is not None:
        lines.append(f"Time: {s['seconds']}s ({s['limit_used_pct']}% of the limit)")
    for name, e in (s.get("engines") or {}).items():
        state = (f"{e['timeouts']} timed out" if e["timeouts"] else "") or (f"{e['failed']} failed" if e["failed"] else "")
        used = (s.get("source_engines") or {}).get(name)
        lines.append(f"Engine {name}: {e['results']} results from {e['queries']} queries, "
                     f"{e['ms'] / 1000 / max(e['queries'] + e['timeouts'], 1):.1f}s each"
                     + (f", {state}" if state else "") + (f", {used} source(s)" if used else ""))
    if s.get("pages_read") or s.get("pages_failed"):
        why = ", ".join(f"{k} {v}" for k, v in (s.get("page_failures") or {}).items())
        lines.append(f"Pages: {s['pages_read']} read, {s['pages_failed']} failed" + (f" ({why})" if why else ""))
    if s.get("picked") is not None:
        lines.append(f"Research: {s.get('queries', 0)} queries" + (" (widened)" if s.get("widened") else "")
                     + f", {s.get('candidates', 0)} results, {s['picked']} picked, {s.get('backfilled', 0)} backfilled, "
                     f"{s.get('sources', 0)} sources, {s.get('passages', 0)} passages, "
                     f"{s.get('result_tokens', 0)}/{s.get('token_budget', '?')} tokens")
    lines.append(f"Network: {s['fetches']} requests, {s['cache_hits']} from cache, {s['downloaded_kb']} KB; "
                 f"robots.txt {s['robots_fetches']} ({s['robots_seconds']}s)")
    if s.get("slowest_fetches"):
        lines.append("Slowest: " + "; ".join(f"{x['seconds']}s {x['url'][:80]}" for x in s["slowest_fetches"]))
    return lines


def format_stats(stats: dict, tips: list, source: str = "") -> str:
    if not stats["calls"]:
        return "No calls to analyse yet."
    lines = [f"Statistics over {stats['calls']} call(s){source}", "", "Tools:"]
    for name, t in sorted(stats["tools"].items(), key=lambda kv: -kv[1]["calls"]):
        lines.append(f"  {name:<9} {t['calls']:>4} calls  ok {t['ok']}, timeout {t['timeout']}, error {t['error']}  "
                     f"avg {t['avg_seconds']}s, p90 {t['p90_seconds']:.1f}s")
    if stats["engines"]:
        lines += ["", "Search engines:"]
        for name, e in sorted(stats["engines"].items(), key=lambda kv: -kv[1]["queries"]):
            lines.append(f"  {name:<13} answered {e['answered_pct']:>3}% of {e['queries'] + e['timeouts']} queries, "
                         f"{e['avg_seconds']}s avg, {e['results']} results, {e['sources']} became sources")
    lines += ["", f"Pages: {stats['pages_read']} read, {stats['pages_failed']} failed "
                  f"({stats['page_success_pct']}% success); cache hits {stats['cache_hit_pct']}% of requests; "
                  f"robots.txt checks {stats['robots_seconds']}s in total"]
    if stats["page_failures"]:
        lines.append("Why pages failed: " + ", ".join(f"{k} {v}" for k, v in stats["page_failures"].items()))
    if stats["worst_domains"]:
        lines.append("Most failing sites: " + ", ".join(f"{d['domain']} ({d['failures']}, {d['why']})"
                                                       for d in stats["worst_domains"][:6]))
    extra = [f"{stats['cold_starts']} cold start(s)" if stats["cold_starts"] else "",
             f"{stats['near_time_limit']} near the time limit" if stats["near_time_limit"] else "",
             f"{stats['widened_searches']} widened search(es)" if stats["widened_searches"] else "",
             f"{stats['tavily_credits']} Tavily credit(s)" if stats["tavily_credits"] else ""]
    if any(extra):
        lines.append("Also: " + ", ".join(x for x in extra if x))
    lines += ["", "Suggestions:"] + ([f"  - {t}" for t in tips] or ["  - nothing stands out"])
    return "\n".join(lines)


# ------------------------------------------------------------------ the GitHub copies

_JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.S)


def load_dir(path: str, limit: int = 5000) -> list:
    """Call entries from a folder of log files written by logship.py (newest first)."""
    files = []
    for root, _dirs, names in os.walk(path):
        files += [os.path.join(root, n) for n in names if n.endswith(".md") and n[:1].isdigit()]
    entries = []
    for p in sorted(files, reverse=True):
        try:
            with open(p, encoding="utf-8") as fh:
                m = _JSON_BLOCK.search(fh.read())
            batch = json.loads(m.group(1).replace("'''", "```")) if m else []
        except (OSError, ValueError):
            continue
        entries += [e for e in (batch if isinstance(batch, list) else [batch]) if isinstance(e, dict)]
        if len(entries) >= limit:
            break
    entries.sort(key=lambda e: -float(e.get("started") or 0))
    return entries[:limit]
