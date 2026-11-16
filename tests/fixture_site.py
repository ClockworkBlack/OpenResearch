"""A small local website used by the tests (no internet needed)."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARTICLE = """<!doctype html><html lang="en"><head><title>Solar Storage Costs Fall | Example News</title>
<meta name="description" content="Battery prices dropped again this year.">
<meta property="article:published_time" content="2026-03-14T09:00:00Z">
<meta name="author" content="Staff Writer">
<link rel="canonical" href="/article">
<script type="application/ld+json">{"@type":"NewsArticle","headline":"Solar Storage Costs Fall","datePublished":"2026-03-14"}</script>
<style>.x{color:red}</style></head><body>
<header><nav class="main-nav"><a href="/">Home</a> <a href="/docs/a">Docs</a> <a href="/about">About</a></nav></header>
<div class="cookie-banner">We use cookies. <button>Accept</button></div>
<div class="layout"><aside class="sidebar"><h3>Trending</h3><ul><li><a href="/t1">Ten weird tricks</a></li>
<li><a href="/t2">Celebrity gossip</a></li></ul></aside>
<article><h1>Solar Storage Costs Fall</h1>
<p>Grid-scale battery prices fell <strong>18 percent</strong> in 2025, according to the
<a href="/reports/annual?utm_source=x">annual survey</a>, continuing a decade-long decline.</p>
<h2>Why prices dropped</h2>
<p>Analysts point to three causes: cheaper lithium carbonate, larger factories, and a shift to
iron-phosphate chemistry, which avoids nickel and cobalt entirely.</p>
<ul><li>Lithium carbonate fell by half</li><li>Factory scale doubled<ul><li>Mostly in Asia</li></ul></li></ul>
<h2>Regional figures</h2>
<table><caption>Price per kWh</caption><thead><tr><th>Region</th><th>2024</th><th>2025</th></tr></thead>
<tbody><tr><td>Europe</td><td>$210</td><td>$172</td></tr><tr><td>Asia</td><td>$150</td><td>$123</td></tr></tbody></table>
<h2>Installing a home system</h2>
<p>A typical household needs between ten and fifteen kilowatt-hours of storage, and installers
recommend sizing the inverter before choosing the battery. Permits take about six weeks.</p>
<pre><code class="language-python">def payback(cost, saving):
    return cost / saving</code></pre>
<blockquote><p>Storage is now cheaper than peaker plants, said one grid operator.</p></blockquote>
<div class="share-buttons"><a href="https://twitter.example/share">Tweet</a></div>
</article></div>
<div class="related-posts"><h3>Related</h3><a href="/other">Other story</a></div>
<footer><p>Copyright 2026 Example News. <a href="/privacy">Privacy</a></p></footer>
<script>console.log("tracking")</script></body></html>"""

DOC_A = """<html><head><title>Docs: Install</title></head><body><nav><a href="/docs/a">Install</a>
<a href="/docs/b">Configure</a></nav><main><h1>Install</h1><p>Run the installer and then read the
<a href="/docs/b">configuration guide</a> to set the widget frequency. Unrelated <a href="/blog/cats">cat blog</a>.
This paragraph is long enough to count as real content for the extractor to keep around here.</p>
<p><a href="/private/secret">Secret</a> <a href="/logo.png">logo</a> <a href="https://elsewhere.example/x">offsite</a></p>
</main></body></html>"""

DOC_B = """<html><head><title>Docs: Configure</title></head><body><main><h1>Configure</h1>
<p>Set the widget frequency to 42 hertz in the settings file. The configuration guide covers every option
and explains how each setting changes the behaviour of the widget in normal operation.</p>
<p>Back to <a href="/docs/a">install</a>. See also <a href="/docs/c">advanced configuration</a>.</p></main></body></html>"""

DOC_C = """<html><head><title>Docs: Advanced</title></head><body><main><h1>Advanced configuration</h1>
<p>Advanced configuration lets you tune the widget damping coefficient and the frequency response curve
beyond the defaults, which most people never need to touch at all in practice.</p></main></body></html>"""

CATS = """<html><head><title>Cat blog</title></head><body><main><h1>Cats</h1><p>Cats sleep for most of the day
and prefer warm windowsills. This page has nothing to do with widgets or their settings whatsoever, truly.</p>
</main></body></html>"""

SPA = """<html><head><title>App</title></head><body><div id="root"></div>
<script>document.getElementById('root').innerHTML = '<main><h1>Rendered headline</h1><p>' +
'This paragraph only exists after JavaScript runs, so a plain HTTP fetch sees an empty shell. '.repeat(4) +
'</p></main>';</script><script></script><script></script></body></html>"""

FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>Example Feed</title>
<item><title>First post</title><link>http://x.example/1</link><pubDate>Mon, 02 Mar 2026</pubDate>
<description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Second post</title><link>http://x.example/2</link></item></channel></rss>"""


