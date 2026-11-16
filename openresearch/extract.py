"""Main-content extraction and page metadata."""
from __future__ import annotations

import json
import re
from urllib.parse import urldefrag, urljoin

from .dom import Node, parse, select
from .markdown import to_markdown

_ALWAYS_DROP = frozenset("script style noscript template iframe svg canvas object embed".split())
_BOILER_TAGS = frozenset("nav footer aside".split())
_BOILER_ROLES = frozenset("navigation banner contentinfo complementary search dialog alert alertdialog menu menubar".split())
_NEG = re.compile(
    r"(?:^|[\s_-])(?:comments?|disqus|sidebar|side-bar|share|sharing|social|promo|related|"
    r"recommend(?:ed|ations)?|cookies?|consent|gdpr|banner|advert(?:isement)?|ads?|sponsor(?:ed)?|"
    r"newsletter|subscribe|signup|popup|modal|overlay|breadcrumbs?|footer|nav|navbar|navigation|"
    r"menu|masthead|toolbar|pagination|pager|skip|widget|outbrain|taboola|hidden|sr-only|"
    r"visually-hidden|screen-reader-text|navbox|mw-editsection|toc|noprint|paywall)(?:$|[\s_-])",
    re.I)
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
# Utility classes that contain "hidden" but leave content visible (overflow-hidden clips, it doesn't hide).
_NOT_HIDING = re.compile(r"^(?:overflow|text|truncate)(?:-[xy])?-hidden$|^[\w-]+:[\w-]*hidden$", re.I)
# A responsive "display" class next to a plain `hidden` means visible at some screen width (Tailwind etc.).
_SHOWN_AT = re.compile(r"^[\w-]+:(?:block|flex|grid|inline|inline-block|inline-flex|inline-grid|table|contents)$")


def _signature(n: Node) -> str:
    """id + class for the boilerplate check, minus classes that only look like hiding."""
    classes = [c for c in n.get("class").split() if not _NOT_HIDING.match(c)]
    if any(_SHOWN_AT.match(c) for c in classes):
        classes = [c for c in classes if c.lower() != "hidden"]
    return (n.get("id") + " " + " ".join(classes)).strip()


# React streaming SSR: content arrives in <div hidden id="S:n"> segments that a script moves into
# <template id="P:n"> placeholders and <!--$?--><template id="B:n">fallback<!--/$--> boundaries.
_SEGMENT = re.compile(r'<div hidden(?:="")? id="S:(\d+)">', re.I)
_DIV_TAG = re.compile(r"<div\b|</div\s*>", re.I)
_BOUNDARY_SWAP = re.compile(r'\$RC\(\s*"B:(\d+)"\s*,\s*"S:(\d+)"\s*\)')
_SUSPENSE_MARK = re.compile(r"<!--\$[?!]?-->|<!--/\$-->")


def _inline_react_segments(html: str) -> str:
    """Do what React's inline scripts would: put streamed segments where they belong."""
    if "hidden" not in html or not _SEGMENT.search(html):
        return html
    segments, spans = {}, []
    for m in _SEGMENT.finditer(html):
        depth, end = 0, None
        for t in _DIV_TAG.finditer(html, m.start()):
            depth += -1 if t.group().startswith("</") else 1
            if depth == 0:
                end = t
                break
        if end is None:
            continue
        segments[m.group(1)] = html[m.end():end.start()]
        spans.append((m.start(), end.end()))
    if not segments:
        return html
    boundaries = {b: s for b, s in _BOUNDARY_SWAP.findall(html) if s in segments}
    for a, b in reversed(spans):                       # drop the hidden copies; they get inlined below
        html = html[:a] + html[b:]

    def fill(text: str, seen: frozenset) -> str:
        def placeholder(m):
            n = m.group(1)
            return fill(segments[n], seen | {n}) if n in segments and n not in seen else ""
        text = re.sub(r'<template id="P:(\d+)"></template>', placeholder, text)
        out, pos = [], 0
        for m in re.finditer(r'<!--\$\?--><template id="B:(\d+)"></template>', text):
            seg = boundaries.get(m.group(1))
            if seg is None or seg in seen:
                continue
            depth, close = 1, None                       # skip the fallback up to the matching <!--/$-->
            for t in _SUSPENSE_MARK.finditer(text, m.end()):
                depth += -1 if t.group() == "<!--/$-->" else 1
                if depth == 0:
                    close = t
                    break
            if close is None or m.start() < pos:
                continue
            out += [text[pos:m.start()], fill(segments[seg], seen | {seg})]
            pos = close.end()
        return "".join(out) + text[pos:]

    return fill(html, frozenset())
_SEMANTIC = ("article, main, [role=main], [itemprop=articleBody], #content, #main-content, "
             "#main, .post-content, .entry-content, .article-body, .article-content, "
             "#mw-content-text, .markdown-body, .content")
