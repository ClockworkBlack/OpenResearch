"""Structured extraction without an LLM: CSS selectors, tables, JSON-LD, embedded app state."""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from .dom import Node, parse, select
from .extract import _meta, jsonld, links
from .markdown import Converter

_SUFFIX = re.compile(r"::(text|html|attr\(([\w:-]+)\))\s*$")


def _value(node: Node, how: str, attr: str, base: str):
    if how == "attr":
        v = node.get(attr)
        return urljoin(base, v) if attr in ("href", "src", "action") and v and base else v
    if how == "html":
        return Converter(base).convert(node)
    return node.text()


def _apply(root: Node, spec, base: str):
    """spec is 'css', 'css::text', 'css::attr(name)', or
    {'selector': css, 'all': bool, 'attr': name, 'fields': {name: spec}}."""
    if isinstance(spec, str):
        spec = {"selector": spec}
    sel = spec.get("selector", "")
    how, attr = "text", spec.get("attr", "")
    m = _SUFFIX.search(sel)
    if m:
        sel = sel[: m.start()].strip()
        how = "attr" if m.group(1).startswith("attr") else m.group(1)
        attr = m.group(2) or attr
    elif attr:
        how = "attr"
    nodes = select(root, sel) if sel else [root]
    fields = spec.get("fields")

    def one(n):
        if fields:
            return {k: _apply(n, v, base) for k, v in fields.items()}
        return _value(n, how, attr, base)

    if spec.get("all") or (fields and spec.get("all") is None and len(nodes) > 1):
        return [one(n) for n in nodes]
    return one(nodes[0]) if nodes else None


def tables(root: Node, base: str = "") -> list:
    conv, out = Converter(base, links="none"), []
    for t in root.find_all("table"):
        if any(n.tag == "table" for n in t.elements()):
            continue
        rows = [r for r in conv.table_rows(t) if any(r)]
        if len(rows) < 2 or max(len(r) for r in rows) < 2:
            continue
        cap = next((c.text() for c in t.children if c.tag == "caption"), "")
        header = rows[0]
        records = [dict(zip(header, r)) for r in rows[1:]] if len(set(header)) == len(header) and all(header) else None
        out.append({"caption": cap, "header": header, "rows": rows[1:], **({"records": records} if records else {})})
    return out


def embedded_state(root: Node) -> dict:
    """JSON blobs that JavaScript apps ship in the page (Next.js, Nuxt, etc.)."""
    out = {}
    for s in root.find_all("script"):
        raw = "".join(c.data for c in s.children if c.tag == "#text").strip()
        if not raw:
            continue
        sid, typ = s.get("id"), s.get("type").lower()
        if sid in ("__NEXT_DATA__", "__NUXT_DATA__") or (typ == "application/json" and sid):
            try:
                out[sid] = json.loads(raw)
            except ValueError:
                pass
    return out


def extract(html: str, url: str = "", selectors: dict | None = None,
            want=("meta", "tables", "jsonld")) -> dict:
    """Pull structured data out of a page. `want` may include: meta, tables, jsonld, links, state."""
    root = parse(html)
    out = {}
    if selectors:
        out["data"] = {name: _apply(root, spec, url) for name, spec in selectors.items()}
    if "meta" in want:
        out["meta"] = {k: v for k, v in _meta(root).items() if v}
    if "jsonld" in want:
        ld = jsonld(root)
        if ld:
            out["jsonld"] = ld
    if "tables" in want:
        tb = tables(root, url)
        if tb:
            out["tables"] = tb
    if "links" in want:
        out["links"] = [{"url": u, "text": t} for u, t in links(root, url)]
    if "state" in want:
        st = embedded_state(root)
        if st:
            out["state"] = st
    return out
