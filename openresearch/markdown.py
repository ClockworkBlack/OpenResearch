"""DOM -> compact Markdown, tuned for an LLM reader (few tokens, structure kept)."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from .dom import Node

SKIP = frozenset(
    "script style noscript template head meta link title svg canvas iframe object embed audio "
    "video map select button input textarea dialog #comment".split())
BLOCK = frozenset(
    "address article aside blockquote body caption center dd details div dl dt fieldset "
    "figcaption figure footer form h1 h2 h3 h4 h5 h6 header hr html li main nav ol p pre "
    "section summary table tbody td tfoot th thead tr ul #document".split())
_WS = re.compile(r"[ \t\r\f\v\n]+")
_LANG = re.compile(r"(?:language|lang|highlight-source|brush:)\s*-?\s*([\w+#-]+)")


def _wrap(s: str, mark: str) -> str:
    core = s.strip()
    if not core or core.startswith(mark):
        return s
    lead = s[: len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()):]
    return f"{lead}{mark}{core}{mark}{trail}"


def _tidy(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    return s.strip()


class Converter:
    def __init__(self, base_url: str = "", links: str = "inline", images: bool = False):
        self.base = base_url
        self.links = links        # inline | none
        self.images = images
        self._blocky = {}

    # ------------------------------------------------------------ helpers
    def _has_block(self, node: Node) -> bool:
        key = id(node)
        if key not in self._blocky:
            self._blocky[key] = any(n.tag in BLOCK for n in node.elements())
        return self._blocky[key]

    def _url(self, href: str) -> str:
        href = (href or "").strip()
        if not href or href.startswith(("#", "javascript:", "data:", "about:")):
            return ""
        try:
            return urljoin(self.base, href) if self.base else href
        except ValueError:
            return ""

    # ------------------------------------------------------------ inline
    def inline(self, node: Node) -> str:
        if node.tag == "#text":
            return _WS.sub(" ", node.data)
        t = node.tag
        if t in SKIP:
            return ""
        if t == "br":
            return "\n"
        if t == "img":
            if not self.images:
                return ""
            src = self._url(node.get("src") or node.get("data-src"))
            return f"![{node.get('alt').strip()}]({src})" if src else ""
        inner = "".join(self.inline(c) for c in node.children)
        if t in ("strong", "b"):
            return _wrap(inner, "**")
        if t in ("em", "i", "cite", "dfn"):
            return _wrap(inner, "*")
        if t in ("code", "kbd", "samp", "tt"):
            core = node.text(raw=True).strip()
            if not core:
                return ""
            tick = "``" if "`" in core else "`"
            return f"{tick}{core}{tick}"
        if t in ("del", "s", "strike"):
            return _wrap(inner, "~~")
        if t == "a":
            text = _WS.sub(" ", inner).strip()
            href = self._url(node.get("href"))
            if not text:
                return ""
            if self.links == "none" or not href:
                return inner
            if text == href:
                return f"<{href}>"
            return f"[{text.replace('[', '(').replace(']', ')')}]({href.replace(' ', '%20').replace(')', '%29')})"
        if t == "q":
            return f"“{inner.strip()}”"
        if t in BLOCK:
            return f" {inner} "
        return inner

    # ------------------------------------------------------------ blocks
    def blocks(self, node: Node) -> list:
        out, buf = [], []

        def flush():
            s = _tidy("".join(buf))
            buf.clear()
            if s:
                out.append(s)

        for c in node.children:
            t = c.tag
            if t == "#text":
                buf.append(_WS.sub(" ", c.data))
            elif t in SKIP:
                continue
            elif t not in BLOCK and (t == "a" or not self._has_block(c)):
                buf.append(self.inline(c))
            else:
                flush()
                out.extend(self.block(c))
        flush()
        return out

    def block(self, node: Node) -> list:
        t = node.tag
        if len(t) == 2 and t[0] == "h" and t[1] in "123456":
            text = _WS.sub(" ", "".join(self.inline(c) for c in node.children)).strip()
            text = re.sub(r"\s*(?:¶|#|\[edit\])\s*$", "", text)
            return [f"{'#' * int(t[1])} {text}"] if text else []
        if t == "pre":
            code = node.text(raw=True).strip("\n")
            if not code.strip():
                return []
            m = _LANG.search(node.get("class") + " " + " ".join(
                c.get("class") for c in node.children if c.tag == "code"))
            fence = "````" if "```" in code else "```"
            return [f"{fence}{m.group(1) if m else ''}\n{code}\n{fence}"]
        if t == "blockquote":
            inner = "\n\n".join(self.blocks(node))
            return ["\n".join(("> " + ln).rstrip() for ln in inner.split("\n"))] if inner else []
        if t in ("ul", "ol"):
            return self._list(node)
        if t == "li":
            inner = "\n".join(self.blocks(node))
            return ["- " + inner.replace("\n", "\n  ")] if inner else []
        if t == "table":
            return self._table(node)
        if t == "hr":
            return ["---"]
        if t == "dt":
            text = _tidy("".join(self.inline(c) for c in node.children))
            return [f"**{text}**"] if text else []
        if t == "summary":
            text = _tidy("".join(self.inline(c) for c in node.children))
            return [f"**{text}**"] if text else []
        if t == "figcaption":
            text = _tidy(" ".join(self.blocks(node)))
            return [f"*{text}*"] if text else []
        return self.blocks(node)

    def _list(self, node: Node) -> list:
        items, ordered = [], node.tag == "ol"
        try:
            num = int(node.get("start") or 1)
        except ValueError:
            num = 1
        for c in node.children:
            if c.tag == "#text" or c.tag in SKIP:
                continue
            inner = "\n".join(self.blocks(c) if c.tag == "li" else self.block(c))
            if not inner:
                continue
            marker = f"{num}. " if ordered else "- "
            num += 1
            items.append(marker + inner.replace("\n", "\n" + " " * len(marker)))
        return ["\n".join(items)] if items else []

    def table_rows(self, table: Node) -> list:
        """Rows of a table as lists of cell strings (nested tables excluded)."""
        rows = []

        def collect(n):
            for ch in n.children:
                if ch.tag == "tr":
                    cells = []
                    for td in ch.children:
                        if td.tag in ("td", "th"):
                            text = _tidy(" ".join(self.blocks(td))).replace("\n", " ")
                            cells.append(text)
                            try:
                                span = min(int(td.get("colspan") or 1), 20)
                            except ValueError:
                                span = 1
                            cells.extend([""] * (span - 1))
                    rows.append(cells)
                elif ch.tag in ("thead", "tbody", "tfoot"):
                    collect(ch)

        collect(table)
        return rows

    def _table(self, node: Node) -> list:
        nested = any(n.tag == "table" for n in node.elements())
        rows = [] if nested else [r for r in self.table_rows(node) if any(r)]
        ncols = max((len(r) for r in rows), default=0)
        if nested or ncols < 2 or any(len(c) > 600 for r in rows for c in r):
            out = []
            for n in node.children:      # layout table: just read it top to bottom
                if n.tag[0] != "#" and n.tag not in SKIP:
                    out.extend(self.blocks(n) if n.tag != "caption" else self.block(n))
            return out
        out = []
        cap = next((c for c in node.children if c.tag == "caption"), None)
        if cap is not None and cap.text():
            out.append(f"**{cap.text()}**")
        lines = []
        for i, r in enumerate(rows):
            r = r + [""] * (ncols - len(r))
            lines.append("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
            if i == 0:
                lines.append("|" + " --- |" * ncols)
        out.append("\n".join(lines))
        return out

    def convert(self, node: Node) -> str:
        md = "\n\n".join(b for b in self.blocks(node) if b.strip())
        md = re.sub(r"\n{3,}", "\n\n", md)
        md = re.sub(r"[ \t]+\n", "\n", md)
        return md.strip()


def to_markdown(node: Node, base_url: str = "", links: str = "inline", images: bool = False) -> str:
    return Converter(base_url, links, images).convert(node)