_PROTECTED = frozenset("html body article main #document".split())
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_DATE_SLASH = re.compile(r"\b((?:19|20)\d{2})[/.](\d{1,2})[/.](\d{1,2})\b")
_DATE_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+((?:19|20)\d{2})\b")
_DATE_MDY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{2})\b")
_URL_DATE = re.compile(r"/((?:19|20)\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)")
_PUBLISHED_KEYS = ("article:published_time", "datepublished", "date", "dc.date", "dc.date.issued", "dcterms.issued",
                   "dcterms.created", "dcterms.date", "citation_publication_date", "citation_date",
                   "citation_online_date", "parsely-pub-date", "pubdate", "publish-date", "publication_date",
                   "sailthru.date", "og:published_time", "article.published", "datecreated", "uploaddate")
_MODIFIED_KEYS = ("article:modified_time", "datemodified", "last-modified", "og:updated_time", "dcterms.modified",
                  "dc.date.modified")


def norm_date(s: str) -> str:
    """Any common date spelling -> YYYY-MM-DD, or '' when there is no full date in it."""
    s = s or ""
    m = _DATE.search(s)
    if m:
        return m.group(0)
    y = mo = d = 0
    m = _DATE_SLASH.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _DATE_DMY.search(s)
        if m and m.group(2)[:3].lower() in _MONTHS:
            y, mo, d = int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1))
        else:
            m = _DATE_MDY.search(s)
            if m and m.group(1)[:3].lower() in _MONTHS:
                y, mo, d = int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2))
    return f"{y:04d}-{mo:02d}-{d:02d}" if y and 1 <= mo <= 12 and 1 <= d <= 31 else ""


def _first(metas: dict, keys) -> str:
    return next((metas[k] for k in keys if metas.get(k)), "")


def _text_lens(root: Node) -> dict:
    """Text length of every element, in one post-order pass."""
    lens = {}
    order = [root] + list(root.walk())
    for n in reversed(order):
        if n.tag == "#text":
            lens[id(n)] = len(n.data.strip())
        else:
            lens[id(n)] = sum(lens.get(id(c), 0) for c in n.children)
    return lens


def _link_len(node: Node) -> int:
    return sum(len(a.text()) for a in node.find_all("a"))


def _is_page_toc(lst: Node) -> bool:
    """A list of jump links to headings on the same page: pure navigation, the headings follow anyway."""
    links = lst.find_all("a")
    if len(links) < 3 or any(not a.get("href").startswith("#") for a in links):
        return False
    return sum(len(a.text()) for a in links) >= 0.8 * max(len(lst.text()), 1)


def _strip(root: Node, main_only: bool):
    for n in list(root.walk()):
        if n.tag in _ALWAYS_DROP or n.tag == "head":
            n.remove()
    if not main_only:
        return
    lens = _text_lens(root)
    total = max(lens[id(root)], 1)
    for n in list(root.elements()):
        if n.parent is None or n.tag in _PROTECTED:
            continue
        drop = False
        if n.tag in _BOILER_TAGS:
            drop = True
        elif n.tag == "header":
            p, inside = n.parent, False
            while p is not None:
                if p.tag in ("article", "main"):
                    inside = True
                    break
                p = p.parent
            drop = not inside
        elif n.tag == "form":
            drop = lens[id(n)] < 0.3 * total
        elif n.get("role") in _BOILER_ROLES:
            drop = True
        elif "hidden" in n.attrs or n.get("aria-hidden") == "true":
            drop = True
        elif n.get("style") and _HIDDEN_STYLE.search(n.get("style")):
            drop = True
        elif n.tag in ("ul", "ol") and _is_page_toc(n):
            drop = True
        else:
            sig = _signature(n)
            if sig and _NEG.search(sig) and lens[id(n)] < 0.4 * total:
                drop = True
        if drop:
            n.remove()


def _pick_main(body: Node) -> Node:
    lens = _text_lens(body)
    total = lens[id(body)]
    if total == 0:
        return body
    floor = max(200, 0.35 * total)
    semantic = [n for n in select(body, _SEMANTIC) if lens.get(id(n), 0) >= floor]
    if semantic:
        return min(semantic, key=lambda n: lens[id(n)])   # tightest wrapper that still holds the text

    scores = {}
    nodes = {}
    for n in body.elements():
        if n.tag not in ("p", "pre", "td", "blockquote", "li", "dd"):
            continue
        text = n.text()
        if len(text) < 25:
            continue
        s = 1 + text.count(",") + min(len(text) / 100.0, 3)
        p, share = n.parent, 1.0
        for _ in range(3):
            if p is None or p.tag == "#document":
                break
            scores[id(p)] = scores.get(id(p), 0.0) + s * share
            nodes[id(p)] = p
            share /= 2
            p = p.parent
    best, best_score = None, 0.0
    for key, s in scores.items():
        n = nodes[key]
        tl = max(lens.get(key, 0), 1)
        s *= max(0.0, 1 - _link_len(n) / tl)
        scores[key] = s
        if s > best_score:
            best, best_score = n, s
    if best is None or lens.get(id(best), 0) < max(200, 0.15 * total):
        return body
    parent = best.parent
    if parent is None or parent.tag in ("body", "html", "#document"):
        return best
    wrapper = Node("div")
    for sib in parent.children:
        keep = sib is best or scores.get(id(sib), 0) >= 0.2 * best_score
        if not keep and sib.tag in ("p", "h1", "h2", "h3", "h4", "ul", "ol", "pre", "blockquote", "table", "figure"):
            tl = lens.get(id(sib), 0)
            keep = tl > 0 and (sib.tag != "p" or tl > 60) and _link_len(sib) < 0.5 * max(tl, 1)
        if keep:
            wrapper.children.append(sib)
    return wrapper


