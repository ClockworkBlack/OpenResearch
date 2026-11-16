"""HTTP fetching: cache, retries, politeness, robots.txt, private-network guard."""
from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import os
import random
import re
import socket
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass, field
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit

from .config import ROBOTS_TOKEN, Config

try:
    import brotli  # type: ignore
except ImportError:  # optional
    brotli = None

_RETRY_STATUS = frozenset((429, 500, 502, 503, 504))
_REDIRECT = frozenset((301, 302, 303, 307, 308))


@dataclass
class Response:
    url: str
    final_url: str = ""
    status: int = 0
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    error: str = ""
    from_cache: bool = False
    fetched_at: float = 0.0
    via: str = "live"            # live | cache | wayback | rendered

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        return decode(self.body, self.headers.get("content-type", ""))

    def json(self):
        return json.loads(self.text)


_CHALLENGE = re.compile(rb"just a moment\.\.\.|attention required|verify(?:ing)? (?:that )?you are (?:a )?human|"
                        rb"captcha|unusual traffic|access denied|are you a robot|enable javascript and cookies", re.I)


def _block_reason(status: int, headers: dict, body: bytes) -> str:
    """Say why a page was refused, so the caller can stop retrying and pick another source."""
    if status == 429:
        return " (rate limited by the site)"
    if status in (401, 403, 503):
        if headers.get("cf-mitigated") == "challenge" or _CHALLENGE.search(body[:4000]):
            return " (the site blocks automated readers with a browser check)"
        if status == 401:
            return " (login required)"
    return ""


def decode(body: bytes, content_type: str = "") -> str:
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:].decode("utf-8", "replace")
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        return body.decode("utf-16", "replace")
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    charset = m.group(1) if m else ""
    if not charset:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
        charset = m.group(1).decode("ascii", "replace") if m else "utf-8"
    try:
        return body.decode(charset, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    url = urldefrag(url)[0]
    s = urlsplit(url)
    path = quote(s.path, safe="/%:@!$&'()*+,;=~-._") or "/"
    query = quote(s.query, safe="=&%:@!$'()*+,;/?~-._")
    return urlunsplit((s.scheme.lower(), s.netloc, path, query, ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Cache:
    """SQLite page cache. Silently becomes a no-op if the directory is not writable."""

    def __init__(self, directory: str):
        self._lock = threading.Lock()
        self._db = None
        try:
            os.makedirs(directory, exist_ok=True)
            self._db = sqlite3.connect(os.path.join(directory, "cache.db"), check_same_thread=False, timeout=10)
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, final_url TEXT, status INTEGER,"
                " headers TEXT, body BLOB, fetched_at REAL)")
            self._db.commit()
        except (OSError, sqlite3.Error):
            self._db = None

    def get(self, key: str):
        if self._db is None:
            return None
        with self._lock:
            try:
                row = self._db.execute(
                    "SELECT final_url, status, headers, body, fetched_at FROM pages WHERE key=?", (key,)).fetchone()
            except sqlite3.Error:
                return None
        if not row:
            return None
        try:
            body = zlib.decompress(row[3])
        except zlib.error:
            return None
        return Response(url=key, final_url=row[0], status=row[1], headers=json.loads(row[2]), body=body,
                        from_cache=True, fetched_at=row[4], via="cache")

    def put(self, key: str, r: Response):
        if self._db is None:
            return
        with self._lock:
            try:
                self._db.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?)",
                                 (key, r.final_url, r.status, json.dumps(r.headers), zlib.compress(r.body, 6),
                                  r.fetched_at))
                self._db.commit()
            except sqlite3.Error:
                pass

    def touch(self, key: str):
        if self._db is None:
            return
        with self._lock:
            try:
                self._db.execute("UPDATE pages SET fetched_at=? WHERE key=?", (time.time(), key))
                self._db.commit()
            except sqlite3.Error:
                pass

    def stats(self) -> dict:
        if self._db is None:
            return {"enabled": False}
        with self._lock:
            n, size = self._db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(body)),0) FROM pages").fetchone()
        return {"enabled": True, "pages": n, "compressed_bytes": size}

    def clear(self):
        if self._db is None:
            return
        with self._lock:
            self._db.execute("DELETE FROM pages")
            self._db.commit()
            self._db.execute("VACUUM")

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


