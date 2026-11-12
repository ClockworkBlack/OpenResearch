"""OpenResearch: search, read, crawl and research the web. Standard library only."""
from .config import VERSION as __version__, Config
from .crawl import crawl, map_site
from .fetch import Fetcher
from .page import Page, read, read_many
from .research import research
from .search import search
from .structured import extract

__all__ = ["Config", "Fetcher", "Page", "read", "read_many", "search", "research", "crawl", "map_site",
           "extract", "__version__"]
