from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from registerwatch.config import settings

_CACHE: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
_TTL_S = 6 * 3600


@dataclass(frozen=True)
class RobotsDecision:
    allowed: bool
    crawl_delay_ms: int
    reason: str  # allowed | crawl_delay_applied | disallowed | unavailable


def _load(host_root: str) -> urllib.robotparser.RobotFileParser | None:
    now = time.time()
    hit = _CACHE.get(host_root)
    if hit and now - hit[0] < _TTL_S:
        return hit[1]

    parser: urllib.robotparser.RobotFileParser | None = None
    try:
        resp = httpx.get(
            f"{host_root}/robots.txt",
            headers={"User-Agent": settings().user_agent},
            timeout=10.0,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(resp.text.splitlines())
    except httpx.HTTPError:
        parser = None

    _CACHE[host_root] = (now, parser)
    return parser


def check(url: str) -> RobotsDecision:
    """Crawl-delay raises our interval. It never lowers it."""
    if not settings().respect_robots:
        return RobotsDecision(True, 0, "allowed")

    parts = urlsplit(url)
    host_root = f"{parts.scheme}://{parts.netloc}"
    parser = _load(host_root)
    if parser is None:
        # No robots.txt, or unreachable. Absence is permission, but stay polite.
        return RobotsDecision(True, 0, "unavailable")

    ua = settings().user_agent
    if not parser.can_fetch(ua, url):
        return RobotsDecision(False, 0, "disallowed")

    delay = parser.crawl_delay(ua)
    if delay:
        return RobotsDecision(True, int(float(delay) * 1000), "crawl_delay_applied")
    return RobotsDecision(True, 0, "allowed")