def _pdf(text: str) -> bytes:
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"]
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


class Site:
    def __init__(self):
        self.hits = {}
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
                body = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                n = site.hits[path] = site.hits.get(path, 0) + 1
                base = f"http://127.0.0.1:{self.server.server_port}"
                if path == "/robots.txt":
                    return self._send(200, f"User-agent: *\nDisallow: /private/\nSitemap: {base}/sitemap.xml\n", "text/plain")
                if path == "/sitemap.xml":
                    locs = "".join(f"<url><loc>{base}{p}</loc><lastmod>2026-01-0{i}</lastmod></url>"
                                   for i, p in enumerate(("/article", "/docs/a", "/docs/b", "/docs/c"), 1))
                    return self._send(200, f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>', "application/xml")
                pages = {"/article": ARTICLE, "/docs/a": DOC_A, "/docs/b": DOC_B, "/docs/c": DOC_C,
                         "/blog/cats": CATS, "/spa": SPA, "/private/secret": "<html><body><p>secret</p></body></html>",
                         "/": "<html><body><main><h1>Home</h1><p><a href='/article'>Article</a> <a href='/docs/a'>Docs</a></p></main></body></html>"}
                if path in pages:
                    return self._send(200, pages[path])
                if path == "/redirect":
                    return self._send(302, "", extra={"Location": "/article"})
                if path == "/flaky":
                    return self._send(503, "busy") if n < 3 else self._send(200, CATS)
                if path == "/etag":
                    if self.headers.get("If-None-Match") == '"v1"':
                        return self._send(304, "")
                    return self._send(200, CATS, extra={"ETag": '"v1"'})
                if path == "/feed.xml":
                    return self._send(200, FEED, "application/rss+xml")
                if path == "/data.json":
                    return self._send(200, '{"name":"widget","sizes":[1,2,3]}', "application/json")
                if path == "/paper.pdf":
                    return self._send(200, _pdf("Quarterly widget output rose to 9000 units"), "application/pdf")
                if path == "/latin1":
                    return self._send(200, "<html><body><main><p>caf\xe9 cr\xe8me</p></main></body></html>".encode("latin-1"),
                                      "text/html; charset=iso-8859-1")
                if path.startswith("/nomap/"):
                    nomap = {
                        "/nomap/": "<html><body><a href='/nomap/about'>About us</a> <a href='/nomap/library/'>Library "
                                   "reference</a> <a href='/nomap/blog'>Blog</a></body></html>",
                        "/nomap/library/": "<html><body><a href='/nomap/library/json'>json</a> "
                                           "<a href='/nomap/library/html.parser'>html.parser - parse HTML</a></body></html>",
                    }
                    if path in nomap:
                        return self._send(200, nomap[path])
                    return self._send(200, "<html><body><p>Leaf page.</p></body></html>")
                if path == "/challenge":
                    return self._send(403, "<html><head><title>Just a moment...</title></head><body></body></html>",
                                      extra={"cf-mitigated": "challenge"})
                if path == "/busy":
                    return self._send(429, "slow down", extra={"Retry-After": "3600"})
                if path == "/down":
                    return self._send(503, "down")
                self._send(404, "<html><body>not found</body></html>")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
