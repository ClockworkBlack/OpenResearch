"""A small, forgiving HTML tree and CSS-selector engine built on the standard library."""
from __future__ import annotations

import re
from html.parser import HTMLParser

VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_CLOSES_P = frozenset(
    "address article aside blockquote details div dl fieldset figcaption figure footer form "
    "h1 h2 h3 h4 h5 h6 header hr main nav ol p pre section table ul".split())
_SCOPE = frozenset("table td th caption html body button object template #document".split())
_MAX_DEPTH = 300


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "data")

    def __init__(self, tag, attrs=None, parent=None, data=""):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent
        self.data = data

    def get(self, key, default=""):
        return self.attrs.get(key, default)

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def walk(self):
        """All descendant nodes (elements and text) in document order."""
        stack = list(reversed(self.children))
        while stack:
            n = stack.pop()
            yield n
            if n.children:
                stack.extend(reversed(n.children))

    def elements(self):
        return (n for n in self.walk() if n.tag[0] != "#")

    def find(self, *tags):
        for n in self.walk():
            if n.tag in tags:
                return n
        return None

    def find_all(self, *tags):
        return [n for n in self.walk() if n.tag in tags]

    def text(self, raw=False):
        """Text content. raw=True keeps whitespace and turns <br> into newlines."""
        if self.tag == "#text":
            parts = [self.data]
        else:
            parts = []
            for n in self.walk():
                if n.tag == "#text":
                    if n.parent.tag not in ("script", "style", "template"):
                        parts.append(n.data)
                elif n.tag == "br":
                    parts.append("\n")
        s = "".join(parts)
        return s if raw else re.sub(r"\s+", " ", s).strip()

    def remove(self):
        if self.parent is not None:
            try:
                self.parent.children.remove(self)
            except ValueError:
                pass
            self.parent = None

    def __repr__(self):
        return f"<{self.tag} {self.attrs}>" if self.tag != "#text" else repr(self.data[:40])


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def _close(self, targets, stops):
        for i in range(len(self.stack) - 1, 0, -1):
            t = self.stack[i].tag
            if t in targets:
                del self.stack[i:]
                return
            if t in stops:
                return

    def _imply(self, tag):
        if tag in _CLOSES_P:
            self._close(("p",), _SCOPE | {"li", "blockquote", "div", "section", "article"})
        if tag == "li":
            self._close(("li",), _SCOPE | {"ul", "ol"})
        elif tag in ("dt", "dd"):
            self._close(("dt", "dd"), _SCOPE | {"dl"})
        elif tag == "tr":
            self._close(("tr",), {"table", "#document"})
        elif tag in ("td", "th"):
            self._close(("td", "th"), {"tr", "table", "#document"})
        elif tag in ("thead", "tbody", "tfoot"):
            self._close(("thead", "tbody", "tfoot"), {"table", "#document"})
        elif tag == "option":
            self._close(("option",), {"select", "datalist", "#document"})

    def handle_starttag(self, tag, attrs):
        self._imply(tag)
        parent = self.stack[-1]
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, parent)
        parent.children.append(node)
        if tag not in VOID and len(self.stack) < _MAX_DEPTH:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._imply(tag)
        parent = self.stack[-1]
        parent.children.append(Node(tag, {k: (v if v is not None else "") for k, v in attrs}, parent))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data:
            parent = self.stack[-1]
            parent.children.append(Node("#text", None, parent, data))


def parse(html: str) -> Node:
    b = _Builder()
    try:
        b.feed(html)
        b.close()
    except Exception:  # html.parser is lenient, but never let a bad page crash a batch
        pass
    return b.root


# ---------------------------------------------------------------- CSS selectors

_PART = re.compile(r"""
    (?P<tag>^(?:[a-zA-Z][\w-]*|\*))
  | \#(?P<id>[\w-]+)
  | \.(?P<cls>[\w-]+)
  | \[\s*(?P<attr>[\w:-]+)\s*(?:(?P<op>[~^$*|]?=)\s*(?P<q>["']?)(?P<val>.*?)(?P=q))?\s*\]
  | :(?P<pseudo>[\w-]+)(?:\((?P<arg>[^)]*)\))?
""", re.X)


