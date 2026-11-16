"""Keep the call log somewhere that outlives the server: a folder in a GitHub repository.

A hosted server's disk is often temporary (Render's free plan wipes it at every restart), so the
log in `log.db` disappears with it. When a repository and a token are configured, finished calls
are also written there as small Markdown files, a few seconds after they happen:

    <folder>/2026-11-16/021455-research-3f2a9c1e.md

Each file holds the readable timeline of one or more calls, with the raw JSON underneath.
Standard library only; a failed upload never disturbs a tool call and is retried with the next one.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request

from .config import VERSION

_MAX_QUEUE = 300


def repo_name(value: str) -> str:
    """'owner/name' from 'owner/name', a github.com URL, or a clone URL ending in .git."""
    value = (value or "").strip()
    value = re.sub(r"^(?:https?://|git@)?(?:www\.)?github\.com[/:]", "", value)
    value = re.sub(r"\.git$", "", value.strip("/"))
    return value if re.fullmatch(r"[\w.-]+/[\w.-]+", value) else ""


class GitHubLogShipper:
    def __init__(self, repo: str, token: str, folder: str = "openresearch", api: str = "https://api.github.com",
                 branch: str = "", delay: float = 15.0, render=None):
        self.repo, self.token = repo_name(repo), token.strip()
        self.folder = folder.strip("/") or "openresearch"
        self.api, self.branch, self.delay = api.rstrip("/"), branch, delay
        self._render = render                 # entry dict -> readable text
        self._queue: list = []
        self._lock = threading.Lock()         # guards the queue and the timer
        self._sending = threading.Lock()      # one upload at a time
        self._timer = None
        self.sent = 0
        self.last_ok = 0.0
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.repo and self.token)

    # ------------------------------------------------------------ queue
    def add(self, entry: dict):
        """Queue a finished call. It is uploaded `delay` seconds later, together with any that follow
        in the meantime, so a burst of calls becomes one file and one commit."""
        if not self.enabled:
            return
        with self._lock:
            self._queue.append(entry)
            del self._queue[:-_MAX_QUEUE]
            if self._timer is None:
                self._timer = threading.Timer(self.delay, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> bool:
        """Upload everything queued, now. Returns True if nothing is left waiting."""
        with self._sending:
            with self._lock:
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                batch, self._queue = self._queue, []
            if not batch:
                return True
            error = self._upload(batch)
            if not error:
                self.sent += len(batch)
                self.last_ok, self.last_error = time.time(), ""
                return True
            self.last_error = f"{time.strftime('%H:%M:%SZ', time.gmtime())} {error}"
            with self._lock:                  # keep the calls; the next save (or shutdown) tries again
                self._queue = (batch + self._queue)[-_MAX_QUEUE:]
            return False

    def status(self) -> str:
        if not self.enabled:
            return "off"
        with self._lock:
            waiting = len(self._queue)
        bits = [f"{self.repo}/{self.folder}: {self.sent} call(s) saved"]
        if waiting:
            bits.append(f"{waiting} waiting")
        if self.last_error:
            bits.append(f"last error {self.last_error}")
        return ", ".join(bits)

    # ------------------------------------------------------------ upload
    def _document(self, batch: list) -> str:
        first = batch[0]
        when = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(first["started"]))
        lines = [f"# OpenResearch log, {when}", "",
                 f"{len(batch)} call(s) from OpenResearch {VERSION}. `!` marks a step that went wrong.", ""]
        for e in batch:
            what = (e.get("args") or {}).get("question") or (e.get("args") or {}).get("query") \
                or (e.get("args") or {}).get("url") or ", ".join((e.get("args") or {}).get("urls") or [])
            lines += [f"## {e['tool']} `{e['id']}`: {e['status']} in {e['duration']:.1f}s, {e['problems']} problem(s)",
                      "", str(what)[:300], "", "```text",
                      (self._render(e) if self._render else json.dumps(e, indent=1)).replace("```", "'''"), "```", ""]
        lines += ["<details><summary>Raw data (JSON)</summary>", "", "```json",
                  json.dumps(batch, ensure_ascii=False, default=str, indent=1).replace("```", "'''"), "```", "",
                  "</details>", ""]
        return "\n".join(lines)

    def _upload(self, batch: list) -> str:
        """Create one file for the batch. Returns '' on success, else the reason."""
        first = batch[0]
        t = time.gmtime(first["started"])
        name = f"{time.strftime('%H%M%S', t)}-{first['tool']}-{first['id']}" + (
            f"-and-{len(batch) - 1}-more" if len(batch) > 1 else "") + ".md"
        path = f"{self.folder}/{time.strftime('%Y-%m-%d', t)}/{name}"
        problems = sum(e["problems"] for e in batch)
        body = {"message": f"OpenResearch log: {len(batch)} call(s), {problems} problem(s)",
                "content": base64.b64encode(self._document(batch).encode("utf-8")).decode("ascii")}
        if self.branch:
            body["branch"] = self.branch
        req = urllib.request.Request(
            f"{self.api}/repos/{self.repo}/contents/{path}", data=json.dumps(body).encode("utf-8"), method="PUT",
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": f"OpenResearch/{VERSION} log upload"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
                return "" if resp.status in (200, 201) else f"GitHub answered HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code == 422:                 # the file is already there: an earlier try got through after all
                return ""
            hint = {401: "the token was rejected", 403: "the token may not write to this repository",
                    404: "repository not found, or the token cannot see it"}.get(e.code, "")
            return f"GitHub answered HTTP {e.code}" + (f": {hint}" if hint else "")
        except Exception as e:
            return f"could not reach GitHub ({type(e).__name__}: {getattr(e, 'reason', e)})"[:200]
