"""Chunking, BM25 passage ranking, token budgeting. No models, no network."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

_WORD = re.compile(r"\w+", re.UNICODE)
STOP = frozenset("""a about above after again all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have
having he her here hers him his how i if in into is it its just me more most my no nor not now of off on
once only or other our out over own same she should so some such than that the their them then there
these they this those through to too under until up very was we were what when where which while who
whom why will with would you your""".split())


_SUFFIXES = ("izations", "ization", "ations", "ation", "ments", "ment", "ings", "ing", "edly", "ied", "ies",
             "ed", "ly", "es", "s")


def stem(w: str) -> str:
    """Light suffix stripping so 'configure', 'configured' and 'configuration' match each other."""
    if len(w) <= 3 or w.isdigit():
        return w
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            if suf == "s" and w.endswith(("ss", "us", "is")):
                break
            w = w[: -len(suf)] + ("y" if suf in ("ies", "ied") else "")
            break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def tokens(text: str) -> list:
    return [stem(w) for w in (m.group(0).lower() for m in _WORD.finditer(text)) if w not in STOP and len(w) > 1]


def est_tokens(text: str) -> int:
    """Cheap token estimate (about 4 characters per token for English prose)."""
    return (len(text) + 3) // 4


@dataclass
class Chunk:
    text: str
    heading: str = ""
    index: int = 0
    doc: int = 0
    score: float = 0.0


_FENCE = re.compile(r"^(```|````)")


def _split_blocks(md: str) -> list:
    blocks, cur, fenced = [], [], False
    for line in md.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
        if not line.strip() and not fenced:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def _split_long(block: str, limit: int) -> list:
    if len(block) <= limit:
        return [block]
    pieces = block.split("\n") if "\n" in block else re.split(r"(?<=[.!?])\s+", block)
    sep = "\n" if "\n" in block else " "
    out, cur = [], ""
    for p in pieces:
        while len(p) > limit:                      # one unbroken run: hard cut
            if cur:
                out.append(cur)
                cur = ""
            out.append(p[:limit])
            p = p[limit:]
        if cur and len(cur) + len(p) + 1 > limit:
            out.append(cur)
            cur = p
        else:
            cur = cur + sep + p if cur else p
    if cur:
        out.append(cur)
    return out


def chunk_markdown(md: str, target: int = 1000, doc: int = 0) -> list:
    """Split Markdown into passages of roughly `target` characters, tracking the heading path."""
    chunks, path, cur, cur_heading = [], [], [], ""

    def flush():
        nonlocal cur
        if cur:
            chunks.append(Chunk("\n\n".join(cur), cur_heading, len(chunks), doc))
            cur = []

    for block in _split_blocks(md):
        m = re.match(r"^(#{1,6})\s+(.*)", block)
        if m and "\n" not in block:
            level = len(m.group(1))
            if sum(len(b) for b in cur) >= 200:
                flush()
            path = [(lv, t) for lv, t in path if lv < level] + [(level, m.group(2).strip())]
            if not cur:
                cur_heading = " > ".join(t for _, t in path[:-1])
            cur.append(block)
            continue
        for piece in _split_long(block, target * 2):
            if cur and sum(len(b) for b in cur) + len(piece) > target:
                flush()
            if not cur:
                cur_heading = " > ".join(t for _, t in path)
            cur.append(piece)
    flush()
    return chunks


class BM25:
    def __init__(self, docs: list, k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.avg = (sum(len(d) for d in docs) / self.n) if self.n else 0.0
        self.tf, df = [], {}
        for d in docs:
            tf = {}
            for t in d:
                tf[t] = tf.get(t, 0) + 1
            self.tf.append(tf)
            for t in tf:
                df[t] = df.get(t, 0) + 1
        self.idf = {t: math.log(1 + (self.n - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def scores(self, query: list) -> list:
        q = list(dict.fromkeys(query))
        out = []
        for d, tf in zip(self.docs, self.tf):
            s, hit = 0.0, 0
            norm = self.k1 * (1 - self.b + self.b * (len(d) / self.avg if self.avg else 1))
            for t in q:
                f = tf.get(t)
                if f:
                    hit += 1
                    s += self.idf[t] * f * (self.k1 + 1) / (f + norm)
            if q and s:
                s *= 0.5 + 0.5 * hit / len(q)      # reward passages covering more of the question
            out.append(s)
        return out


def rank_chunks(chunks: list, query: str) -> list:
    """Score chunks in place against the query; returns them sorted best-first."""
    if not chunks:
        return []
    q = tokens(query)
    bm = BM25([tokens(c.heading + " " + c.text) for c in chunks])
    phrase = [p.lower() for p in re.findall(r'"([^"]{3,})"', query)]
    for c, s in zip(chunks, bm.scores(q)):
        low = c.text.lower()
        if s and phrase and any(p in low for p in phrase):
            s *= 1.5
        c.score = s
    return sorted(chunks, key=lambda c: -c.score)


def _shingles(text: str) -> set:
    t = tokens(text)
    return {hash(tuple(t[i:i + 4])) for i in range(max(len(t) - 3, 1))}


def dedupe(chunks: list, threshold: float = 0.7) -> list:
    """Drop near-duplicate passages (syndicated or quoted text), keeping the first seen."""
    kept, sigs = [], []
    for c in chunks:
        s = _shingles(c.text)
        if any(len(s & o) / max(min(len(s), len(o)), 1) >= threshold for o in sigs):
            continue
        kept.append(c)
        sigs.append(s)
    return kept


def focus(md: str, query: str, max_tokens: int) -> tuple:
    """The passages of a document most relevant to `query`, in document order, within a budget.
    Returns (text, matched) where matched is False when nothing in the page matched the query."""
    chunks = chunk_markdown(md)
    ranked = [c for c in rank_chunks(chunks, query) if c.score > 0]
    if not ranked:
        return truncate(md, max_tokens)[0], False
    picked, used = [], 0
    for c in ranked:
        cost = est_tokens(c.text) + 12
        if picked and used + cost > max_tokens:
            continue
        picked.append(c)
        used += cost
        if used >= max_tokens:
            break
    picked.sort(key=lambda c: c.index)
    parts, prev = [], None
    for c in picked:
        if prev is not None and c.index != prev + 1:
            parts.append("[...]")
        if c.heading and not c.text.startswith("#") and (prev is None or c.index != prev + 1):
            parts.append(f"({c.heading})")
        parts.append(c.text)
        prev = c.index
    return truncate("\n\n".join(parts), max_tokens)[0], True


def truncate(text: str, max_tokens: int, offset: int = 0) -> tuple:
    """Slice text to a token budget starting at character `offset`, cutting at a paragraph
    or line boundary. Returns (slice, next_offset or None)."""
    text = text[offset:] if offset else text
    limit = max_tokens * 4
    if len(text) <= limit:
        return text, None
    cut = text.rfind("\n\n", int(limit * 0.6), limit)
    if cut < 0:
        cut = text.rfind("\n", int(limit * 0.6), limit)
    if cut < 0:
        cut = text.rfind(" ", int(limit * 0.8), limit)
    if cut < 0:
        cut = limit
    return text[:cut].rstrip(), offset + cut