def _split(selector, seps):
    """Split on separator chars that sit outside [] and ()."""
    out, cur, depth, quote = [], [], 0, ""
    for ch in selector:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if depth == 0 and ch in seps:
            out.append(("".join(cur), ch))
            cur = []
        else:
            cur.append(ch)
    out.append(("".join(cur), ""))
    return out


def _compile_compound(text):
    checks, pos = [], 0
    while pos < len(text):
        m = _PART.match(text, pos) if pos else _PART.match(text)
        if not m or m.end() == pos:
            raise ValueError(f"unsupported selector: {text!r}")
        pos = m.end()
        g = m.groupdict()
        if g["tag"]:
            if g["tag"] != "*":
                checks.append(("tag", g["tag"].lower()))
        elif g["id"]:
            checks.append(("id", g["id"]))
        elif g["cls"]:
            checks.append(("cls", g["cls"]))
        elif g["attr"]:
            checks.append(("attr", g["attr"].lower(), g["op"], g["val"]))
        else:
            checks.append(("pseudo", g["pseudo"].lower(), (g["arg"] or "").strip().strip("\"'")))
    return checks


def _compile(selector):
    groups = []
    for group, _ in _split(selector, ","):
        group = re.sub(r"\s*>\s*", ">", group.strip())
        if not group:
            continue
        parts, comb = [], " "
        for piece, sep in _split(group, " >"):
            if piece:
                parts.append((comb, _compile_compound(piece)))
                comb = " "
            if sep == ">":
                comb = ">"
        if parts:
            groups.append(parts)
    return groups


def _elem_children(node):
    return [c for c in node.children if c.tag[0] != "#"]


def _test(node, checks):
    for c in checks:
        kind = c[0]
        if kind == "tag":
            if node.tag != c[1]:
                return False
        elif kind == "id":
            if node.attrs.get("id") != c[1]:
                return False
        elif kind == "cls":
            if c[1] not in node.attrs.get("class", "").split():
                return False
        elif kind == "attr":
            _, name, op, val = c
            if name not in node.attrs:
                return False
            have = node.attrs[name]
            if op is None:
                continue
            if op == "=" and have != val:
                return False
            if op == "^=" and not have.startswith(val):
                return False
            if op == "$=" and not have.endswith(val):
                return False
            if op == "*=" and val not in have:
                return False
            if op == "~=" and val not in have.split():
                return False
            if op == "|=" and not (have == val or have.startswith(val + "-")):
                return False
        else:
            _, name, arg = c
            if name in ("first-child", "last-child", "nth-child"):
                if node.parent is None:
                    return False
                sibs = _elem_children(node.parent)
                idx = sibs.index(node) + 1
                if name == "first-child" and idx != 1:
                    return False
                if name == "last-child" and idx != len(sibs):
                    return False
                if name == "nth-child" and (not arg.isdigit() or idx != int(arg)):
                    return False
            elif name == "contains":
                if arg.lower() not in node.text().lower():
                    return False
            elif name == "not":
                if _test(node, _compile_compound(arg)):
                    return False
            else:
                raise ValueError(f"unsupported pseudo-class :{name}")
    return True


def _matches(node, parts, i):
    if not _test(node, parts[i][1]):
        return False
    if i == 0:
        return True
    comb, p = parts[i][0], node.parent
    if comb == ">":
        return p is not None and p.tag[0] != "#" and _matches(p, parts, i - 1)
    while p is not None and p.tag[0] != "#":
        if _matches(p, parts, i - 1):
            return True
        p = p.parent
    return False


def select(root: Node, selector: str) -> list:
    """Descendants of root matching a CSS selector (tag, #id, .class, [attr], >, space, commas,
    :first-child, :last-child, :nth-child(n), :not(x), :contains(text))."""
    groups = _compile(selector)
    out = []
    for n in root.elements():
        for parts in groups:
            if _matches(n, parts, len(parts) - 1):
                out.append(n)
                break
    return out


def select_one(root: Node, selector: str):
    r = select(root, selector)
    return r[0] if r else None
