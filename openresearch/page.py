"""Read one URL into clean Markdown: HTML, PDF, JSON, feeds and plain text, with fallbacks."""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from .extract import extract_html
from .fetch import Fetcher, Response
from .rank import est_tokens, focus, truncate

_BLOCKED = frozenset((401, 403, 404, 406, 410, 429, 451, 500, 502, 503, 504))
_JS_HINT = re.compile(r'id=["\'](?:root|app|__next|__nuxt)["\']|ng-app|enable javascript|requires javascript|'
                      r'__NEXT_DATA__|data-reactroot', re.I)


@dataclass
class Page:
    url: str
    final_url: str = ""
    status: int = 0
    error: str = ""
    title: str = ""
    description: str = ""
    author: str = ""
    published: str = ""
    modified: str = ""
    site: str = ""
    lang: str = ""
    content_type: str = ""
    via: str = "live"
    snapshot: str = ""           # Wayback timestamp when via == 'wayback'
    markdown: str = ""           # full extracted document
    content: str = ""            # what to show: focused / truncated slice of markdown
    next_offset: int | None = None
    matched: bool = True         # False if a focus query matched nothing
    links: list = field(default_factory=list)
    fetched_at: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.markdown)

    @property
    def domain(self) -> str:
        return domain_of(self.final_url or self.url)

    def header(self) -> str:
        bits = [f"# {self.title or self.url}", f"URL: {self.final_url or self.url}"]
        meta = []
        if self.published:
            meta.append(f"published {self.published}")
        if self.modified and self.modified != self.published:
            meta.append(f"modified {self.modified}")
        if self.author:
            meta.append(f"by {self.author}")
        if self.via == "wayback":
            meta.append(f"Internet Archive copy from {self.snapshot[:8]} (live page unavailable)")
        elif self.via == "rendered":
            meta.append("JavaScript-rendered")
        elif self.via == "cache":
            meta.append(f"cached {int((time.time() - self.fetched_at) / 60)} min ago")
        meta.append(f"~{est_tokens(self.markdown)} tokens total")
        bits.append(" | ".join(meta))
        return "\n".join(bits)

    def render(self) -> str:
        """Markdown block for an LLM: short provenance header, then content."""
        if not self.ok:
            return f"# {self.url}\nERROR: {self.error or 'no readable content'}"
        out = [self.header(), "", self.content]
        if not self.matched:
            out.append("\n[No passage matched the focus query; showing the start of the page.]")
        if self.next_offset is not None:
            out.append(f"\n[Truncated. Continue with offset={self.next_offset}, or pass a query to pull only the relevant passages.]")
        return "\n".join(out)

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "url", "final_url", "status", "error", "title", "description", "author", "published", "modified",
            "site", "lang", "content_type", "via", "snapshot", "content", "next_offset")}
        d["tokens_total"] = est_tokens(self.markdown)
        return d


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


# ------------------------------------------------------------------ converters