class Fetcher:
    def __init__(self, config: Config | None = None):
        self.cfg = config or Config.load()
        self.cache = Cache(self.cfg.cache_dir)
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self._lock = threading.Lock()
        self._host_locks: dict = {}
        self._host_last: dict = {}
        self._robots: dict = {}

    def close(self):
        self.cache.close()

    # ------------------------------------------------------------ safety
    def check_url(self, url: str) -> str:
        """Return an error string if the URL must not be fetched, else ''."""
        s = urlsplit(url)
        if s.scheme not in ("http", "https"):
            return f"unsupported URL scheme: {s.scheme or '(none)'}"
        host = s.hostname
        if not host:
            return "URL has no host"
        if self.cfg.allow_private:
            return ""
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return "private-network address blocked (set OPENRESEARCH_ALLOW_PRIVATE=1 to allow)"
        addrs = []
        try:
            addrs = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                addrs = [ipaddress.ip_address(i[4][0].split("%")[0])
                         for i in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
            except (OSError, ValueError):
                addrs = []          # cannot resolve here (e.g. a proxy resolves names): let the request decide
        for a in addrs:
            if a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified:
                return "private-network address blocked (set OPENRESEARCH_ALLOW_PRIVATE=1 to allow)"
        return ""

    # ------------------------------------------------------------ robots
    def _robots_for(self, origin: str):
        with self._lock:
            if origin in self._robots:
                return self._robots[origin]
        rp = None
        r = self.fetch(origin + "/robots.txt", robots=False, max_age=86400, timeout=10)
        if r.status == 200 and "<html" not in r.text[:500].lower():
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(r.text.splitlines())
        with self._lock:
            self._robots[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        s = urlsplit(url)
        rp = self._robots_for(f"{s.scheme}://{s.netloc}")
        if rp is None:
            return True
        try:
            return rp.can_fetch(ROBOTS_TOKEN, url)
        except Exception:
            return True

    def sitemaps(self, origin: str) -> list:
        rp = self._robots_for(origin)
        return list(rp.site_maps() or []) if rp is not None else []

    def _throttle(self, url: str, delay: float | None):
        host = urlsplit(url).netloc
        wait = self.cfg.min_delay if delay is None else delay
        rp = self._robots.get(f"{urlsplit(url).scheme}://{host}")
        if rp is not None:
            try:
                cd = rp.crawl_delay(ROBOTS_TOKEN)
                if cd:
                    wait = max(wait, min(float(cd), 10.0))
            except Exception:
                pass
        if wait <= 0:
            return
        with self._lock:
            lock = self._host_locks.setdefault(host, threading.Lock())
        with lock:
            pause = self._host_last.get(host, 0) + wait - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            self._host_last[host] = time.monotonic()

    # ------------------------------------------------------------ transport
    def _once(self, url, headers, timeout, data):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            try:
                resp = self._opener.open(req, timeout=timeout)
                status = resp.status
            except urllib.error.HTTPError as e:
                resp, status = e, e.code
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            chunks, size = [], 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > self.cfg.max_bytes:
                    return 0, hdrs, b"", f"response larger than {self.cfg.max_bytes} bytes"
                chunks.append(chunk)
            body = b"".join(chunks)
            enc = hdrs.get("content-encoding", "").lower()
            try:
                if "gzip" in enc or body[:2] == b"\x1f\x8b" and url.endswith(".gz"):
                    body = gzip.decompress(body)
                elif "deflate" in enc:
                    try:
                        body = zlib.decompress(body)
                    except zlib.error:
                        body = zlib.decompress(body, -zlib.MAX_WBITS)
                elif "br" in enc and brotli is not None:
                    body = brotli.decompress(body)
            except Exception as e:
                return 0, hdrs, b"", f"could not decompress response: {e}"
            return status, hdrs, body, ""
        except Exception as e:
            reason = getattr(e, "reason", None)
            return 0, {}, b"", f"{type(e).__name__}: {reason or e}"

    def fetch(self, url: str, *, max_age: int | None = None, headers: dict | None = None,
              robots: bool | None = None, timeout: float | None = None, data: bytes | None = None,
              delay: float | None = None, retries: int | None = None) -> Response:
        """GET (or POST when data is given) a URL. Never raises: check .ok / .error.

        retries overrides the configured retry count (0 = one attempt), for callers on a time budget."""
        try:
            url = normalize_url(url)
        except ValueError as e:
            return Response(url=url, error=f"bad URL: {e}")
        err = self.check_url(url)
        if err:
            return Response(url=url, error=err)
        ttl = self.cfg.cache_ttl if max_age is None else max_age
        key = url if data is None else url + "#" + hashlib.sha1(data).hexdigest()
        cached = self.cache.get(key)
        if cached is not None and time.time() - cached.fetched_at <= ttl:
            cached.url = url
            return cached
        if (self.cfg.respect_robots if robots is None else robots) and not self.allowed(url):
            return Response(url=url, error="disallowed by the site's robots.txt")

        h = {
            "User-Agent": self.cfg.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,application/json;q=0.9,*/*;q=0.7",
            "Accept-Language": "en-US,en;q=0.8",
            "Accept-Encoding": "gzip, deflate" + (", br" if brotli is not None else ""),
        }
        h.update(headers or {})
        if cached is not None and data is None:
            if cached.headers.get("etag"):
                h["If-None-Match"] = cached.headers["etag"]
            if cached.headers.get("last-modified"):
                h["If-Modified-Since"] = cached.headers["last-modified"]

        cur, status, hdrs, body, error = url, 0, {}, b"", ""
        timeout = timeout or self.cfg.timeout
        tries = max(self.cfg.retries if retries is None else retries, 0)
        for _hop in range(8):
            for attempt in range(tries + 1):
                self._throttle(cur, delay)
                status, hdrs, body, error = self._once(cur, h, timeout, data)
                transient = status in _RETRY_STATUS or (
                    status == 0 and not re.search(r"403|407|certificate|larger than|decompress", error, re.I))
                if not transient or attempt == tries:
                    break
                try:
                    pause = float(hdrs.get("retry-after", ""))
                except ValueError:
                    pause = 0.6 * (2 ** attempt)
                if pause > 10:                       # told to come back much later: waiting won't help now
                    break
                time.sleep(min(pause, 10.0) + random.random() * 0.3)
            if status in _REDIRECT and hdrs.get("location"):
                cur = normalize_url(urljoin(cur, hdrs["location"]))
                err = self.check_url(cur)
                if err:
                    return Response(url=url, final_url=cur, error="redirect to " + err)
                if status == 303:
                    data = None
                continue
            break
        else:
            return Response(url=url, final_url=cur, error="too many redirects")

        if status == 304 and cached is not None:
            self.cache.touch(key)
            cached.url, cached.fetched_at = url, time.time()
            return cached
        r = Response(url=url, final_url=cur, status=status, headers=hdrs, body=body, error=error,
                     fetched_at=time.time())
        if status == 200 and not error:
            self.cache.put(key, r)
        elif not error:
            r.error = f"HTTP {status}" + _block_reason(status, hdrs, body)
        return r

    def get_json(self, url: str, **kw):
        """Fetch and parse JSON; returns None on any failure."""
        kw.setdefault("robots", False)
        r = self.fetch(url, **kw)
        if not r.ok:
            return None
        try:
            return r.json()
        except ValueError:
            return None
