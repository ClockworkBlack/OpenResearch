"""RSS, Atom and JSON Feed: find a site's feeds, read them, and merge their items newest first.

`parse_feed` reads RSS 2.0, RSS 1.0 (RDF), Atom and JSON Feed into one shape. `discover` finds the
feeds a site advertises (or keeps at the usual addresses). `read_feeds` puts it together for the
`feeds` tool: several sites or feeds at once, filtered by topic and age, optionally reading the
newest articles themselves.
"""
from __future__ import annotations

import html as _html
import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from . import trace
from .fetch import Fetcher
from .rank import tokens

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/rdf+xml")
# Where sites keep feeds when their pages do not say: WordPress, Ghost, Hugo, Jekyll, Blogger, Substack...
COMMON_PATHS = ("/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml", "/feed/",
                "/feeds/posts/default", "/blog/feed", "/blog/rss.xml", "/news/rss.xml")
_FEED_TTL = 600                       # feeds change often: re-read after ten minutes


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(s: str) -> str:
    """HTML or escaped HTML -> plain text on one line."""
    s = _html.unescape(re.sub(r"<[^>]+>", " ", _html.unescape(s or "")))
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s: str):
    """A feed date (RFC 822 or ISO 8601, the two that feeds use) -> aware datetime in UTC, or None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        iso = re.sub(r"Z$", "+00:00", s)
        iso = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", iso)
        try:
            d = datetime.fromisoformat(iso)
        except ValueError:
            from .extract import norm_date      # "Mon, 02 Mar 2026", "2026/03/02", "March 2, 2026"...
            day = norm_date(s)
            if not day:
                return None
            d = datetime(*map(int, day.split("-")))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


_ROOT = re.compile(rb"<(rss|feed|rdf:rdf)[\s>]")


def is_feed(body: bytes, content_type: str = "") -> bool:
    """Judged by the content, not only the declared type: many feeds are served as text/xml, or with
    no XML declaration, or after a stylesheet line."""
    head = body[:2000].lstrip().lower()
    if any(t in content_type for t in ("rss", "atom", "feed+json", "rdf")):
        return True
    if b"jsonfeed.org" in head:
        return True
    m = _ROOT.search(head)
    return bool(m) and b"<html" not in head[:m.start()]


def parse_feed(body, base_url: str = "") -> dict:
    """{'title', 'link', 'kind', 'items': [{title, link, date (datetime|None), author, summary, tags}]}.
    Raises ValueError when it is not a feed."""
    raw = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
    stripped = raw.lstrip()
    if stripped[:1] == b"{":
        return _parse_json_feed(raw, base_url)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ValueError(f"not a feed (XML error: {e})") from None
    kind = _local(root.tag)
    if kind not in ("rss", "feed", "rdf"):
        raise ValueError(f"not a feed (root element <{kind}>)")
    out = {"title": "", "link": "", "kind": {"rss": "RSS", "feed": "Atom", "rdf": "RSS 1.0"}[kind], "items": []}
    channel = next((c for c in root if _local(c.tag) == "channel"), root)
    for ch in channel:
        name = _local(ch.tag)
        if name == "title" and not out["title"]:
            out["title"] = _text(ch.text)
        elif name == "link" and not out["link"]:
            out["link"] = ch.get("href") if ch.get("rel", "alternate") == "alternate" and ch.get("href") else (ch.text or "").strip()
    entries = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    for e in entries:
        item = {"title": "", "link": "", "date": None, "author": "", "summary": "", "tags": []}
        summary = content = ""
        for ch in e:
            name, txt = _local(ch.tag), (ch.text or "")
            if name == "title":
                item["title"] = _text(txt)
            elif name == "link":
                rel = ch.get("rel", "alternate")
                href = ch.get("href") or txt.strip()
                if href and rel == "alternate" and not item["link"]:
                    item["link"] = href
            elif name == "guid" and not item["link"] and ch.get("isPermaLink", "true") != "false" \
                    and txt.strip().startswith("http"):
                item["link"] = txt.strip()
            elif name in ("pubdate", "published", "date", "issued", "created") and item["date"] is None:
                item["date"] = parse_date(txt)
            elif name in ("updated", "modified") and item["date"] is None:
                item["date"] = parse_date(txt)
            elif name in ("author", "creator"):
                who = _text(txt) or _text(next((x.text for x in ch if _local(x.tag) == "name"), ""))
                item["author"] = item["author"] or who
            elif name in ("description", "summary"):
                summary = summary or txt
            elif name in ("encoded", "content"):
                content = content or txt
            elif name in ("category", "subject"):
                tag = ch.get("term") or _text(txt)
                if tag:
                    item["tags"].append(tag)
        item["summary"] = _text(summary or content)[:600]
        if item["link"]:
            item["link"] = urljoin(base_url, item["link"])
        if item["title"] or item["link"]:
            out["items"].append(item)
    return out


def _parse_json_feed(raw: bytes, base_url: str) -> dict:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise ValueError("not a feed (bad JSON)") from None
    if not isinstance(data, dict) or "jsonfeed.org" not in str(data.get("version", "")):
        raise ValueError("not a feed (JSON without a JSON Feed version)")
    out = {"title": _text(data.get("title")), "link": data.get("home_page_url") or "", "kind": "JSON Feed",
           "items": []}
    for e in data.get("items") or []:
        if not isinstance(e, dict):
            continue
        authors = e.get("authors") or ([e["author"]] if isinstance(e.get("author"), dict) else [])
        out["items"].append({
            "title": _text(e.get("title")), "link": urljoin(base_url, e.get("url") or e.get("external_url") or ""),
            "date": parse_date(e.get("date_published") or e.get("date_modified") or ""),
            "author": next((a.get("name", "") for a in authors if isinstance(a, dict)), ""),
            "summary": _text(e.get("summary") or e.get("content_text") or e.get("content_html") or "")[:600],
            "tags": [t for t in e.get("tags") or [] if isinstance(t, str)]})
    return out


def advertised(html: str, base_url: str) -> list:
    """Feed URLs a page names in <link rel="alternate" type="application/rss+xml" ...>."""
    from .dom import parse
    found = []
    for ln in parse(html).find_all("link"):
        rel = ln.get("rel").lower().split()
        if "alternate" in rel and ln.get("type").lower().split(";")[0].strip() in FEED_TYPES and ln.get("href"):
            found.append(urljoin(base_url, ln.get("href")))
    return list(dict.fromkeys(found))


def discover(f: Fetcher, url: str, guess: bool = True) -> tuple:
    """Find the feeds for a URL. Returns (feed urls, note). A feed URL is returned as it is."""
    r = f.fetch(url, max_age=_FEED_TTL)
    if not r.ok:
        return [], f"could not open {url}: {r.error}"
    if is_feed(r.body, r.content_type):
        return [r.final_url or url], ""
    found = advertised(r.text, r.final_url or url)
    if found or not guess:
        return found, "" if found else "the page names no feed"
    s = urlsplit(r.final_url or url)
    origin = f"{s.scheme}://{s.netloc}"
    for path in COMMON_PATHS:                  # the page names none: try where sites usually keep them
        if trace.remaining(99) < 5:
            break
        g = f.fetch(origin + path, max_age=_FEED_TTL, timeout=6, retries=0)
        if g.ok and is_feed(g.body, g.content_type):
            return [g.final_url or origin + path], f"found at {path} (the page names no feed)"
    return [], "no feed found: the page names none and none sits at the usual addresses"


def _matches(item: dict, terms: set) -> float:
    if not terms:
        return 1.0
    have = set(tokens(f"{item['title']} {item['summary']} {' '.join(item['tags'])}"))
    return len(terms & have) / len(terms)


def read_feeds(f: Fetcher, urls: list, *, query: str = "", days: float = 0, limit: int = 30, read: int = 0,
               per_feed: int = 0, max_tokens: int = 6000) -> str:
    """The `feeds` tool: items from the feeds of these sites or feed URLs, newest first, as Markdown."""
    from .page import read_many
    urls = list(dict.fromkeys(u for u in urls if u and u.strip()))[:20]
    if not urls:
        raise ValueError("feeds needs at least one site or feed URL in `urls`")
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        found = list(pool.map(trace.bind(lambda u: discover(f, u)), urls))
    feed_urls, notes = [], []
    for u, (fs, note) in zip(urls, found):
        feed_urls += fs[:2]                    # a site's first two feeds (often posts and comments: enough)
        if note and not fs:
            notes.append(f"- {u}: {note}")
    feed_urls = list(dict.fromkeys(feed_urls))

    def load(u):
        r = f.fetch(u, max_age=_FEED_TTL)
        if not r.ok:
            return u, None, r.error
        try:
            return u, parse_feed(r.body, r.final_url or u), ""
        except ValueError as e:
            return u, None, str(e)

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(feed_urls)))) as pool:
        loaded = list(pool.map(trace.bind(load), feed_urls))
    terms = set(tokens(query))
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400 if days else None
    items, sources, seen = [], [], set()
    for u, feed, err in loaded:
        if feed is None:
            notes.append(f"- {u}: {err}")
            continue
        kept = 0
        for it in feed["items"]:
            key = it["link"] or it["title"]
            if key in seen:
                continue
            if cutoff and it["date"] is not None and it["date"].timestamp() < cutoff:
                continue
            if terms and _matches(it, terms) < (0.5 if len(terms) > 1 else 1.0):
                continue
            if per_feed and kept >= per_feed:
                break
            seen.add(key)
            it["feed"] = feed["title"] or urlsplit(u).netloc
            items.append(it)
            kept += 1
        sources.append(f"- {feed['title'] or u} ({feed['kind']}, {len(feed['items'])} items): {u}")
    items.sort(key=lambda it: it["date"].timestamp() if it["date"] else 0, reverse=True)
    items = items[:max(1, min(limit, 200))]
    trace.note(feeds=len(feed_urls), feed_items=len(items), feeds_failed=len(notes))

    lines = [f"# Feeds: {len(items)} item(s) from {len(sources)} feed(s)"
             + (f" matching \"{query}\"" if query else "") + (f", last {days:g} day(s)" if days else "")]
    if sources:
        lines.append("\nFeeds read:\n" + "\n".join(sources))
    if notes:
        lines.append("\nProblems:\n" + "\n".join(notes))
    if items:
        lines.append("\n## Items (newest first)")
    budget = max_tokens * 4
    for i, it in enumerate(items, 1):
        when = it["date"].strftime("%Y-%m-%d %H:%M UTC") if it["date"] else "undated"
        block = (f"\n{i}. [{it['title'] or it['link']}]({it['link']})\n   {when} | {it['feed']}"
                 + (f" | {it['author']}" if it["author"] else "")
                 + (f"\n   {it['summary'][:300]}" if it["summary"] else ""))
        if len(block) > budget:
            lines.append(f"\n[{len(items) - i + 1} more item(s) left out: token budget reached]")
            break
        budget -= len(block)
        lines.append(block)
    if read and items:
        to_read = [it["link"] for it in items[:min(read, 5)] if it["link"]]
        pages = read_many(f, to_read, query=query, max_tokens=max(300, budget // 4 // max(len(to_read), 1)))
        lines.append("\n## Articles")
        for p in pages:
            lines.append("\n" + p.render())
    elif not items and not notes:
        lines.append("\nNo items" + (" matched" if query or days else "") + ".")
    return "\n".join(lines)


def feed_markdown(text: str, base_url: str = "") -> tuple:
    """A feed as Markdown for `fetch`: (markdown, title), or ('', '') when it is not a feed."""
    try:
        feed = parse_feed(text.encode("utf-8"), base_url)
    except ValueError:
        return "", ""
    lines = [f"# {feed['title'] or 'Feed'}", ""]
    for it in feed["items"]:
        when = it["date"].strftime("%a, %d %b %Y") if it["date"] else ""
        lines.append(f"- [{it['title'] or it['link']}]({it['link']})" + (f" - {when}" if when else "")
                     + (f"\n  {it['summary'][:300]}" if it["summary"] else ""))
    return "\n".join(lines), feed["title"]

