"""Command line: python -m openresearch <command>"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from .config import VERSION, Config
from .fetch import Fetcher
from .tools import call


def _csv(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def _doctor(f: Fetcher) -> int:
    """Live-check every engine and the fetch path, so you can see what works from this machine."""
    from .page import read
    from .search import ENGINES
    print(f"OpenResearch {VERSION} | cache: {f.cfg.cache_dir} {f.cache.stats()}")
    for mod, why in (("ddgs", "more search backends"), ("pypdf", "PDF text"), ("playwright", "JavaScript pages"),
                     ("brotli", "brotli compression")):
        try:
            __import__(mod)
            print(f"  optional {mod:<11} installed ({why})")
        except ImportError:
            print(f"  optional {mod:<11} missing   ({why})")
    bad = 0
    for name, fn in ENGINES.items():
        t = time.time()
        try:
            rows = fn(f, "open source web crawler", 5, {})
            state = f"ok, {len(rows)} results" if rows else "EMPTY (parser may be out of date, or you are rate-limited)"
            bad += not rows
        except Exception as e:
            skip = "no " in str(e) and ("configured" in str(e) or "set" in str(e))
            state = f"skipped ({e})" if skip else f"FAILED ({e})"
            bad += not skip
        print(f"  engine {name:<14} {state}  [{time.time() - t:.1f}s]")
    has_ddgs = any(__import__("importlib").util.find_spec(m) for m in ("ddgs", "duckduckgo_search"))
    if not (has_ddgs or f.cfg.brave_api_key or f.cfg.searxng_url):
        print("  hint: web search leans on DuckDuckGo alone. `pip install ddgs`, or set BRAVE_API_KEY or\n"
              "        OPENRESEARCH_SEARXNG_URL, for backups when it throttles you (README: Search backends)")
    p = read(f, "https://example.com", max_age=0)
    print(f"  fetch  example.com    {'ok' if p.ok else 'FAILED (' + p.error + ')'}")
    return 1 if bad or not p.ok else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="openresearch", description="Search, read, crawl and research the web.")
    ap.add_argument("--version", action="version", version=VERSION)
    ap.add_argument("--allow-private", action="store_true", help="allow localhost / private-network URLs")
    ap.add_argument("--no-robots", action="store_true", help="do not check robots.txt")
    ap.add_argument("--no-cache", action="store_true", help="always refetch")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("research", help="search + read + rank passages for a question")
    p.add_argument("question")
    p.add_argument("-q", "--query", action="append", help="search query (repeatable)")
    p.add_argument("--depth", default="standard", choices=["quick", "standard", "deep"])
    p.add_argument("--vertical", default="web")
    p.add_argument("--include"), p.add_argument("--exclude"), p.add_argument("--time")
    p.add_argument("--url", action="append", help="also read this URL (repeatable)")
    p.add_argument("--max-tokens", type=int)

    p = sub.add_parser("search", help="multi-engine web search")
    p.add_argument("query")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--vertical", default="web")
    p.add_argument("--site"), p.add_argument("--include"), p.add_argument("--exclude")
    p.add_argument("--time"), p.add_argument("--engines")

    p = sub.add_parser("fetch", help="read URLs as Markdown")
    p.add_argument("urls", nargs="+")
    p.add_argument("--query", default="", help="keep only passages relevant to this")
    p.add_argument("--max-tokens", type=int, default=6000)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--mode", default="main", choices=["main", "full"])
    p.add_argument("--links", default="inline", choices=["inline", "none"])
    p.add_argument("--render", action="store_true", help="load in a headless browser first (needs Playwright)")

    p = sub.add_parser("crawl", help="crawl a site")
    p.add_argument("url")
    p.add_argument("--query", default="")
    p.add_argument("--max-pages", type=int, default=15)
    p.add_argument("--max-depth", type=int, default=3)
    p.add_argument("--include"), p.add_argument("--exclude")
    p.add_argument("--subdomains", action="store_true")
    p.add_argument("--sitemap", action="store_true")
    p.add_argument("--max-tokens", type=int, default=12000)
    p.add_argument("--out", help="write each page to this directory as Markdown plus index.jsonl")

    p = sub.add_parser("map", help="list a site's URLs")
    p.add_argument("url")
    p.add_argument("--search", default="")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--subdomains", action="store_true")
    p.add_argument("--explore", type=int, default=12, help="pages to open for links when there is no sitemap")

    p = sub.add_parser("extract", help="structured data as JSON")
    p.add_argument("url")
    p.add_argument("-s", "--select", action="append", default=[], metavar="NAME=CSS",
                   help="e.g. -s title='h1::text' -s links='a.item::attr(href)' (append * to NAME for all matches)")
    p.add_argument("--want", help="comma list: meta,tables,jsonld,links,state")
    p.add_argument("--max-tokens", type=int, default=8000)

    p = sub.add_parser("mcp", help="run the MCP server (stdio by default)")
    p.add_argument("--http", action="store_true")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    p.add_argument("--token", default=os.environ.get("OPENRESEARCH_TOKEN", ""),
                   help="require this secret (Bearer header, or as /mcp/<token>)")

    sub.add_parser("doctor", help="check which engines and features work from here")
    p = sub.add_parser("cache", help="cache stats / clear")
    p.add_argument("action", choices=["stats", "clear"])

    a = ap.parse_args(argv)
    cfg = Config.load()
    if a.allow_private:
        cfg.allow_private = True
    if a.no_robots:
        cfg.respect_robots = False
    if a.no_cache:
        cfg.cache_ttl = 0
    f = Fetcher(cfg)

    try:
        return _run(a, f)
    except KeyboardInterrupt:
        return 130
    except (ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        f.close()


def _run(a, f) -> int:
    if True:
        if a.cmd == "mcp":
            from . import mcp_server
            if a.http:
                if a.host not in ("127.0.0.1", "localhost", "::1") and not a.token:
                    raise ValueError("refusing to listen on a public address without --token (or OPENRESEARCH_TOKEN): "
                                     "anyone who found the URL could use this server to fetch pages")
                srv = mcp_server.serve_http(a.host, a.port, a.token, mcp_server.Server(f))
                print(f"OpenResearch MCP on http://{a.host}:{a.port}/mcp" + (" (token required)" if a.token else ""),
                      file=sys.stderr)
                srv.serve_forever()
            else:
                mcp_server.serve_stdio(mcp_server.Server(f))
            return 0
        if a.cmd == "doctor":
            return _doctor(f)
        if a.cmd == "cache":
            if a.action == "clear":
                f.cache.clear()
            print(json.dumps(f.cache.stats()))
            return 0
        if a.cmd == "crawl" and a.out:
            from .crawl import crawl
            os.makedirs(a.out, exist_ok=True)
            index = open(os.path.join(a.out, "index.jsonl"), "w", encoding="utf-8")
            count = [0]

            def save(page):
                rec = page.as_dict()
                rec.pop("content", None)
                if page.ok:
                    count[0] += 1
                    name = re.sub(r"[^\w.-]+", "_", (page.final_url or page.url).split("://", 1)[-1])[:150] + ".md"
                    with open(os.path.join(a.out, name), "w", encoding="utf-8") as fh:
                        fh.write(page.header() + "\n\n" + page.markdown + "\n")
                    rec["file"] = name
                index.write(json.dumps(rec, ensure_ascii=False) + "\n")
                index.flush()
                print(("ok   " if page.ok else "FAIL ") + page.url + ("" if page.ok else f"  ({page.error})"),
                      file=sys.stderr)

            crawl(f, a.url, max_pages=a.max_pages, max_depth=a.max_depth, query=a.query, include=_csv(a.include),
                  exclude=_csv(a.exclude), subdomains=a.subdomains, use_sitemap=a.sitemap, on_page=save)
            index.close()
            print(f"{count[0]} pages written to {a.out}")
            return 0

        args = {k: v for k, v in vars(a).items() if v not in (None, "", [], False) and k not in (
            "cmd", "allow_private", "no_robots", "no_cache")}
        for key in ("include", "exclude", "engines", "want"):
            if key in args:
                args[key] = _csv(args[key])
        if a.cmd in ("research", "search"):
            for old, new in (("include", "include_domains"), ("exclude", "exclude_domains")):
                if old in args:
                    args[new] = args.pop(old)
        if a.cmd == "research":
            if "query" in args:
                args["queries"] = args.pop("query")
            if "url" in args:
                args["urls"] = args.pop("url")
        if a.cmd == "map":
            args["explore"] = a.explore                  # 0 is meaningful here, so keep it
        if a.cmd == "crawl" and args.pop("sitemap", None):
            args["use_sitemap"] = True
        if a.cmd == "extract":
            sels = {}
            for item in args.pop("select", []):
                name, _, css = item.partition("=")
                sels[name.rstrip("*")] = {"selector": css, "all": True} if name.endswith("*") else css
            if sels:
                args["selectors"] = sels
        print(call(f, a.cmd, args))
        return 0
