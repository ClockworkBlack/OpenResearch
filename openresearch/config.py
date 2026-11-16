"""Configuration: defaults < config file < environment variables."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields

VERSION = "0.2.0"
ROBOTS_TOKEN = "OpenResearch"


def _default_cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "openresearch")


@dataclass
class Config:
    user_agent: str = (
        f"Mozilla/5.0 (compatible; OpenResearch/{VERSION})"
    )
    timeout: float = 20.0
    max_bytes: int = 15_000_000
    cache_dir: str = field(default_factory=_default_cache_dir)
    cache_ttl: int = 3600            # seconds a cached page counts as fresh
    respect_robots: bool = True
    allow_private: bool = False      # allow localhost / private-network URLs
    min_delay: float = 0.3           # seconds between requests to one host
    retries: int = 2
    wayback_fallback: bool = True    # try the Internet Archive when a page is blocked or gone
    render: str = "ask"              # ask: only when the caller asks | auto: when a page comes back empty |
                                     # always | never. Needs Playwright.
    render_concurrency: int = 1      # browsers open at once: each needs ~150-250 MB of memory
    engines: list = field(default_factory=list)          # override default web engines
    exclude_domains: list = field(default_factory=list)  # never return these
    prefer_domains: list = field(default_factory=list)   # rank these higher
    searxng_url: str = ""
    brave_api_key: str = ""
    tavily_api_key: str = ""
    github_token: str = ""
    openalex_api_key: str = ""
    tool_timeout: float = 50.0       # seconds a tool call may take before it returns what it has (0 = no limit)
    engine_timeout: float = 12.0     # seconds to wait for any one search engine
    log_keep: int = 300              # how many finished calls the log remembers
    log_stderr: bool = False         # also print one JSON line per call to stderr (for hosted logs)
    log_github_repo: str = ""        # owner/name (or URL) of a repository to copy the call log into
    log_github_token: str = ""       # a token that may write to that repository's contents
    log_github_dir: str = "openresearch"
    log_github_api: str = "https://api.github.com"

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        cfg = cls()
        path = path or os.environ.get("OPENRESEARCH_CONFIG") or os.path.join(
            os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
            "openresearch", "config.json")
        data = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
        for f in fields(cls):
            raw = os.environ.get("OPENRESEARCH_" + f.name.upper())
            if raw is None and f.name == "github_token":
                raw = os.environ.get("GITHUB_TOKEN") if os.environ.get("GITHUB_TOKEN") != "proxy-injected" else None
            if raw is None and f.name == "brave_api_key":
                raw = os.environ.get("BRAVE_API_KEY")
            if raw is None and f.name == "tavily_api_key":
                raw = os.environ.get("TAVILY_API_KEY")
            cur = getattr(cfg, f.name)
            if raw is not None:
                if isinstance(cur, bool):
                    val = raw.strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(cur, int):
                    val = int(raw)
                elif isinstance(cur, float):
                    val = float(raw)
                elif isinstance(cur, list):
                    val = [x.strip() for x in raw.split(",") if x.strip()]
                else:
                    val = raw
                setattr(cfg, f.name, val)
            elif f.name in data and isinstance(data[f.name], type(cur)):
                setattr(cfg, f.name, data[f.name])
        return cfg
