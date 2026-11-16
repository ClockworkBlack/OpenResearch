"""Offline test suite: python -m unittest discover -s tests"""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
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


    def test_utility_hidden_classes_kept(self):
        body = "Release notes: added a feature, fixed a bug, and improved startup time for large projects. "
        html = ("<html><body><div class='wrap'>"
                + "".join(f"<div class='update'><span>Day {i}</span><div class='flex-1 overflow-hidden'>"
                          f"<p>{body}Item {i}.</p></div></div>" for i in range(6))
                + "<div class='hidden md:block'><p>Shown on wide screens, with plenty of words here.</p></div>"
                + "<div class='hidden'><p>Really hidden text that never shows.</p></div></div></body></html>")
        md = extract_html(html)["markdown"]
        self.assertIn("Item 0.", md)
        self.assertIn("Item 5.", md)
        self.assertIn("Shown on wide screens", md)
        self.assertNotIn("Really hidden", md)

    def test_react_streamed_segments(self):
        html = ('<html><head><title>Streamed</title></head><body><main><h1>Streamed article</h1>'
                '<!--$?--><template id="B:0"></template><div role="status"><span>Loading</span></div><!--/$-->'
                '</main><div hidden id="S:0"><div class="intro"><p>Intro paragraph that the server streamed '
                'in later, with enough words to count as real content.</p></div><template id="P:1"></template>'
                '</div><script>$RC("B:0","S:0")</script><div hidden id="S:1"><div class="article-body"><p>Body '
                'paragraph from a nested segment, also long enough to be kept as content.</p></div></div>'
                '</body></html>')
        md = extract_html(html, "https://x.example/a")["markdown"]
        self.assertIn("Intro paragraph", md)
        self.assertIn("Body paragraph from a nested segment", md)
        self.assertNotIn("Loading", md)
        self.assertEqual(md.count("Intro paragraph"), 1)
        full = extract_html(html, "https://x.example/a", mode="full")["markdown"]
        self.assertEqual(full.count("Body paragraph"), 1)


    def test_dates(self):
        from openresearch.extract import norm_date
        for raw, want in (("2017/06/12", "2017-06-12"), ("12 Jun 2017", "2017-06-12"), ("June 12, 2017", "2017-06-12"),
                          ("2026-11-13T02:00:00Z", "2026-11-13"), ("May 2026", ""), ("13/45/2026", "")):
            self.assertEqual(norm_date(raw), want, raw)
        page = "<html><head>{}</head><body><main><p>" + "Words, words and more words. " * 20 + "</p>{}</main></body></html>"
        meta = extract_html(page.format('<meta name="citation_online_date" content="2023/08/02">'
                                        '<meta name="citation_date" content="2017/06/12">', ""))
        self.assertEqual(meta["published"], "2017-06-12")
        ld = '<script type="application/ld+json">[{"@type":"Article","datePublished":"3 March 2024"}]</script>'
        self.assertEqual(extract_html(page.format(ld, ""))["published"], "2024-03-03")
        self.assertEqual(extract_html(page.format("", ""), "https://x.example/2025/01/09/post")["published"], "2025-01-09")
        listing = '<time datetime="2026-01-01">a</time><time datetime="2026-02-02">b</time>'
        self.assertEqual(extract_html(page.format("", listing))["published"], "")       # a listing has no one date

    def test_markdown_cleanups(self):
        para = "<p>Real paragraph text with enough words, commas, and substance to be kept as content.</p>"
        toc = ("<ul><li><a href='#one'>Section one</a></li><li><a href='#two'>Section two</a></li>"
               "<li><a href='#three'>Section three</a></li></ul>")
        html = ("<html><body><main>" + toc + "<h2>Section one<a class='headerlink' href='#one'>¶</a></h2>" + para * 3
                + "<dl><dt>setting<a href='#setting'>¶</a></dt><dd>What it does.</dd></dl>"
                + "<ul><li>A promo line long enough to matter, shown for desktop and for mobile</li></ul>" * 2
                + "<table><tr><td>1.</td><td></td><td>First story</td></tr><tr><td>2.</td><td></td>"
                  "<td>Second story</td></tr></table></main></body></html>")
        md = extract_html(html)["markdown"]
        self.assertNotIn("Section three", md)                # jump-link table of contents
        self.assertNotIn("¶", md)
        self.assertIn("## Section one\n", md)
        self.assertEqual(md.count("A promo line"), 1)        # back-to-back duplicate block
        self.assertIn("| 1. | First story |\n| --- | --- |\n| 2. | Second story |", md)   # spacer column gone


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
                    "http://10.0.0.5/", "file:///etc/passwd", "http://[::1]/", "http://100.64.0.1/",
                    "http://[::ffff:10.0.0.5]/", "http://[::ffff:169.254.169.254]/"):
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

    def test_blocked_pages_say_why_and_fail_fast(self):
        p = read(self.f, self.base + "/challenge")
        self.assertIn("HTTP 403", p.error)
        self.assertIn("browser check", p.error)
        r = self.f.fetch(self.base + "/busy")
        self.assertIn("rate limited", r.error)
        self.assertEqual(self.site.hits["/busy"], 1)          # Retry-After: 1 hour is not worth waiting for
        self.f.fetch(self.base + "/down", retries=0)
        self.assertEqual(self.site.hits["/down"], 1)

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

    def test_map_explores_when_start_page_lacks_the_answer(self):
        with mock.patch("openresearch.crawl.sitemap_urls", return_value=[]):
            urls = [i["url"] for i in map_site(self.f, self.base + "/nomap/", search="html parser")]
            self.assertEqual(urls[0], self.base + "/nomap/library/html.parser")    # found one level down
            only_start = [i["url"] for i in map_site(self.f, self.base + "/nomap/", search="html parser", explore=0)]
        self.assertNotIn(self.base + "/nomap/library/html.parser", only_start)

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
    def test_table_spans(self):
        html = ("<table><tr><th rowspan='2'>Name</th><th colspan='2'>Height</th><th rowspan='2'>City</th></tr>"
                "<tr><th>m</th><th>ft</th></tr>"
                "<tr><td>Alpha</td><td>828</td><td>2,717</td><td rowspan='2'>Dubai</td></tr>"
                "<tr><td>Beta</td><td>400</td><td>1,312</td></tr></table>")
        t = import_module("openresearch.structured").extract(html, want=("tables",))["tables"][0]
        self.assertEqual(t["header"], ["Name", "Height m", "Height ft", "City"])
        self.assertEqual(t["records"][1], {"Name": "Beta", "Height m": "400", "Height ft": "1,312", "City": "Dubai"})

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

    def test_bing_news_feed(self):
        feed = (b'<rss version="2.0" xmlns:News="https://www.bing.com/news/search"><channel>'
                b'<item><title>Widget sales &amp; profits rise</title><link>http://www.bing.com/news/apiclick.aspx?'
                b'ref=FexRss&amp;url=https%3a%2f%2fnews.example%2fwidgets%3fa%3d1&amp;c=1</link>'
                b'<description>Sales rose.</description><pubDate>Thu, 12 Nov 2026 12:17:00 GMT</pubDate>'
                b'<News:Source>Example News</News:Source></item>'
                b'<item><title>Old story</title><link>https://old.example/x</link>'
                b'<pubDate>Mon, 01 Jan 2001 00:00:00 GMT</pubDate></item></channel></rss>')
        rows = S.parse_bingnews(feed)
        self.assertEqual((rows[0].title, rows[0].url, rows[0].date, rows[0].extra),
                         ("Widget sales & profits rise", "https://news.example/widgets?a=1", "2026-11-12", "Example News"))
        self.assertEqual(len(rows), 2)
        self.assertNotIn("Old story", [r.title for r in S.parse_bingnews(feed, days=7)])
        with self.assertRaises(RuntimeError):
            S.parse_bingnews(b"<html>not a feed")
        self.assertEqual(S.engines_for(self.f, "news")[0], "bingnews")

    def test_tavily(self):
        with mock.patch.object(S, "_TAVILY_URL", self.base + "/tavily"):
            with self.assertRaises(RuntimeError) as cm:
                S.tavily(self.f, "widgets", 5, {})
            self.assertIn("no TAVILY_API_KEY", str(cm.exception))
            self.assertNotIn("tavily", S.engines_for(self.f, "web"))
            self.f.cfg.tavily_api_key = "tvly-good"
            self.assertEqual(S.engines_for(self.f, "web")[0], "tavily")           # used first once a key is set
            rows = S.tavily(self.f, "widgets site:w.example", 5, {"time": "week"})
            self.assertEqual((rows[0].title, rows[0].url, rows[0].date), ("Widget guide", "https://w.example/guide", "2026-11-12"))
            path, auth, body = self.site.posts[-1]
            self.assertEqual(body, {"query": "widgets", "max_results": 5, "search_depth": "basic",
                                    "include_domains": ["w.example"], "time_range": "week"})
            self.f.cfg.tavily_api_key = "tvly-bad"
            with self.assertRaises(RuntimeError) as cm:
                S.tavily(self.f, "other widgets", 5, {})
            self.assertIn("401", str(cm.exception))

    def test_ddg_is_a_fallback_once_a_keyed_engine_is_set(self):
        self.assertEqual(S.engines_for(self.f, "web"), ["ddg", "wikipedia"])     # no key: ddg is still a default
        self.f.cfg.tavily_api_key = "tvly-good"
        self.assertEqual(S.engines_for(self.f, "web"), ["tavily", "wikipedia"])
        self.assertEqual(S.engines_for(self.f, "news"), ["tavily", "bingnews", "hackernews"])
        self.assertIn("ddg", S.engines_for(self.f, "code"))                      # no keyed engine serves code
        self.assertEqual(S.engines_for(self.f, "web", ["ddg"]), ["ddg"])         # named by the caller
        asked = []

        def engine(name, rows):
            def run(f, q, n, o):
                asked.append(name)
                if rows is None:
                    raise RuntimeError("down")
                return [S.Result(f"{name} widget {i}", f"https://{name}{i}.example/w", "widget frequency", name)
                        for i in range(rows)]
            return run

        old = dict(S.ENGINES)
        try:
            S.ENGINES.update(tavily=engine("tavily", 5), wikipedia=engine("wikipedia", 1), ddg=engine("ddg", 3),
                             bingnews=engine("bingnews", 0), hackernews=engine("hackernews", 0))
            results, report = search(self.f, "widget frequency")
            self.assertNotIn("ddg", asked)                                       # enough without it
            self.assertNotIn("ddg", report)
            asked.clear()
            S.ENGINES["tavily"] = engine("tavily", None)                         # Tavily down: ddg steps in
            results, report = search(self.f, "widget frequency")
            self.assertIn("ddg", asked)
            self.assertEqual(report["ddg"], 3)
            self.assertIn("down", report["tavily"])
            self.assertTrue(any(r.engines == ["ddg"] for r in results))
        finally:
            S.ENGINES.clear()
            S.ENGINES.update(old)

    def test_guide(self):
        overview = call(self.f, "guide", {})
        self.assertIn("Five rules", overview)
        self.assertIn("vertical `web` asks: ddg, wikipedia", call(self.f, "guide", {"topic": "engines"}))
        self.f.cfg.tavily_api_key = "tvly-x"                                   # reflects this server's settings
        engines = call(self.f, "guide", {"topic": "engines"})
        self.assertIn("vertical `web` asks: tavily, wikipedia; fallbacks when those fail or find little: ddg", engines)
        self.assertIn("Keyed engines configured on this server: tavily", engines)
        everything = call(self.f, "guide", {"topic": "all"})
        for heading in ("# Writing queries", "# Recipes", "# When results disappoint"):
            self.assertIn(heading, everything)
        with self.assertRaises(ValueError):
            call(self.f, "guide", {"topic": "nonsense"})
        self.assertEqual(self.f.log.recent(5), [])                             # reading the guide is not logged
        r = mcp_server.Server(self.f).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": "guide", "arguments": {"topic": "queries"}}})
        self.assertIn("Keywords, not sentences", r["result"]["content"][0]["text"])

    def test_mojeek_is_opt_in(self):
        self.assertNotIn("mojeek", S.engines_for(self.f, "web"))
        self.assertEqual(S.engines_for(self.f, "web", ["mojeek"]), ["mojeek"])

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

    def test_engine_errors_name_the_cause(self):
        with self.assertRaises(RuntimeError) as cm:
            S._json(self.f, self.base + "/busy")
        self.assertIn("429", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            S._json(self.f, self.base + "/article")
        self.assertIn("not JSON", str(cm.exception))

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
        self.assertIn("Could not read:\n- https://spam.example/x", text)
        self.assertNotIn("Cats sleep", text)                           # irrelevant page contributes nothing
        self.assertLess(rank.est_tokens(text), 3200)


    def test_results_for_a_different_search_are_caught(self):
        q = "heat pump cold climate COP field study"
        junk = [S.Result("Heat (1995 film) - Wikipedia", "https://en.wikipedia.org/wiki/Heat_(1995_film)", "LAPD detective"),
                S.Result("Miami Heat Scores - ESPN", "https://www.espn.com/nba/team/_/name/mia/miami-heat", "live scores"),
                S.Result("Heat (1995) - IMDb", "https://www.imdb.com/title/tt0113277/", "Directed by Michael Mann"),
                S.Result("Heat - Wikipedia", "https://en.wikipedia.org/wiki/Heat", "energy in transfer")]
        good = [S.Result("Cold climate heat pump field study", "https://nrel.example/report", "measured COP at -15C"),
                S.Result("Air source heat pump", "https://en.wikipedia.org/wiki/Air_source_heat_pump", "COP in cold weather")]
        self.assertTrue(S.off_topic(q, junk))
        self.assertFalse(S.off_topic(q, good))
        self.assertFalse(S.off_topic(q, good + junk[:2]))
        self.assertFalse(S.off_topic("heat", junk))                    # too short to judge
        self.assertFalse(S.off_topic("ヒートポンプ 寒冷地 性能 調査", junk))   # no word boundaries to compare
        self.assertFalse(S.off_topic("python asyncio site:docs.python.org -tutorial", [
            S.Result("asyncio", "https://docs.python.org/3/library/asyncio.html", "Python library")]))

        old = dict(S.ENGINES)
        S.ENGINES.update({"junk": lambda f, q, n, o: list(junk), "good": lambda f, q, n, o: list(good)})
        try:                                   # the engine listing junk first must not put it on top
            results, _ = search(self.f, q, 6, engines=["junk", "good"])
        finally:
            S.ENGINES.clear()
            S.ENGINES.update(old)
        self.assertEqual({r.domain for r in results[:2]}, {"nrel.example", "en.wikipedia.org"})
        self.assertIn("Air_source", results[1].url + results[0].url)

        fake = mock.Mock()                     # the ddgs library answering for the first word only
        fake.DDGS.return_value.text.return_value = [{"title": r.title, "href": r.url, "body": r.snippet} for r in junk]
        with mock.patch.dict(sys.modules, {"ddgs": fake}), \
                mock.patch.object(self.f, "fetch", return_value=mcp_server.Fetcher.fetch(self.f, "ftp://x")):
            with self.assertRaises(RuntimeError):
                S.ddg(self.f, q, 5, {})
        self.assertEqual(fake.DDGS.return_value.text.call_args.kwargs["region"], "us-en")

    def test_research_widens_a_search_that_found_almost_nothing(self):
        base, asked = self.base, []

        def picky(f, q, n, o):                 # like a real engine: nothing for a long sentence
            asked.append(q)
            if len(q.split()) > 3:
                return []
            return [S.Result("Configure", base + "/docs/b", "", "picky"), S.Result("Solar", base + "/article", "", "picky"),
                    S.Result("Elsewhere", "http://localhost:1/x", "", "picky")]
        old = dict(S.ENGINES)
        S.ENGINES["picky"] = picky
        self.f.cfg.engines = ["picky"]
        try:
            out = research(self.f, "what widget frequency should I set", depth="quick",
                           queries=["what is the right widget frequency to set"])
        finally:
            S.ENGINES.clear()
            S.ENGINES.update(old)
        self.assertIn("right widget frequency set", asked)             # the same query as plain keywords
        self.assertIn("42 hertz", out["text"])
        self.assertIn("| right widget frequency set", out["text"])     # the reply says what was searched

    def test_research_replaces_failed_pages(self):
        base, dead = self.base, "http://127.0.0.1:1/gone"

        def flaky(f, q, n, o):
            return [S.Result("Gone", dead, "The widget frequency is 42 hertz by default.", "flaky", "2026-01-02"),
                    S.Result("Configure", base + "/docs/b", "", "flaky", "2026-02-03")]

        old = dict(S.ENGINES)
        S.ENGINES["flaky"] = flaky
        self.f.cfg.engines = ["flaky"]
        self.f.cfg.retries = 0
        try:
            out = research(self.f, "widget frequency", queries=["widget frequency"], max_pages=1)
        finally:
            S.ENGINES.clear()
            S.ENGINES.update(old)
        text = out["text"]
        self.assertIn("42 hertz in the settings file", text)            # the next result took the dead page's slot
        self.assertIn("2026-02-03 (date from search listing)", text)    # undated page borrows the listing's date
        self.assertIn(f"- {dead} (", text)
        self.assertIn('search snippet: "The widget frequency is 42 hertz by default."', text)
        self.assertEqual(out["unread"], [])


class TestMcp(Base):
    def setUp(self):
        super().setUp()
        self.srv = mcp_server.Server(self.f)

    def test_handshake_and_tools(self):
        r = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = self.srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"research", "search", "fetch", "crawl", "map", "extract", "log", "guide"})
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["annotations"]["readOnlyHint"])
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
            self.assertEqual(len(post(url, {"Authorization": "Bearer s3cret"})[1]["result"]["tools"]), 8)
            self.assertEqual(post(url + "/s3cret")[0], 200)
        finally:
            http.shutdown()
            http.server_close()


    def test_http_rejected_request_does_not_poison_keepalive(self):
        import http.client
        srv = mcp_server.serve_http("127.0.0.1", 0, "goodtoken", self.srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            statuses = []
            for path in ("/mcp/wrong", "/mcp/goodtoken", "/mcp", "/mcp/goodtoken"):
                c.request("POST", path, body, {"Content-Type": "application/json"})
                r = c.getresponse()
                r.read()
                statuses.append(r.status)
            self.assertEqual(statuses, [404, 200, 404, 200])      # same connection throughout
        finally:
            srv.shutdown()
            srv.server_close()


    def _serve(self, **kw):
        srv = mcp_server.serve_http("127.0.0.1", 0, kw.pop("token", ""), self.srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def post(path, method="ping", headers=None, **params):
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}{path}", data=body,
                                         headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with opener.open(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                e.read()
                return e.code, None
        post.port = srv.server_port
        return post

    def test_web_pages_cannot_drive_a_local_server(self):
        post = self._serve()                                           # no secret: this machine only
        self.assertEqual(post("/mcp")[0], 200)
        self.assertEqual(post("/mcp", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(post("/mcp", headers={"Origin": "null"})[0], 403)
        self.assertEqual(post("/mcp", headers={"Origin": "http://localhost:6274"})[0], 200)
        self.assertEqual(post("/mcp", headers={"Origin": "http://[::1]:6274"})[0], 200)
        post = self._serve(token="s3cret")                             # a secret is proof enough
        self.assertEqual(post("/mcp/s3cret", headers={"Origin": "https://claude.ai"})[0], 200)
        import socket                                                  # a path that is not ASCII is refused, not a crash
        with socket.create_connection(("127.0.0.1", post.port), timeout=10) as raw:
            raw.sendall("POST /mcp/caf\u00e9 HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}".encode())
            self.assertIn(b" 404 ", raw.recv(200))


class TestLog(Base):
    """The per-call log, and the time limits that keep a call from hanging."""

    def test_call_is_logged_with_its_problems(self):
        text = call(self.f, "fetch", {"urls": [self.base + "/article", self.base + "/missing"]})
        self.assertIn("Solar Storage", text)
        entry = self.f.log.recent(1)[0]
        self.assertEqual((entry["tool"], entry["status"]), ("fetch", "ok"))
        self.assertGreaterEqual(entry["problems"], 1)
        pages = {e["url"]: e for e in entry["events"] if e["kind"] == "page"}
        self.assertIn("HTTP 404", pages[self.base + "/missing"]["error"])
        self.assertGreater(pages[self.base + "/article"]["chars"], 100)
        listing = call(self.f, "log", {})
        self.assertIn(entry["id"], listing)
        detail = call(self.f, "log", {"id": entry["id"]})
        self.assertIn("/missing -> FAILED - HTTP 404", detail)
        self.assertIn("/article -> read", detail)
        only = call(self.f, "log", {"id": entry["id"], "problems_only": True})
        self.assertNotIn("/article -> read", only)
        self.assertIn("No call with id", call(self.f, "log", {"id": "nope"}))
        self.assertEqual(len(self.f.log.recent(5)), 1)                 # looking at the log is not itself logged

    def test_crash_is_captured(self):
        with mock.patch("openresearch.tools.research", side_effect=ZeroDivisionError("boom")):
            with self.assertRaises(ZeroDivisionError):
                call(self.f, "research", {"question": "anything"})
        entry = self.f.log.recent(1)[0]
        self.assertEqual(entry["status"], "error")
        self.assertIn("ZeroDivisionError: boom", entry["error"])
        self.assertIn("Traceback", call(self.f, "log", {"id": entry["id"]}))
        self.assertEqual(len(self.f.log.recent(5, problems_only=True)), 1)

    def test_log_keeps_only_the_newest(self):
        from openresearch import trace
        log = trace.Log(self.tmp.name + "/small", keep=3)
        for i in range(6):
            t = trace.Trace("search", {"query": f"q{i}"})
            t.finish("ok")
            log.save(t)
        self.assertEqual([e["args"]["query"] for e in log.recent(10)], ["q5", "q4", "q3"])
        log.close()

    def test_hung_engine_is_not_waited_for(self):
        import time

        def fast(f, q, n, o):
            return [S.Result("Widget guide", "https://a.example/widgets", "widget frequency", "fast")]

        def hung(f, q, n, o):
            time.sleep(4)
            return []

        old = dict(S.ENGINES)
        S.ENGINES.update(fast=fast, hung=hung)
        self.f.cfg.engine_timeout = 0.5
        try:
            t0 = time.monotonic()
            results, report = search(self.f, "widget frequency", engines=["fast", "hung"])
        finally:
            S.ENGINES.clear()
            S.ENGINES.update(old)
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual([r.url for r in results], ["https://a.example/widgets"])
        self.assertIn("no answer within", report["hung"])

    def test_backup_engines_step_in(self):
        def empty(f, q, n, o):
            raise RuntimeError("throttled")

        def backup(f, q, n, o):
            return [S.Result("Widget news", "https://news.example/widgets", "widget frequency", "bingnews")]

        old = dict(S.ENGINES)
        S.ENGINES.update(ddg=empty, wikipedia=lambda f, q, n, o: [], bingnews=backup, hackernews=lambda f, q, n, o: [])
        try:
            results, report = search(self.f, "widget frequency")
            named, named_report = search(self.f, "widget frequency", engines=["ddg"])
        finally:
            S.ENGINES.clear()
            S.ENGINES.update(old)
        self.assertEqual([r.url for r in results], ["https://news.example/widgets"])
        self.assertIn("throttled", report["ddg"])
        self.assertEqual(report["bingnews"], 1)
        self.assertEqual((named, list(named_report)), ([], ["ddg"]))      # engines named by the caller: no backups

    def test_time_limit_returns_what_there_is(self):
        import time
        self.f.cfg.tool_timeout = 2.0
        t0 = time.monotonic()
        text = call(self.f, "research", {"question": "solar storage battery prices", "queries": [],
                                         "urls": [self.base + "/article", self.base + "/slow"]})
        self.assertLess(time.monotonic() - t0, 2.9)                      # /slow takes 3 seconds
        self.assertIn("18 percent", text)                                # the page that was ready is used
        self.assertIn("Stopped at the time limit", text)
        self.assertIn("/slow (skipped: this call's time limit", text)
        entry = self.f.log.recent(1)[0]
        self.assertTrue(any(e["kind"] == "timeout" for e in entry["events"]))

    def test_log_is_copied_to_github(self):
        import base64
        from openresearch.logship import GitHubLogShipper, repo_name
        self.assertEqual(repo_name("https://github.com/ClockworkBlack/LogView.git"), "ClockworkBlack/LogView")
        self.assertEqual(repo_name("ClockworkBlack/LogView"), "ClockworkBlack/LogView")
        self.assertEqual(repo_name("not a repo"), "")
        cfg = Config(cache_dir=self.tmp.name + "/gh", allow_private=True, min_delay=0, wayback_fallback=False,
                     render="never", log_github_repo="https://github.com/me/LogView.git", log_github_token="tok",
                     log_github_api=self.base)
        f = Fetcher(cfg)
        self.site.puts.clear()
        call(f, "fetch", {"urls": [self.base + "/article"]})
        call(f, "fetch", {"urls": [self.base + "/missing"]})
        self.assertEqual(self.site.puts, [])                              # batched: nothing sent yet
        self.assertIn("2 waiting", call(f, "log", {}))
        f.close()                                                         # shutting down sends what is waiting
        (path, auth, body), = self.site.puts
        self.assertRegex(path, r"^/repos/me/LogView/contents/openresearch/\d{4}-\d\d-\d\d/\d{6}-fetch-\w{8}-and-1-more\.md$")
        self.assertEqual(auth, "Bearer tok")
        text = base64.b64decode(body["content"]).decode()
        self.assertIn("/article -> read", text)
        self.assertIn("/missing -> FAILED - HTTP 404", text)
        self.assertIn("1 problem(s)", body["message"])

        entry = {"id": "abcd1234", "tool": "search", "status": "ok", "duration": 1.0, "problems": 0,
                 "started": 1_800_000_000.0, "args": {"query": "q"}, "events": []}
        down = GitHubLogShipper("me/LogView", "tok", api="http://127.0.0.1:1", delay=60)
        down.add(entry)
        self.assertFalse(down.flush())                                    # GitHub unreachable: the call is kept
        self.assertIn("1 waiting", down.status())
        self.assertIn("could not reach GitHub", down.status())
        down.api = self.base
        self.assertTrue(down.flush())
        self.assertIn("1 call(s) saved", down.status())
        denied = GitHubLogShipper("me/denied", "tok", api=self.base, delay=60)
        denied.add(entry)
        denied.flush()
        self.assertIn("may not write", denied.status())
        off = GitHubLogShipper("me/LogView", "", api=self.base)
        off.add(entry)
        self.assertEqual(off.status(), "off")

    def test_engine_that_keeps_hanging_is_rested(self):
        import time
        calls = []

        def fast(f, q, n, o):
            return [S.Result("Widget guide", "https://a.example/widgets", "widget frequency", "fast")]

        def hung(f, q, n, o):
            calls.append(q)
            time.sleep(3.6)
            return []

        old, old_web = dict(S.ENGINES), S.VERTICALS["web"]
        S.ENGINES.update(fast=fast, hung=hung)
        S.VERTICALS["web"] = ["fast", "hung"]
        self.f.cfg.engine_timeout = 3.1
        try:
          with mock.patch.dict(S.BACKUP, {}, clear=True):                 # keep the test offline
              search(self.f, "widget frequency")
              search(self.f, "widget frequency")                          # second time-out in a row
              t0 = time.monotonic()
              results, report = search(self.f, "widget frequency")          # third call: not asked, not waited for
              self.assertLess(time.monotonic() - t0, 1)
              self.assertEqual(len(calls), 2)
              self.assertIn("skipped: no answer the last 2 times", report["hung"])
              self.assertEqual(len(results), 1)
              search(self.f, "widget frequency", engines=["hung", "fast"])  # named by the caller: always asked
              self.assertEqual(len(calls), 3)
        finally:
            S.ENGINES.clear()
            S.ENGINES.update(old)
            S.VERTICALS["web"] = old_web

    def test_research_entry_measures_its_work(self):
        def one(f, q, n, o):
            return [S.Result("Configure", self.base + "/docs/b", "widget frequency 42 hertz", "one"),
                    S.Result("Gone", self.base.replace("127.0.0.1", "localhost") + "/missing",
                             "widget frequency page", "one")]

        def two(f, q, n, o):
            return [S.Result("Configure", self.base + "/docs/b", "widget frequency", "two"),
                    S.Result("Cats", self.base + "/blog/cats", "widget cats", "two")]

        old = dict(S.ENGINES)
        S.ENGINES.update(one=one, two=two)
        self.f.cfg.engines = ["one", "two"]
        try:
            call(self.f, "research", {"question": "widget frequency setting", "queries": ["widget frequency"],
                                      "depth": "quick"})
        finally:
            S.ENGINES.clear()
            S.ENGINES.update(old)
        e = self.f.log.recent(1)[0]
        s = e["summary"]
        # two sites only, so research widens its search (one more query) before reading
        self.assertEqual((s["queries"], s["candidates"], s["widened"]), (2, 3, True))
        self.assertGreaterEqual(s["sources"], 1)
        self.assertGreaterEqual(s["source_engines"].get("one", 0), 1)          # credit goes to the engines that
        self.assertGreaterEqual(s["source_engines"].get("two", 0), 1)          # found the pages used
        self.assertEqual(s["engines"]["one"]["results"], 4)                     # 2 results x 2 queries
        self.assertEqual(s["page_failures"], {"not found": 1})
        self.assertEqual(s["failed_sites"], [{"domain": "localhost", "why": "not found"}])
        self.assertIn("search_seconds", s)
        self.assertEqual(e["env"]["version"], __import__("openresearch").__version__)
        text = call(self.f, "log", {"id": e["id"]})
        self.assertIn("Summary:", text)
        self.assertIn("Engine one: 4 results from 2 queries", text)
        self.assertIn("Research: 2 queries (widened), 3 results", text)

    def test_stats_and_suggestions(self):
        from openresearch import insights

        def entry(i, tool, status, secs, engines, pages, sources=None):
            ev = []
            for name, (results, err, timeout) in engines.items():
                if timeout:
                    ev.append({"t": 12, "kind": "timeout", "engine": name, "ms": 12000, "error": "no answer"})
                else:
                    ev.append({"t": 1, "kind": "engine", "engine": name, "results": results, "ms": 900,
                               "error": err})
            for url, err in pages:
                ev.append({"t": 2, "kind": "page", "url": url, "chars": 0 if err else 900, "error": err})
            return {"id": f"c{i}", "tool": tool, "status": status, "duration": secs, "budget": 50, "started": i,
                    "events": ev, "metrics": {"source_engines": sources or {}}, "result_chars": 4000}

        entries = [entry(i, "research", "ok", 48 if i < 2 else 20,
                         {"ddg": (0, "", True), "tavily": (10, "", False), "wikipedia": (5, "", False)},
                         [("https://blocked.example/a", "HTTP 403 (the site blocks automated readers with a browser check)"),
                          ("https://ok.example/a", "")], {"tavily": 3})
                   for i in range(6)]
        stats = insights.aggregate(entries)
        self.assertEqual(stats["tools"]["research"]["calls"], 6)
        self.assertEqual(stats["engines"]["ddg"]["answered_pct"], 0)
        self.assertEqual(stats["engines"]["tavily"]["sources"], 18)
        self.assertEqual(stats["page_success_pct"], 50)
        self.assertEqual(stats["worst_domains"][0], {"domain": "blocked.example", "failures": 6, "why": "bot check"})
        tips = "\n".join(insights.suggest(stats))
        self.assertIn("ddg answered only 0%", tips)
        self.assertIn("wikipedia returned 30 results but none became a research source", tips)
        self.assertIn("blocked.example failed 6 times (bot check)", tips)
        self.assertIn("ran close to the time limit", tips)
        self.assertNotIn("tavily", tips)                                     # the engine doing the work is fine
        text = insights.format_stats(stats, insights.suggest(stats))
        self.assertIn("tavily        answered 100% of 6 queries", text)
        self.assertEqual(insights.cause("HTTP 419"), "refused")
        self.assertEqual(insights.cause("skipped: this call's time limit was reached first"), "time limit")

    def test_stats_from_server_log_and_github_copies(self):
        from openresearch import insights
        from openresearch.logship import GitHubLogShipper
        call(self.f, "fetch", {"urls": [self.base + "/article"]})
        call(self.f, "fetch", {"urls": [self.base + "/missing"]})
        text = call(self.f, "log", {"stats": True})
        self.assertIn("Statistics over 2 call(s)", text)
        self.assertIn("Pages: 1 read, 1 failed (50% success)", text)
        folder = self.tmp.name + "/logview/openresearch/2026-11-16"
        os.makedirs(folder)
        shipper = GitHubLogShipper("me/LogView", "tok", render=__import__("openresearch").trace.format_entry)
        entries = self.f.log.recent(2)
        with open(folder + "/031500-fetch-abc.md", "w", encoding="utf-8") as fh:
            fh.write(shipper._document(list(reversed(entries))))
        loaded = insights.load_dir(self.tmp.name + "/logview")
        self.assertEqual([e["id"] for e in loaded], [e["id"] for e in entries])   # newest first, intact
        self.assertEqual(insights.aggregate(loaded)["pages_failed"], 1)

    def test_long_rate_limit_wait_is_skipped_not_slept(self):
        import time
        from urllib.parse import urlsplit
        url = self.base + "/article"
        self.f._host_last[urlsplit(url).netloc] = time.monotonic()
        t0 = time.monotonic()
        r = self.f.fetch(url, delay=30, max_wait=1, max_age=0, robots=False)
        self.assertLess(time.monotonic() - t0, 1)
        self.assertIn("asks for 30s between requests", r.error)

    def test_mcp_abandons_a_call_that_overruns(self):
        import time
        srv = mcp_server.Server(self.f)
        srv.grace = 0.2
        self.f.cfg.tool_timeout = 0.2
        with mock.patch("openresearch.tools._dispatch", side_effect=lambda *a: time.sleep(1.5) or "late"):
            r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "search", "arguments": {"query": "x"}}})["result"]
            self.assertTrue(r["isError"])
            self.assertIn("gave up", r["content"][0]["text"])
            time.sleep(1.6)                                              # let the abandoned worker finish
        entries = self.f.log.recent(5)
        self.assertEqual([e["status"] for e in entries], ["timeout"])    # logged once, and not rewritten as ok

    def test_mcp_result_carries_the_call_id(self):
        srv = mcp_server.Server(self.f)
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "fetch", "arguments": {"urls": [self.base + "/missing"]}}})["result"]
        note = r["content"][1]["text"]
        entry = self.f.log.recent(1)[0]
        self.assertIn(f"[call {entry['id']}:", note)
        self.assertIn("1 problem(s)", note)
        listed = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "log", "arguments": {}}})["result"]
        self.assertIn(entry["id"], listed["content"][0]["text"])

    def test_log_page_and_encoded_token(self):
        token = "a+b/c="
        srv = mcp_server.serve_http("127.0.0.1", 0, token, mcp_server.Server(self.f))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def get(path, method="GET", body=None):
            req = urllib.request.Request(base + path, data=body, method=method,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, resp.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()
        try:
            call(self.f, "fetch", {"urls": [self.base + "/article"]})
            ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
            self.assertEqual(get("/mcp/a+b/c=", "POST", ping)[0], 200)
            self.assertEqual(get("/mcp/a%2Bb%2Fc%3D", "POST", ping)[0], 200)      # the same token, percent-encoded
            self.assertEqual(get("/mcp/a%2Bb", "POST", ping)[0], 404)
            status, text = get("/log/a%2Bb%2Fc%3D")
            self.assertEqual(status, 200)
            self.assertIn("fetch", text)
            call_id = self.f.log.recent(1)[0]["id"]
            self.assertIn("/article -> read", get(f"/log/a+b/c=?id={call_id}")[1])
            self.assertEqual(get("/log")[0], 404)
            self.assertEqual(get("/log/wrong")[0], 404)
            health = json.loads(get("/health")[1])
            self.assertIn("OPENRESEARCH_LOG_GITHUB_TOKEN are not set", health["log_copy"])   # names only, no values
            self.assertEqual(health["search_keys"], [])
            self.assertEqual((health["web_engines"], health["web_fallbacks"]),
                             (["ddg", "wikipedia"], ["bingnews", "hackernews"]))
        finally:
            srv.shutdown()
            srv.server_close()


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
