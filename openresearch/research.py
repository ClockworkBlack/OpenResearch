"""One-call research: search several ways, read the best pages in parallel, and hand back only
the passages that bear on the question, each tied to a numbered source.

There is no LLM in here on purpose. The model calling this tool does the thinking; this does
the legwork and keeps the token bill small."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from .fetch import Fetcher
from .page import read_many
from .rank import chunk_markdown, dedupe, est_tokens, rank_chunks, tokens
from .search import canonical, search

DEPTH = {
    "quick": {"pages": 5, "tokens": 3000, "per_source": 2, "results": 8},
    "standard": {"pages": 9, "tokens": 6000, "per_source": 3, "results": 10},
    "deep": {"pages": 18, "tokens": 12000, "per_source": 4, "results": 15},
}
_OFFICIAL = re.compile(r"\.(gov|mil|edu)(\.[a-z]{2})?$|\.gov\.[a-z]{2}$|\.int$")


def _auto_queries(question: str) -> list:
    """A keyword-only variant catches pages that a long natural-language query misses."""
    kw = re.findall(r"[\w'-]+", question)
    stop = {"what", "which", "who", "whom", "when", "where", "why", "how", "is", "are", "was", "were", "do",
            "does", "did", "the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "it", "that", "this",
            "be", "can", "should", "would", "could", "with", "about", "there", "their", "has", "have", "i", "me"}
    short = " ".join(w for w in kw if w.lower() not in stop)
    out = [question]
    if short and short.lower() != question.lower() and len(short.split()) >= 2:
        out.append(short)
    return out


def research(f: Fetcher, question: str, *, queries=None, depth: str = "standard", max_tokens: int | None = None,
             max_pages: int | None = None, vertical: str = "web", include_domains=None, exclude_domains=None,
             time: str = "", urls=None, max_age: int | None = None) -> dict:
    """Returns {'text': evidence pack in Markdown, 'sources': [...], 'unread': [...], 'report': {...}}.

    queries  search queries to run (the caller usually writes better ones than the default)
    urls     pages to read in addition to (or, with queries=[], instead of) search results
    """
    d = DEPTH.get(depth, DEPTH["standard"])
    budget = max_tokens or d["tokens"]
    n_pages = max_pages or d["pages"]
    qs = [q for q in (queries if queries is not None else _auto_queries(question)) if q.strip()][:8]

    fused, report, snippets = {}, {}, {}
    if qs:
        with ThreadPoolExecutor(max_workers=len(qs)) as pool:
            runs = list(pool.map(lambda q: search(f, q, d["results"], vertical=vertical, time=time,
                                                  include_domains=include_domains,
                                                  exclude_domains=exclude_domains, per_domain=2), qs))
        for results, rep in runs:
            for k, v in rep.items():
                old = report.get(k)
                if isinstance(v, int):
                    report[k] = v + (old if isinstance(old, int) else 0)
                elif not isinstance(old, int):
                    report[k] = v
            for rank, r in enumerate(results):
                key = canonical(r.url)
                fused.setdefault(key, [r, 0.0])[1] += 1.0 / (30 + rank)
                snippets[key] = r
    ranked = [r for r, _ in sorted(fused.values(), key=lambda x: -x[1])]
    given = list(dict.fromkeys(u for u in (urls or []) if u and u.strip()))
    per, picks, seen = {}, list(given), {canonical(u) for u in given}
    queue = [r for r in ranked if canonical(r.url) not in seen]

    def take(count: int) -> list:
        """Next `count` results to read, at most two per domain."""
        batch = []
        for r in list(queue):
            if len(batch) >= count:
                break
            if per.get(r.domain, 0) >= 2:
                continue
            per[r.domain] = per.get(r.domain, 0) + 1
            queue.remove(r)
            batch.append(r.url)
        return batch

    picks += take(n_pages)
    reader = dict(workers=8, max_tokens=100000, timeout=15, max_age=max_age, retries=1)
    pages = read_many(f, picks, **reader)
    for _ in range(2):                                # a blocked or dead page gives its slot to the next result
        short = sum(1 for p in pages if not p.ok)
        extra = take(min(short, n_pages)) if short else []
        if not extra:
            break
        n_before = len(pages)
        pages += read_many(f, extra, **reader)
        if all(not p.ok for p in pages[n_before:]):
            break
        if sum(1 for p in pages if p.ok) >= len(picks):
            break
    read_urls = {canonical(u) for p in pages for u in (p.url, p.final_url) if u}
    unread = [r for r in ranked if canonical(r.url) not in read_urls]
    good = [p for p in pages if p.ok]
    failed = [p for p in pages if not p.ok]

    chunks = []
    for i, p in enumerate(good):
        chunks.extend(chunk_markdown(p.markdown, target=900, doc=i))
    scoring_query = question + " " + " ".join(q for q in qs if q != question)
    ranked_chunks = dedupe([c for c in rank_chunks(chunks, scoring_query) if c.score > 0][:200])

    taken, per_doc, used = [], {}, 0
    for c in ranked_chunks:
        if per_doc.get(c.doc, 0) >= d["per_source"]:
            continue
        cost = est_tokens(c.text) + 25
        if used + cost > budget * 0.85 and taken:
            continue
        per_doc[c.doc] = per_doc.get(c.doc, 0) + 1
        taken.append(c)
        used += cost

    order = []                                        # sources numbered by their best passage
    for c in taken:
        if c.doc not in order:
            order.append(c.doc)
    silent = [i for i in range(len(good)) if i not in order]

    lines = [f"# Research: {question}"]
    eng = "; ".join(f"{k} {v}" if isinstance(v, int) else f"{k} FAILED ({v})" for k, v in report.items())
    lines.append(f"Queries: {' | '.join(qs) if qs else '(none)'}\n"
                 f"Search: {len(ranked)} unique results ({eng or 'no search'}). "
                 f"Read {len(good)} pages, {len(failed)} failed.")
    if not good:
        lines.append("\nNothing could be read. Check the engine errors above; try other queries or pass urls directly.")
    sources = []
    if order:
        lines.append("\n## Sources")
    for num, doc in enumerate(order, 1):
        p = good[doc]
        tag = " [official]" if _OFFICIAL.search(p.domain) else ""
        via = f" [archived copy {p.snapshot[:8]}]" if p.via == "wayback" else ""
        hit = snippets.get(canonical(p.url))
        when = p.published or (f"{hit.date} (date from search listing)" if hit is not None and hit.date else "")
        lines.append(f"[{num}] {p.title or p.url} - {p.domain}{tag}"
                     + (f" - {when}" if when else " - undated") + via + f"\n    {p.final_url or p.url}")
        sources.append({"n": num, "title": p.title, "url": p.final_url or p.url, "domain": p.domain,
                        "published": p.published or (hit.date if hit is not None else ""), "via": p.via,
                        "tokens": est_tokens(p.markdown)})
    if taken:
        lines.append("\n## Evidence (most relevant passages; quote sources by [number])")
    for num, doc in enumerate(order, 1):
        for c in sorted((c for c in taken if c.doc == doc), key=lambda c: c.index):
            head = f" ({c.heading})" if c.heading and not c.text.startswith("#") else ""
            lines.append(f"\n### [{num}]{head}\n{c.text}")
    words = {}                                        # stem -> the word as the caller wrote it
    for w in re.findall(r"\w+", question):
        for t in tokens(w):
            words.setdefault(t, w.lower())
    covered = set()
    for c in taken:
        covered |= words.keys() & set(tokens(c.text))
    missing = sorted(words[t] for t in words.keys() - covered)
    notes = []
    if missing and taken:
        notes.append("Question terms not found in any passage: " + ", ".join(missing))
    if silent:
        notes.append("Read but nothing relevant: " + ", ".join(good[i].domain for i in silent[:8]))
    if failed:
        def why(p):
            hit = snippets.get(canonical(p.url))
            hint = f' - search snippet: "{hit.snippet[:200]}"' if hit is not None and hit.snippet else ""
            return f"- {p.url} ({p.error}){hint}"
        notes.append("Could not read:\n" + "\n".join(why(p) for p in failed[:8]))
    if unread:
        notes.append("Not read (lower ranked):\n" + "\n".join(
            f"- [{r.title}]({r.url})" + (f" - {r.snippet[:140]}" if r.snippet else "") for r in unread[:8]))
    if notes:
        lines.append("\n## Gaps and leads\n" + "\n".join(notes))
    return {"text": "\n".join(lines), "sources": sources, "report": report,
            "unread": [{"title": r.title, "url": r.url} for r in unread],
            "failed": [{"url": p.url, "error": p.error} for p in failed]}
