"""Site mapping and crawling. Crawls can be steered by a question (best-first)."""
from __future__ import annotations

import fnmatch
import hashlib
import heapq
import itertools
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from .fetch import Fetcher, normalize_url
from .page import Page, domain_of, read
from .rank import tokens

_SKIP_EXT = re.compile(
    r"\.(?:jpe?g|png|gif|webp|svg|ico|bmp|tiff?|css|js|mjs|map|woff2?|ttf|eot|otf|mp[34]|m4[av]|mov|avi|"
    r"wmv|webm|ogg|wav|flac|zip|rar|7z|tar|gz|bz2|exe|dmg|iso|apk|bin|docx?|xlsx?|pptx?)(?:$|\?)", re.I)


def _same_site(url: str, root: str, subdomains: bool) -> bool:
    d = domain_of(url)
    return d == root or (subdomains and d.endswith("." + root))


def _match(url: str, patterns) -> bool:
    """Patterns are shell-style globs matched against the path (or the full URL if they contain '://')."""
    path = urlsplit(url).path or "/"
    return any(fnmatch.fnmatch(url if "://" in p else path, p) for p in patterns)


def _relevance(query_tokens: set, url: str, anchor: str) -> float:
    if not query_tokens:
        return 0.0
    have = set(tokens(re.sub(r"[/_\-.]+", " ", urlsplit(url).path) + " " + anchor))
    return len(query_tokens & have) / len(query_tokens)


def sitemap_urls(f: Fetcher, origin: str, limit: int = 5000) -> list:
    """(url, lastmod) pairs from robots.txt sitemaps and /sitemap.xml, following sitemap indexes."""
    queue = list(f.sitemaps(origin)) or [origin + "/sitemap.xml"]
    seen, out = set(), []
    while queue and len(out) < limit and len(seen) < 60:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        r = f.fetch(sm, robots=False, max_age=3600, timeout=15)
        if not r.ok:
            continue
        try:
            root = ET.fromstring(r.body)
        except ET.ParseError:
            continue
        index = root.tag.rsplit("}", 1)[-1].lower() == "sitemapindex"
        for el in root:
            loc = lastmod = ""
            for ch in el:
                name = ch.tag.rsplit("}", 1)[-1].lower()
                if name == "loc":
                    loc = (ch.text or "").strip()
                elif name == "lastmod":
                    lastmod = (ch.text or "").strip()[:10]
            if not loc:
                continue
            if index:
                queue.append(loc)
            else:
                out.append((loc, lastmod))
                if len(out) >= limit:
                    break
    return out


def map_site(f: Fetcher, url: str, limit: int = 200, search: str = "", subdomains: bool = False) -> list:
    """Discover a site's URLs quickly (sitemaps + links on the start page) without reading pages.
    With `search`, URLs are ordered by how well their path and link text match it.
    Returns dicts: {url, lastmod, text}."""
    url = normalize_url(url)
    s = urlsplit(url)
    origin, root = f"{s.scheme}://{s.netloc}", domain_of(url)
    found = {}
    for loc, lastmod in sitemap_urls(f, origin):
        if _same_site(loc, root, subdomains):
            found.setdefault(loc, {"url": loc, "lastmod": lastmod, "text": ""})
    page = read(f, url, max_tokens=200, mode="full", render="never", fallback=False)
    for link, text in page.links:
        if _same_site(link, root, subdomains) and not _SKIP_EXT.search(link):
            found.setdefault(link, {"url": link, "lastmod": "", "text": text})["text"] = text
    items = list(found.values())
    if search:
        q = set(tokens(search))
        items.sort(key=lambda it: -_relevance(q, it["url"], it["text"]))
    return items[:limit]


def crawl(f: Fetcher, url: str, *, max_pages: int = 25, max_depth: int = 3, query: str = "",
          include=None, exclude=None, subdomains: bool = False, use_sitemap: bool = False,
          workers: int = 4, delay: float = 0.5, page_tokens: int = 1500, on_page=None, **read_kw) -> list:
    """Crawl a site from `url`, staying on its domain.

    query        best-first: follow the links most related to the question first, and focus each
                 page's content on it. Without a query the crawl is breadth-first.
    include/exclude  glob patterns on the URL path, e.g. ['/docs/*'] or ['*/tag/*'].
    on_page      optional callback(Page) as each page arrives (used by the CLI to write files).
    Returns the Pages read (failed ones included, with .error set).
    """
    start = normalize_url(url)
    root = domain_of(start)
    qtok = set(tokens(query))
    counter = itertools.count()
    heap, seen, hashes, pages = [], {start}, set(), []

    def push(u, depth, anchor):
        prio = -_relevance(qtok, u, anchor) if qtok else 0.0
        heapq.heappush(heap, (depth if not qtok else 0, prio, next(counter), u, depth))

    push(start, 0, "")
    if use_sitemap:
        s = urlsplit(start)
        for loc, _ in sitemap_urls(f, f"{s.scheme}://{s.netloc}", limit=max_pages * 20):
            if loc not in seen and _same_site(loc, root, subdomains) and not _SKIP_EXT.search(loc) \
                    and (not include or _match(loc, include)) and not (exclude and _match(loc, exclude)):
                seen.add(loc)
                push(loc, 1, "")

    def job(item):
        return item, read(f, item[3], query=query, max_tokens=page_tokens, delay=delay, **read_kw)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while heap and len(pages) < max_pages:
            batch = [heapq.heappop(heap) for _ in range(min(workers, len(heap), max_pages - len(pages)))]
            for item, page in pool.map(job, batch):
                depth = item[4]
                if page.ok:
                    digest = hashlib.sha1(page.markdown.encode("utf-8", "replace")).hexdigest()
                    if digest in hashes:              # same content under another URL
                        continue
                    hashes.add(digest)
                pages.append(page)
                if on_page:
                    on_page(page)
                if not page.ok or depth >= max_depth:
                    continue
                for link, anchor in page.links:
                    if link in seen or _SKIP_EXT.search(link) or not _same_site(link, root, subdomains):
                        continue
                    if include and not _match(link, include):
                        continue
                    if exclude and _match(link, exclude):
                        continue
                    seen.add(link)
                    push(link, depth + 1, anchor)
    return pages


def format_crawl(pages: list, max_tokens: int = 12000) -> str:
    ok = [p for p in pages if p.ok]
    bad = [p for p in pages if not p.ok]
    out = [f"Crawled {len(pages)} pages ({len(ok)} read, {len(bad)} failed)."]
    budget = max_tokens * 4
    for i, p in enumerate(ok, 1):
        block = f"\n## [{i}] {p.title or p.url}\n{p.final_url or p.url}" + (
            f" | {p.published}" if p.published else "") + f"\n\n{p.content}"
        if len(block) > budget:
            rest = [q.final_url or q.url for q in ok[i - 1:]]
            out.append(f"\n[Token budget reached. {len(rest)} more pages were read but not shown; "
                       "fetch them individually:]\n" + "\n".join(f"- {u}" for u in rest[:60]))
            break
        budget -= len(block)
        out.append(block)
    if bad:
        out.append("\nFailed:\n" + "\n".join(f"- {p.url} - {p.error}" for p in bad[:30]))
    return "\n".join(out)
