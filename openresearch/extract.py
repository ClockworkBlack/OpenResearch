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
_SEMANTIC = ("article, main, [role=main], [itemprop=articleBody], #content, #main-content, "
             "#main, .post-content, .entry-content, .article-body, .article-content, "
             "#mw-content-text, .markdown-body, .content")
_PROTECTED = frozenset("html body article main #document".split())
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


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
        else:
            sig = (n.get("id") + " " + n.get("class")).strip()
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


def _meta(root: Node) -> dict:
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
        "author": metas.get("author") or metas.get("article:author") or metas.get("parsely-author") or "",
        "published": (metas.get("article:published_time") or metas.get("datepublished") or metas.get("date")
                      or metas.get("dc.date") or metas.get("parsely-pub-date") or metas.get("pubdate") or ""),
        "modified": metas.get("article:modified_time") or metas.get("datemodified") or metas.get("last-modified") or "",
        "site": metas.get("og:site_name") or "",
        "lang": html.get("lang") if html else "",
        "canonical": canonical,
    }
    for ld in jsonld(root):
        for item in (ld.get("@graph") if isinstance(ld, dict) and isinstance(ld.get("@graph"), list) else [ld]):
            if not isinstance(item, dict):
                continue
            out["published"] = out["published"] or str(item.get("datePublished") or "")
            out["modified"] = out["modified"] or str(item.get("dateModified") or "")
            if not out["author"]:
                a = item.get("author")
                a = a[0] if isinstance(a, list) and a else a
                out["author"] = a.get("name", "") if isinstance(a, dict) else (a if isinstance(a, str) else "")
    if not out["published"]:
        t = next((t for t in root.find_all("time") if t.get("datetime")), None)
        out["published"] = t.get("datetime") if t else ""
    for k in ("published", "modified"):
        m = _DATE.search(out[k] or "")
        out[k] = m.group(0) if m else (out[k] or "")[:40]
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
    root = parse(html)
    meta = _meta(root)
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