def pdf_to_markdown(data: bytes) -> tuple:
    """(markdown, title). Uses pypdf, then pdfminer, then the pdftotext binary - whichever exists."""
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for i, p in enumerate(reader.pages, 1):
            text = (p.extract_text() or "").strip()
            if text:
                pages.append(f"[page {i}]\n\n{text}")
        title = ""
        try:
            title = (reader.metadata.title or "") if reader.metadata else ""
        except Exception:
            pass
        if pages:
            return "\n\n".join(pages), str(title).strip()
    except ImportError:
        pass
    except Exception:
        pass
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        text = extract_text(io.BytesIO(data)).strip()
        if text:
            return text.replace("\f", "\n\n---\n\n"), ""
    except ImportError:
        pass
    except Exception:
        pass
    if shutil.which("pdftotext"):
        try:
            out = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True, timeout=60)
            text = out.stdout.decode("utf-8", "replace").strip()
            if text:
                return text.replace("\f", "\n\n---\n\n"), ""
        except (OSError, subprocess.SubprocessError):
            pass
    return "", ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def xml_to_markdown(text: str) -> tuple:
    """RSS/Atom feeds and sitemaps become link lists; other XML is returned as-is."""
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return text, ""
    kind = _local(root.tag)
    if kind in ("rss", "feed", "rdf"):
        title, items = "", []
        for el in root.iter():
            name = _local(el.tag)
            if name == "title" and not title:
                title = (el.text or "").strip()
            if name in ("item", "entry"):
                f = {}
                for ch in el:
                    n = _local(ch.tag)
                    if n == "link":
                        f.setdefault("link", ch.get("href") or (ch.text or "").strip())
                    elif n in ("title", "pubdate", "published", "updated", "description", "summary", "date"):
                        f.setdefault(n, (ch.text or "").strip())
                when = f.get("pubdate") or f.get("published") or f.get("updated") or f.get("date") or ""
                desc = _strip_tags(f.get("description") or f.get("summary") or "")[:300]
                items.append(f"- [{f.get('title') or f.get('link', '')}]({f.get('link', '')})"
                             + (f" - {when}" if when else "") + (f"\n  {desc}" if desc else ""))
        return f"# {title or 'Feed'}\n\n" + "\n".join(items), title
    if kind in ("urlset", "sitemapindex"):
        locs = [(el.text or "").strip() for el in root.iter() if _local(el.tag) == "loc"]
        return f"# Sitemap ({len(locs)} URLs)\n\n" + "\n".join(f"- {u}" for u in locs), "Sitemap"
    return text, ""


def _convert(r: Response, mode: str, links: str, images: bool) -> dict:
    ct = r.content_type
    path = urlsplit(r.final_url or r.url).path.lower()
    if r.body[:5] == b"%PDF-" or ct == "application/pdf":
        md, title = pdf_to_markdown(r.body)
        if not md:
            return {"error": "PDF has no extractable text (scanned image, or install pypdf)"}
        return {"markdown": md, "title": title or path.rsplit("/", 1)[-1]}
    if "json" in ct or path.endswith(".json"):
        try:
            pretty = json.dumps(r.json(), indent=1, ensure_ascii=False)
        except ValueError:
            pretty = r.text
        return {"markdown": f"```json\n{pretty}\n```", "title": path.rsplit("/", 1)[-1] or "JSON"}
    text = r.text
    head = text[:600].lstrip().lower()
    if "html" in ct or head.startswith(("<!doctype html", "<html")) or ("<body" in head or "<head" in head):
        return extract_html(text, r.final_url or r.url, mode, links, images)
    if "xml" in ct or head.startswith("<?xml") or path.endswith((".xml", ".rss", ".atom")):
        md, title = xml_to_markdown(text)
        return {"markdown": md, "title": title}
    if ct.startswith("text/") or not ct or path.endswith((".md", ".txt", ".csv", ".rst")):
        if "\x00" in text[:2000]:
            return {"error": f"binary content ({ct or 'unknown type'}) is not readable as text"}
        return {"markdown": text.strip(), "title": path.rsplit("/", 1)[-1]}
    return {"error": f"unsupported content type: {ct}"}


def _wayback(f: Fetcher, url: str):
    """Closest Internet Archive snapshot of a URL, as (Response, timestamp) or (None, '')."""
    info = f.get_json("https://archive.org/wayback/available?url=" + quote(url, safe=""), max_age=3600, timeout=8,
                      retries=0)
    snap = ((info or {}).get("archived_snapshots") or {}).get("closest") or {}
    ts = snap.get("timestamp")
    if not ts or not snap.get("available"):
        return None, ""
    r = f.fetch(f"https://web.archive.org/web/{ts}id_/{url}", robots=False, max_age=86400, timeout=12, retries=0)
    return (r, ts) if r.ok else (None, "")


