"""Per-call log: what each tool call did, how long each step took, and what went wrong.

Every tool call runs inside a Trace. Code anywhere below it (search engines, the fetcher, the page
reader) reports events with `trace.event(...)` and asks `trace.remaining()` how much of the call's
time limit is left. Finished traces are kept in a small SQLite file next to the page cache, so
`log` (tool, CLI command and HTTP page) can show what happened in any recent call.
"""
from __future__ import annotations

import contextvars
import json
import os
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from collections import deque

_current: contextvars.ContextVar = contextvars.ContextVar("openresearch_trace", default=None)
_PROCESS_START = time.time()
SERVER = False                         # set by the MCP server: only then does "cold start" mean anything
_calls_in_process = 0
_calls_lock = threading.Lock()


def _environment() -> dict:
    """Where the call ran: version, deployed commit, and whether the process had just started."""
    from .config import VERSION
    commit = os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("GIT_COMMIT") or ""
    return {"version": VERSION, "commit": commit[:10], "host": os.environ.get("RENDER_SERVICE_NAME", "")}
_MAX_EVENTS = 400
PROBLEM_KINDS = ("error", "timeout")


def is_problem(e: dict) -> bool:
    """A step that went wrong. A failed download on its own is not counted: the page or engine step
    that needed it reports the failure (and a missing robots.txt, say, is no failure at all)."""
    return e["kind"] in PROBLEM_KINDS or (bool(e.get("error")) and e["kind"] != "fetch")


def _short(v, limit: int = 300):
    """Arguments and messages trimmed for storage."""
    if isinstance(v, str):
        return v if len(v) <= limit else v[:limit] + "..."
    if isinstance(v, (list, tuple)):
        return [_short(x, limit) for x in v[:30]]
    if isinstance(v, dict):
        return {str(k)[:60]: _short(x, limit) for k, x in list(v.items())[:30]}
    return v


class Trace:
    """One tool call: its arguments, a timeline of events, and a time limit."""

    def __init__(self, tool: str, args: dict | None = None, budget: float = 0.0):
        self.id = uuid.uuid4().hex[:8]
        self.tool = tool
        self.args = _short(dict(args or {}))
        self.started = time.time()
        self._t0 = time.monotonic()
        self.budget = float(budget or 0)
        self.deadline = self._t0 + self.budget if self.budget > 0 else None
        self.events: list = []
        self.metrics: dict = {}          # numbers the tool reports about its own work (see note())
        global _calls_in_process
        with _calls_lock:
            _calls_in_process += 1
            self.call_number = _calls_in_process
        self.uptime = round(self.started - _PROCESS_START, 1)
        self.status = "running"          # running | ok | error | timeout
        self.error = ""
        self.duration = 0.0
        self.result_chars = 0
        self._lock = threading.Lock()
        self._dropped = 0

    # ------------------------------------------------------------ time limit
    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    def remaining(self):
        """Seconds left before this call's time limit, or None when it has none."""
        return None if self.deadline is None else self.deadline - time.monotonic()

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    # ------------------------------------------------------------ events
    def event(self, kind: str, **data):
        entry = {"t": round(self.elapsed(), 2), "kind": kind}
        entry.update({k: _short(v, 400) for k, v in data.items() if v not in (None, "", [], {})})
        with self._lock:
            if len(self.events) < _MAX_EVENTS:
                self.events.append(entry)
            else:
                self._dropped += 1

    def note(self, **values):
        """Record numbers about this call's own work (queries run, sources used...). Numbers given
        more than once are added up; other values are replaced."""
        with self._lock:
            for k, v in values.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(self.metrics.get(k), (int, float)):
                    self.metrics[k] += v
                else:
                    self.metrics[k] = _short(v, 200)

    def problems(self) -> list:
        """Events that record something going wrong (a failed engine, an unreadable page, a time-out)."""
        with self._lock:
            return [e for e in self.events if is_problem(e)]

    def finish(self, status: str = "ok", error: str = "", result_chars: int = 0) -> bool:
        """Record the verdict. Returns False if one was already recorded: the first stands, so a call
        that was given up on is not later rewritten as a success."""
        with self._lock:
            if self.status != "running":
                return False
            self.status, self.error = status, _short(error, 2000)
            self.duration = round(self.elapsed(), 2)
            self.result_chars = result_chars
            return True

    def as_dict(self) -> dict:
        from . import insights
        with self._lock:
            events, metrics = list(self.events), dict(self.metrics)
        entry = {"id": self.id, "tool": self.tool, "args": self.args, "started": self.started,
                 "duration": self.duration or round(self.elapsed(), 2), "status": self.status, "error": self.error,
                 "budget": self.budget, "result_chars": self.result_chars, "events": events,
                 "dropped_events": self._dropped, "problems": sum(1 for e in events if is_problem(e)),
                 "metrics": metrics, "env": _environment(), "uptime": self.uptime,
                 "cold_start": SERVER and self.call_number == 1 and self.uptime < 120}
        try:
            entry["summary"] = insights.summarize(entry)
            entry["suggestions"] = insights.suggest_call(entry, entry["summary"])
        except Exception as e:                # the analysis must never cost a call its log entry
            entry["summary"], entry["suggestions"] = {}, [f"(summary failed: {type(e).__name__}: {e})"]
        return entry


