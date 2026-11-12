"""Offline test suite: python -m unittest discover -s tests"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openresearch import Config, Fetcher, crawl, extract, map_site, read, read_many, research, search  # noqa: E402
from openresearch import mcp_server, rank  # noqa: E402
from importlib import import_module  # noqa: E402

S = import_module("openresearch.search")   # the package also exports a search() function
from openresearch.dom import parse, select  # noqa: E402
from openresearch.extract import extract_html  # noqa: E402
from openresearch.tools import TOOLS, call  # noqa: E402
from fixture_site import ARTICLE, Site  # noqa: E402


def make_fetcher(tmp, **kw):
    cfg = Config(cache_dir=tmp, allow_private=True, min_delay=0, wayback_fallback=False, render="never", **kw)
    return Fetcher(cfg)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = Site()
        cls.base = cls.site.base

    @classmethod
    def tearDownClass(cls):
        cls.site.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.f = make_fetcher(self.tmp.name)

    def tearDown(self):
        self.f.close()
        self.tmp.cleanup()


class TestDom(unittest.TestCase):
    def test_broken_html_and_selectors(self):
        doc = parse("<ul><li>one<li>two <b>bold</ul><p>para<p>next<div id=x class='a b'><a href='/k'>k</a></div>")
        self.assertEqual([n.text() for n in select(doc, "li")], ["one", "two bold"])
        self.assertEqual(len(select(doc, "p")), 2)
        self.assertEqual(select(doc, "div#x.a > a[href^='/']")[0].get("href"), "/k")
        self.assertEqual(len(select(doc, "li:nth-child(2), p:first-child")), 1)
        self.assertEqual(select(doc, "li:contains(two)")[0].text(), "two bold")
        self.assertEqual(len(select(doc, "div:not(.zzz)")), 1)


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.d = extract_html(ARTICLE, "http://news.example/article")
        self.md = self.d["markdown"]

    def test_metadata(self):
        self.assertEqual(self.d["title"], "Solar Storage Costs Fall | Example News")
        self.assertEqual(self.d["published"], "2026-03-14")
        self.assertEqual(self.d["author"], "Staff Writer")
        self.assertEqual(self.d["lang"], "en")

    def test_boilerplate_removed(self):
        for junk in ("Trending", "weird tricks", "cookies", "Copyright", "Tweet", "Other story", "tracking", "About"):
            self.assertNotIn(junk, self.md, junk)

    def test_structure_kept(self):
        self.assertIn("# Solar Storage Costs Fall", self.md)
        self.assertIn("## Why prices dropped", self.md)
        self.assertIn("**18 percent**", self.md)
        self.assertIn("[annual survey](http://news.example/reports/annual?utm_source=x)", self.md)
        self.assertIn("- Lithium carbonate fell by half\n- Factory scale doubled\n  - Mostly in Asia", self.md)
        self.assertIn("| Region | 2024 | 2025 |\n| --- | --- | --- |\n| Europe | $210 | $172 |", self.md)
        self.assertIn("```python\ndef payback(cost, saving):\n    return cost / saving\n```", self.md)
        self.assertIn("> Storage is now cheaper", self.md)

    def test_full_mode_keeps_everything(self):
        full = extract_html(ARTICLE, "http://news.example/article", mode="full")["markdown"]
        self.assertIn("Trending", full)
        self.assertNotIn("tracking", full)

    def test_links_none(self):
        md = extract_html(ARTICLE, "http://news.example/a", link_style="none")["markdown"]
        self.assertIn("annual survey", md)
        self.assertNotIn("](", md)

    def test_no_semantic_tags(self):
        html = ("<html><body><div class='menu'><a href='/a'>A</a><a href='/b'>B</a></div><div class='wrap'><div>"
                + "".join(f"<p>Paragraph {i} has enough words, commas, and substance to be scored as content.</p>"
                          for i in range(8)) + "</div></div><div class='footer'>Footer junk here</div></body></html>")
        md = extract_html(html)["markdown"]
        self.assertIn("Paragraph 7", md)
        self.assertNotIn("Footer junk", md)


class TestRank(unittest.TestCase):
    def test_focus_returns_relevant_passage_only(self):
        md = extract_html(ARTICLE, "http://news.example/article")["markdown"] + "\n\n" + "\n\n".join(
            f"## Filler {i}\n\n" + "Nothing relevant lives in this padding paragraph about gardening. " * 12
            for i in range(30))
        text, matched = rank.focus(md, "how many kilowatt-hours does a household need", 300)
        self.assertTrue(matched)
        self.assertIn("ten and fifteen kilowatt-hours", text)
        self.assertLess(rank.est_tokens(text), 340)
        self.assertLess(len(text), len(md) / 5)

    def test_focus_no_match(self):
        text, matched = rank.focus("# T\n\nalpha beta gamma", "zebra", 100)
        self.assertFalse(matched)

    def test_truncate_pagination(self):
        md = "\n\n".join(f"Paragraph number {i} " + "word " * 40 for i in range(50))
        first, nxt = rank.truncate(md, 200)
        self.assertIsNotNone(nxt)
        second, _ = rank.truncate(md, 200, nxt)
        self.assertTrue(md.startswith(first))
        self.assertEqual(md[nxt:nxt + len(second)], second)

    def test_dedupe(self):
        a = rank.Chunk("the quick brown fox jumps over the lazy dog near the river bank today")
        b = rank.Chunk("The quick brown fox jumps over the lazy dog near the river bank today!")
        c = rank.Chunk("completely different words about battery storage pricing in european markets")
        self.assertEqual(len(rank.dedupe([a, b, c])), 2)


class TestFetch(Base):
    def test_read_article(self):
        p = read(self.f, self.base + "/article")
        self.assertTrue(p.ok, p.error)
        self.assertEqual(p.published, "2026-03-14")
        self.assertIn("## Regional figures", p.content)
        self.assertIn("published 2026-03-14", p.render())

    def test_cache_and_fresh(self):
        read(self.f, self.base + "/docs/c")
        p = read(self.f, self.base + "/docs/c")
        self.assertEqual(p.via, "cache")
        before = self.site.hits["/docs/c"]
        read(self.f, self.base + "/docs/c", max_age=0)
        self.assertEqual(self.site.hits["/docs/c"], before + 1)

    def test_conditional_revalidation(self):
        r1 = self.f.fetch(self.base + "/etag")
        r2 = self.f.fetch(self.base + "/etag", max_age=0)
        self.assertTrue(r1.ok and r2.ok)
        self.assertTrue(r2.from_cache)             # server said 304; body came from the cache

    def test_redirect_retry_charset(self):
        self.assertTrue(read(self.f, self.base + "/redirect").final_url.endswith("/article"))
        self.assertTrue(read(self.f, self.base + "/flaky").ok)
        self.assertIn("café crème", read(self.f, self.base + "/latin1").markdown)

    def test_robots(self):
        p = read(self.f, self.base + "/private/secret")
        self.assertIn("robots.txt", p.error)
        f2 = make_fetcher(self.tmp.name + "2", respect_robots=False)
        self.assertTrue(read(f2, self.base + "/private/secret").ok)
        f2.close()

    def test_private_network_guard(self):
        f = Fetcher(Config(cache_dir=self.tmp.name + "3"))
        for url in (self.base + "/article", "http://localhost/x", "http://169.254.169.254/latest/meta-data",
                    "http://10.0.0.5/", "file:///etc/passwd", "http://[::1]/"):
            self.assertTrue(f.fetch(url).error, url)
        f.close()

    def test_other_formats(self):
        feed = read(self.f, self.base + "/feed.xml")
        self.assertIn("- [First post](http://x.example/1) - Mon, 02 Mar 2026\n  Hello world", feed.markdown)
        self.assertIn('"sizes"', read(self.f, self.base + "/data.json").markdown)

    def test_pdf(self):
        import importlib.util
        import shutil
        if not (importlib.util.find_spec("pypdf") or importlib.util.find_spec("pdfminer") or shutil.which("pdftotext")):
            self.skipTest("no PDF backend installed")
        pdf = read(self.f, self.base + "/paper.pdf")
        self.assertTrue(pdf.ok, pdf.error)
        self.assertIn("9000 units", pdf.markdown)

    def test_errors_do_not_raise(self):
        p = read(self.f, self.base + "/missing")
        self.assertFalse(p.ok)
        self.assertIn("404", p.error)
        self.assertIn("ERROR", p.render())

    def test_javascript_shell_is_reported_not_returned_empty(self):
        p = read(self.f, self.base + "/spa")
        self.assertFalse(p.ok)
        self.assertIn("JavaScript", p.error)

    def test_wayback_fallback(self):
        import openresearch.page as P
        self.f.cfg.wayback_fallback = True
        real_json, real_fetch = self.f.get_json, self.f.fetch
        self.f.get_json = lambda url, **kw: {"archived_snapshots": {"closest": {"available": True, "timestamp": "20250101000000"}}}
        self.f.fetch = lambda url, **kw: real_fetch(self.base + "/docs/b", **{k: v for k, v in kw.items() if k != "robots"}) \
            if "web.archive.org" in url else real_fetch(url, **kw)
        try:
            p = read(self.f, self.base + "/missing")
        finally:
            self.f.get_json, self.f.fetch = real_json, real_fetch
        self.assertTrue(p.ok, p.error)
        self.assertEqual((p.via, p.snapshot[:8]), ("wayback", "20250101"))
        self.assertIn("Internet Archive copy from 20250101", p.render())

    def test_read_many_keeps_order(self):
        pages = read_many(self.f, [self.base + "/docs/b", self.base + "/missing", self.base + "/docs/a"])
        self.assertEqual([p.title for p in pages], ["Docs: Configure", "", "Docs: Install"])


class TestCrawl(Base):
    def test_map(self):
        items = map_site(self.f, self.base + "/docs/a", search="configuration guide")
        urls = [i["url"] for i in items]
        self.assertEqual(urls[0], self.base + "/docs/b")
        self.assertIn(self.base + "/article", urls)
        self.assertEqual(next(i for i in items if i["url"].endswith("/article"))["lastmod"], "2026-01-01")
        self.assertFalse(any("elsewhere" in u or u.endswith(".png") for u in urls))

    def test_crawl_scope_and_robots(self):
        pages = crawl(self.f, self.base + "/docs/a", max_pages=20, delay=0)
        got = {p.url.replace(self.base, ""): p for p in pages}
        self.assertEqual({k for k, p in got.items() if p.ok}, {"/docs/a", "/docs/b", "/docs/c", "/blog/cats"})
        self.assertIn("robots.txt", got["/private/secret"].error)
        self.assertNotIn("/logo.png", got)

    def test_crawl_include_and_limit(self):
        pages = crawl(self.f, self.base + "/docs/a", include=["/docs/*"], delay=0)
        self.assertEqual({p.url.replace(self.base, "") for p in pages}, {"/docs/a", "/docs/b", "/docs/c"})
        self.assertEqual(len(crawl(self.f, self.base + "/docs/a", max_pages=2, delay=0)), 2)

    def test_focused_crawl_goes_to_relevant_pages_first(self):
        pages = crawl(self.f, self.base + "/docs/a", query="widget frequency configuration", max_pages=2,
                      workers=1, delay=0)
        self.assertEqual([p.url.replace(self.base, "") for p in pages], ["/docs/a", "/docs/b"])
        self.assertIn("42 hertz", pages[1].content)


class TestStructured(unittest.TestCase):
    def test_extract(self):
        d = extract(ARTICLE, "http://news.example/article", {
            "headline": "article h1", "first_link": "article a::attr(href)",
            "sections": {"selector": "article h2", "all": True},
            "rows": {"selector": "tbody tr", "all": True, "fields": {"region": "td:nth-child(1)", "y2025": "td:nth-child(3)"}},
        })
        self.assertEqual(d["data"]["headline"], "Solar Storage Costs Fall")
        self.assertEqual(d["data"]["first_link"], "http://news.example/reports/annual?utm_source=x")
        self.assertEqual(len(d["data"]["sections"]), 3)
        self.assertEqual(d["data"]["rows"][1], {"region": "Asia", "y2025": "$123"})
        self.assertEqual(d["tables"][0]["records"][0], {"Region": "Europe", "2024": "$210", "2025": "$172"})
        self.assertEqual(d["tables"][0]["caption"], "Price per kWh")
        self.assertEqual(d["jsonld"][0]["headline"], "Solar Storage Costs Fall")
        self.assertEqual(d["meta"]["author"], "Staff Writer")


# Markup below is modelled on each engine's result page; it is not a live capture.
DDG_HTML = """<div class="result results_links results_links_deep web-result"><div class="links_main">
<h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fpage%3Fa%3D1%26utm_source%3Dddg&amp;rut=abc">Example <b>Page</b></a></h2>
<a class="result__snippet" href="#">A <b>snippet</b> here.</a></div></div>
<div class="result result--ad"><a class="result__a" href="//duckduckgo.com/y.js?ad=1">Ad</a></div>
<div class="result"><a class="result__a" href="https://direct.example/x">Direct</a></div>"""
DDG_LITE = """<table><tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Flite.example%2F" class="result-link">Lite one</a></td></tr>
<tr><td class="result-snippet">Lite snippet</td></tr></table>"""
MOJEEK = """<ul class="results-standard"><li><a class="ob" href="https://m.example/a">m.example</a>
<h2><a class="title" href="https://m.example/a">Mojeek title</a></h2><p class="s">Mojeek snippet</p></li></ul>"""


class TestSearch(Base):
    def test_parsers(self):
        r = S.parse_ddg_html(DDG_HTML)
        self.assertEqual([(x.title, x.url) for x in r],
                         [("Example Page", "https://example.org/page?a=1&utm_source=ddg"), ("Direct", "https://direct.example/x")])
        self.assertEqual(r[0].snippet, "A snippet here.")
        lite = S.parse_ddg_lite(DDG_LITE)
        self.assertEqual((lite[0].title, lite[0].url, lite[0].snippet), ("Lite one", "https://lite.example/", "Lite snippet"))
        m = S.parse_mojeek(MOJEEK)
        self.assertEqual((m[0].title, m[0].url, m[0].snippet), ("Mojeek title", "https://m.example/a", "Mojeek snippet"))

    def _fake_engines(self):
        base = self.base

        def one(f, q, n, o):
            return [S.Result("Solar", base + "/article?utm_source=a", "short", "one"),
                    S.Result("Install", base + "/docs/a", "", "one"),
                    S.Result("Spam", "https://spam.example/x", "", "one")]

        def two(f, q, n, o):
            return [S.Result("Configure", base + "/docs/b", "", "two"),
                    S.Result("Solar story", base + "/article", "a longer snippet", "two"),
                    S.Result("Cats", base + "/blog/cats", "", "two")]

        def broken(f, q, n, o):
            raise RuntimeError("engine down")

        return {"one": one, "two": two, "broken": broken}

    def test_fusion(self):
        old = dict(S.ENGINES)
        S.ENGINES.update(self._fake_engines())
        try:
            results, report = search(self.f, "x", 10, engines=["one", "two", "broken"],
                                     exclude_domains=["spam.example"], per_domain=10)
        finally:
            S.ENGINES.clear()
            S.ENGINES.update(old)
        self.assertEqual(results[0].url, self.base + "/article")       # found by both engines -> first
        self.assertEqual(results[0].engines, ["one", "two"])
        self.assertEqual(results[0].snippet, "a longer snippet")
        self.assertEqual(len(results), 4)
        self.assertEqual(report["one"], 3)
        self.assertIn("engine down", report["broken"])
        self.assertIn("FAILED", S.format_results(results, report))

    def test_research_end_to_end(self):
        old = dict(S.ENGINES)
        S.ENGINES.update(self._fake_engines())
        self.f.cfg.engines = ["one", "two", "broken"]
        try:
            out = research(self.f, "what widget frequency should I set", depth="quick",
                           queries=["widget frequency", "widget settings"])
        finally:
            S.ENGINES.clear()
            S.ENGINES.update(old)
        text = out["text"]
        self.assertIn("42 hertz", text)
        self.assertIn("## Sources", text)
        self.assertIn("[1] Docs: Configure", text)                     # best passage's source is numbered first
        self.assertIn("broken FAILED", text)
        self.assertIn("Could not read: https://spam.example/x", text)
        self.assertNotIn("Cats sleep", text)                           # irrelevant page contributes nothing
        self.assertLess(rank.est_tokens(text), 3200)


class TestMcp(Base):
    def setUp(self):
        super().setUp()
        self.srv = mcp_server.Server(self.f)

    def test_handshake_and_tools(self):
        r = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = self.srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"research", "search", "fetch", "crawl", "map", "extract"})
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            json.dumps(t)
        self.assertEqual(self.srv.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"], -32601)

    def test_tool_calls(self):
        def tool(name, **args):
            return self.srv.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                    "params": {"name": name, "arguments": args}})["result"]
        r = tool("fetch", urls=[self.base + "/article", self.base + "/missing"], query="lithium price")
        self.assertFalse(r["isError"])
        self.assertIn("Lithium carbonate", r["content"][0]["text"])
        self.assertIn("ERROR: HTTP 404", r["content"][0]["text"])
        self.assertIn("/docs/c", tool("map", url=self.base + "/")["content"][0]["text"])
        self.assertIn("Crawled 3 pages", tool("crawl", url=self.base + "/docs/a", include=["/docs/*"])["content"][0]["text"])
        ex = json.loads(tool("extract", url=self.base + "/article", selectors={"h": "h1"})["content"][0]["text"])
        self.assertEqual(ex["data"]["h"], "Solar Storage Costs Fall")
        self.assertTrue(tool("fetch")["isError"])
        self.assertTrue(tool("bogus")["isError"])

    def test_stdio_transport(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "fetch", "arguments": {"urls": [self.base + "/docs/b"]}}}]
        out = io.BytesIO()
        out.close = lambda: None
        mcp_server.serve_stdio(self.srv, io.BytesIO(("\n".join(json.dumps(m) for m in msgs) + "\nnot json\n").encode()), out)
        replies = [json.loads(line) for line in out.getvalue().decode().splitlines()]
        by_id = {r["id"]: r for r in replies}
        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "openresearch")
        self.assertIn("42 hertz", by_id[2]["result"]["content"][0]["text"])
        self.assertEqual(by_id[None]["error"]["code"], -32700)

    def test_http_transport_with_token(self):
        import threading
        http = mcp_server.serve_http("127.0.0.1", 0, "s3cret", self.srv)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{http.server_port}/mcp"
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()

        def post(u, headers=None):
            req = urllib.request.Request(u, data=body, headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None
        try:
            self.assertEqual(post(url)[0], 404)
            self.assertEqual(post(url + "/wrong")[0], 404)
            self.assertEqual(len(post(url, {"Authorization": "Bearer s3cret"})[1]["result"]["tools"]), 6)
            self.assertEqual(post(url + "/s3cret")[0], 200)
        finally:
            http.shutdown()
            http.server_close()


class TestCli(Base):
    def test_cli_fetch_and_extract(self):
        from contextlib import redirect_stdout
        from openresearch.cli import main
        os.environ["OPENRESEARCH_CACHE_DIR"] = self.tmp.name
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--allow-private", "fetch", self.base + "/article", "--query", "inverter sizing", "--max-tokens", "200"])
        self.assertEqual(code, 0)
        self.assertIn("sizing the inverter", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--allow-private", "extract", self.base + "/article", "-s", "heads*=article h2", "--want", "meta"])
        self.assertEqual(len(json.loads(buf.getvalue())["data"]["heads"]), 3)
        with tempfile.TemporaryDirectory() as out, redirect_stdout(io.StringIO()):
            main(["--allow-private", "crawl", self.base + "/docs/a", "--include", "/docs/*", "--out", out])
            files = sorted(os.listdir(out))
            self.assertEqual(len(files), 4)
            self.assertIn("index.jsonl", files)
        del os.environ["OPENRESEARCH_CACHE_DIR"]


if __name__ == "__main__":
    unittest.main()