def _meta(root: Node, url: str = "") -> dict:
    metas = {}
    for m in root.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop")).lower()
        if key and m.get("content") and key not in metas:
            metas[key] = m.get("content").strip()
    title_node = root.find("title")
    h1 = root.find("h1")
    html = root.find("html")
    canonical = ""
    for ln in root.find_all("link"):
        if "canonical" in ln.get("rel").lower().split():
            canonical = ln.get("href")
            break
    out = {
        "title": metas.get("og:title") or (title_node.text() if title_node else "") or (h1.text() if h1 else ""),
        "description": metas.get("description") or metas.get("og:description") or "",
        "author": (metas.get("author") or metas.get("article:author") or metas.get("parsely-author")
                   or metas.get("citation_author") or metas.get("dc.creator") or ""),
        "published": _first(metas, _PUBLISHED_KEYS),
        "modified": _first(metas, _MODIFIED_KEYS),
        "site": metas.get("og:site_name") or "",
        "lang": html.get("lang") if html else "",
        "canonical": canonical,
    }
    items = []
    for ld in jsonld(root):
        for top in (ld if isinstance(ld, list) else [ld]):
            items += top["@graph"] if isinstance(top, dict) and isinstance(top.get("@graph"), list) else [top]
    for item in items:
        if not isinstance(item, dict):
            continue
        out["published"] = out["published"] or str(
            item.get("datePublished") or item.get("uploadDate") or item.get("dateCreated") or "")
        out["modified"] = out["modified"] or str(item.get("dateModified") or "")
        if not out["author"]:
            a = item.get("author")
            a = a[0] if isinstance(a, list) and a else a
            out["author"] = a.get("name", "") if isinstance(a, dict) else (a if isinstance(a, str) else "")
    if not norm_date(out["published"]):
        # one <time> is the article's date; several are a listing (a blog index, a feed of posts)
        times = {norm_date(t.get("datetime")) for t in root.find_all("time") if t.get("datetime")} - {""}
        m = _URL_DATE.search(url)
        marked = next((t.get("datetime") for t in root.find_all("time") if t.get("datetime") and (
            "pubdate" in t.attrs or "datepublished" in t.get("itemprop").lower()
            or re.search(r"publish|posted|entry-date", t.get("class"), re.I))), "")
        if len(times) == 1:
            out["published"] = times.pop()
        elif marked:
            out["published"] = marked
        elif m:
            out["published"] = norm_date("/".join(m.groups()))
    for k in ("published", "modified"):
        out[k] = norm_date(out[k])
    return {k: (v or "").strip() if isinstance(v, str) else "" for k, v in out.items()}


def jsonld(root: Node) -> list:
    out = []
    for s in root.find_all("script"):
        if "ld+json" in s.get("type").lower():
            raw = "".join(c.data for c in s.children if c.tag == "#text").strip()
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            out.extend(data if isinstance(data, list) else [data])
    return out


def links(root: Node, base_url: str) -> list:
    """(absolute_url, anchor_text) for every http(s) link, de-duplicated, in first-seen order."""
    base = base_url
    b = root.find("base")
    if b is not None and b.get("href"):
        base = urljoin(base_url, b.get("href"))
    found = {}
    for a in root.find_all("a"):
        href = a.get("href").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        try:
            url = urldefrag(urljoin(base, href))[0]
        except ValueError:
            continue
        if url.startswith(("http://", "https://")):
            text = a.text()[:120]
            if len(text) > len(found.get(url, "")) or url not in found:
                found[url] = text                      # keep the most descriptive anchor text
    return list(found.items())


def extract_html(html: str, url: str = "", mode: str = "main", link_style: str = "inline",
                 images: bool = False) -> dict:
    """HTML -> {markdown, title, ..., links}. mode: 'main' (article only) or 'full' (whole page)."""
    root = parse(_inline_react_segments(html))
    meta = _meta(root, url)
    all_links = links(root, url)
    body = root.find("body") or root
    _strip(root, main_only=(mode != "full"))
    target = _pick_main(body) if mode != "full" else body
    md = to_markdown(target, url, link_style, images)
    if mode != "full" and len(md) < 200:
        fuller = to_markdown(body, url, link_style, images)
        if len(fuller) > len(md) * 1.5:
            md = fuller
    title = re.sub(r"\s+", " ", meta["title"]).strip()
    meta["empty"] = not md.strip()
    if title and not re.search(r"^# ", md, re.M):
        md = f"# {title}\n\n{md}"
    meta.update(markdown=md.strip(), links=all_links, title=title)
    return meta