# ------------------------------------------------------------------ current trace

def current():
    return _current.get()


def event(kind: str, **data):
    t = _current.get()
    if t is not None:
        t.event(kind, **data)


def remaining(default=None):
    """Seconds left in the current call's time limit; `default` when there is no call or no limit."""
    t = _current.get()
    left = t.remaining() if t is not None else None
    return default if left is None else left


def note(**values):
    t = _current.get()
    if t is not None:
        t.note(**values)


def expired() -> bool:
    t = _current.get()
    return t is not None and t.expired()


def clamp(seconds: float, floor: float = 1.0) -> float:
    """`seconds`, shortened to what is left of the current call's time limit (never below `floor`)."""
    left = remaining()
    return seconds if left is None else max(min(seconds, left), floor)


def bind(fn):
    """Wrap `fn` so that it runs under the caller's trace when a worker thread picks it up
    (thread pools do not carry context variables across)."""
    t = _current.get()
    if t is None:
        return fn

    def bound(*a, **kw):
        token = _current.set(t)
        try:
            return fn(*a, **kw)
        finally:
            _current.reset(token)
    return bound


class active:
    """Context manager: make `trace` the current one for the enclosed code."""

    def __init__(self, trace: Trace):
        self.trace = trace

    def __enter__(self):
        self._token = _current.set(self.trace)
        return self.trace

    def __exit__(self, exc_type, exc, tb):
        _current.reset(self._token)
        if exc is not None and self.trace.status == "running":
            self.trace.event("error", error=f"{exc_type.__name__}: {exc}",
                             traceback="".join(traceback.format_exception(exc_type, exc, tb))[-1500:])
        return False


# ------------------------------------------------------------------ storage

class Log:
    """The last `keep` finished calls, in SQLite beside the page cache (in memory if that fails)."""

    def __init__(self, directory: str, keep: int = 300, stderr: bool = False, shipper=None):
        self.keep, self.stderr = max(int(keep), 1), stderr
        self.shipper = shipper               # optional off-machine copy (see logship.py)
        self._lock = threading.Lock()
        self._mem: deque = deque(maxlen=self.keep)
        self._db = None
        try:
            os.makedirs(directory, exist_ok=True)
            self._db = sqlite3.connect(os.path.join(directory, "log.db"), check_same_thread=False, timeout=10)
            self._db.execute("CREATE TABLE IF NOT EXISTS calls (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT,"
                             " started REAL, tool TEXT, status TEXT, problems INTEGER, entry TEXT)")
            self._db.execute("CREATE INDEX IF NOT EXISTS calls_id ON calls (id)")
            self._db.commit()
        except (OSError, sqlite3.Error):
            self._db = None

    def save(self, trace: Trace):
        entry = trace.as_dict()
        if self.stderr:
            line = {k: entry[k] for k in ("id", "tool", "status", "duration", "problems", "error") if entry[k] != ""}
            line["args"] = entry["args"]
            line["problem_list"] = [e for e in entry["events"] if is_problem(e)][:12]
            try:
                print("openresearch " + json.dumps(line, ensure_ascii=False, default=str), file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass
        if self.shipper is not None:
            self.shipper.add(entry)
        with self._lock:
            self._mem.append(entry)
            if self._db is None:
                return
            try:
                self._db.execute("INSERT INTO calls (id, started, tool, status, problems, entry) VALUES (?,?,?,?,?,?)",
                                 (entry["id"], entry["started"], entry["tool"], entry["status"], entry["problems"],
                                  json.dumps(entry, ensure_ascii=False, default=str)))
                self._db.execute("DELETE FROM calls WHERE seq <= (SELECT MAX(seq) FROM calls) - ?", (self.keep,))
                self._db.commit()
            except sqlite3.Error:
                pass

    def recent(self, n: int = 20, problems_only: bool = False) -> list:
        """Newest first."""
        n = max(1, min(int(n), self.keep))
        with self._lock:
            if self._db is not None:
                try:
                    where = "WHERE problems > 0 OR status != 'ok'" if problems_only else ""
                    rows = self._db.execute(f"SELECT entry FROM calls {where} ORDER BY seq DESC LIMIT ?", (n,)).fetchall()
                    return [json.loads(r[0]) for r in rows]
                except (sqlite3.Error, ValueError):
                    pass
            items = [e for e in reversed(self._mem) if not problems_only or e["problems"] or e["status"] != "ok"]
            return items[:n]

    def get(self, call_id: str):
        call_id = (call_id or "").strip().lower()
        with self._lock:
            if self._db is not None:
                try:
                    row = self._db.execute("SELECT entry FROM calls WHERE id=? ORDER BY seq DESC LIMIT 1",
                                           (call_id,)).fetchone()
                    if row:
                        return json.loads(row[0])
                except (sqlite3.Error, ValueError):
                    pass
            return next((e for e in reversed(self._mem) if e["id"] == call_id), None)

    def clear(self):
        with self._lock:
            self._mem.clear()
            if self._db is not None:
                try:
                    self._db.execute("DELETE FROM calls")
                    self._db.commit()
                except sqlite3.Error:
                    pass

    def close(self):
        if self.shipper is not None:
            self.shipper.flush()             # do not lose the last calls when the server stops
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


# ------------------------------------------------------------------ display

def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) + "Z"