def _render_js(f: Fetcher, url: str):
    """Load a page in headless Chromium via Playwright, if it is installed. Returns HTML or None."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return None
    import os
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    try:
        with sync_playwright() as p:
            kw = {"headless": True}
            if proxy:
                kw["proxy"] = {"server": proxy, "bypass": os.environ.get("NO_PROXY", "")}
            try:
                browser = p.chromium.launch(**kw)
            except Exception:
                browser = p.chromium.launch(channel="chrome", **kw)
            try:
                ctx = browser.new_context(user_agent=f.cfg.user_agent)
                page = ctx.new_page()

                def gate(route):
                    req = route.request
                    if req.resource_type in ("image", "media", "font") or f.check_url(req.url):
                        return route.abort()
                    return route.continue_()

                page.route("**/*", gate)
                page.goto(url, wait_until="domcontentloaded", timeout=int(f.cfg.timeout * 1000))
                try:
                    page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                return page.content()
            finally:
                browser.close()
    except Exception:
        return None


# ------------------------------------------------------------------ public

def read(f: Fetcher, url: str, *, query: str = "", max_tokens: int = 6000, offset: int = 0,
         mode: str = "main", links: str = "inline", images: bool = False, max_age: int | None = None,
         render: str | None = None, fallback: bool | None = None, timeout: float | None = None,
         delay: float | None = None, retries: int | None = None) -> Page:
    """Fetch a URL and return it as a Page with token-budgeted Markdown in .content.

    query      keep only the passages relevant to this question (big token saver)
    max_tokens budget for .content; offset continues a truncated page
    mode       'main' strips navigation/boilerplate, 'full' keeps the whole page
    """
    render = render or f.cfg.render
    fallback = f.cfg.wayback_fallback if fallback is None else fallback
    r = f.fetch(url, max_age=max_age, timeout=timeout, delay=delay, retries=retries)
    page = Page(url=url, final_url=r.final_url or r.url, status=r.status, via=r.via,
                content_type=r.content_type, fetched_at=r.fetched_at)
    data = None
    if r.ok:
        data = _convert(r, mode, links, images)
        thin = len(data.get("markdown", "")) < 400
        is_html = "links" in data
        if is_html and render != "never" and (render == "always" or (thin and _JS_HINT.search(r.text))):
            html = _render_js(f, r.final_url or url)
            if html:
                rendered = extract_html(html, r.final_url or url, mode, links, images)
                if len(rendered.get("markdown", "")) > len(data.get("markdown", "")):
                    data, page.via = rendered, "rendered"
    elif fallback and r.status in _BLOCKED and "robots.txt" not in r.error and "private-network" not in r.error:
        wr, ts = _wayback(f, url)
        if wr is not None:
            data = _convert(wr, mode, links, images)
            if data.get("markdown"):
                page.via, page.snapshot, page.status, page.content_type = "wayback", ts, 200, wr.content_type
                r = wr
            else:
                data = None
    if data is None:
        page.error = r.error or f"HTTP {r.status}"
        return page
    if data.get("error"):
        page.error = data["error"]
        return page
    for k in ("title", "description", "author", "published", "modified", "site", "lang", "links"):
        if data.get(k):
            setattr(page, k, data[k])
    page.markdown = data.get("markdown", "")
    if not page.markdown.strip() or data.get("empty"):
        page.markdown = ""
        page.error = "page has no readable text" + (
            " (it is built by JavaScript: install Playwright to render it)" if _JS_HINT.search(r.text) else "")
        return page
    if query and not offset:
        page.content, page.matched = focus(page.markdown, query, max_tokens)
    else:
        page.content, page.next_offset = truncate(page.markdown, max_tokens, offset)
    return page


def read_many(f: Fetcher, urls: list, workers: int = 8, **kw) -> list:
    """Read several URLs in parallel; results come back in input order."""
    urls = list(dict.fromkeys(urls))
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as pool:
        return list(pool.map(lambda u: read(f, u, **kw), urls))