def _args_brief(entry: dict) -> str:
    a = entry.get("args") or {}
    main = a.get("question") or a.get("query") or a.get("url") or ", ".join(a.get("urls") or []) or ""
    return str(main)[:90]


def _event_line(e: dict) -> str:
    kind, bits = e["kind"], []
    if kind == "engine":
        bits.append(f"{e.get('engine')}: " + (f"FAILED - {e['error']}" if e.get("error") else f"{e.get('results', 0)} results"))
        bits.append(f"query \"{e.get('query', '')}\"")
    elif kind == "fetch":
        state = e.get("error") or f"HTTP {e.get('status')}"
        bits.append(f"{e.get('url')} -> {state}" + (" (cached)" if e.get("cached") else ""))
        if e.get("bytes"):
            bits.append(f"{e['bytes']} bytes")
    elif kind == "page":
        bits.append(f"{e.get('url')} -> " + (f"FAILED - {e['error']}" if e.get("error")
                                             else f"read, {e.get('chars', 0)} chars" + (f" via {e['via']}" if e.get("via") not in (None, "live") else "")))
    else:
        bits.append(" ".join(f"{k}={v}" for k, v in e.items() if k not in ("t", "kind", "ms", "traceback")))
    if e.get("ms") is not None:
        bits.append(f"{e['ms'] / 1000:.1f}s")
    mark = "!" if is_problem(e) else " "
    return f"{mark} +{e['t']:>6.1f}s  {kind:<7} " + " | ".join(bits)


def format_list(entries: list) -> str:
    if not entries:
        return "No calls logged yet."
    lines = ["id        when (UTC)            tool      status   time    problems  what"]
    for e in entries:
        lines.append(f"{e['id']}  {_when(e['started'])}  {e['tool']:<8}  {e['status']:<7}  {e['duration']:>5.1f}s  "
                     f"{e['problems']:>8}  {_args_brief(e)}")
    lines.append("\nPass an id to see that call's full timeline.")
    return "\n".join(lines)


def format_entry(e: dict, problems_only: bool = False) -> str:
    """One call, as text: verdict, arguments, then the timeline (`!` marks a problem)."""
    limit = f" (limit {e['budget']:.0f}s)" if e.get("budget") else ""
    lines = [f"Call {e['id']}: {e['tool']} - {e['status'].upper()} in {e['duration']:.1f}s{limit}, "
             f"{e['problems']} problem(s), {e.get('result_chars', 0)} chars returned",
             f"Started {_when(e['started'])}", "Arguments: " + json.dumps(e.get("args") or {}, ensure_ascii=False)]
    if e.get("error"):
        lines.append("Error: " + e["error"])
    env = e.get("env") or {}
    where = [f"version {env['version']}" if env.get("version") else "",
             f"commit {env['commit']}" if env.get("commit") else "",
             "first call after the server started (cold start)" if e.get("cold_start") else ""]
    if any(where):
        lines.append("Server: " + ", ".join(x for x in where if x))
    if not problems_only and e.get("summary"):
        from . import insights
        lines += ["", "Summary:"] + ["  " + x for x in insights.format_summary(e["summary"])]
    if e.get("suggestions"):
        lines += ["", "Suggestions:"] + [f"  - {t}" for t in e["suggestions"]]
    events = [x for x in e.get("events", []) if not problems_only or is_problem(x)]
    if events:
        lines.append("\nTimeline (seconds from start; ! = problem):")
        lines += [_event_line(x) for x in events]
        tb = next((x["traceback"] for x in events if x.get("traceback")), "")
        if tb:
            lines.append("\nTraceback:\n" + tb)
    if e.get("dropped_events"):
        lines.append(f"({e['dropped_events']} further events not kept)")
    return "\n".join(lines)
