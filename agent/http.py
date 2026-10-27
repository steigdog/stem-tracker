"""A polite HTTP client.

Every network request the agent makes goes through `PoliteSession`, which:
  * checks robots.txt before fetching any URL (and caches it per host),
  * waits at least `min_delay_seconds` between requests to the same host,
  * honors a robots.txt Crawl-delay if it is longer,
  * retries temporary failures (429 / 5xx / timeouts) with backoff,
  * refuses to continue when a request lands on a login page.

It is thread-safe, so several sources can be fetched in parallel while each
individual host still sees one request at a time.
"""

from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests


class Blocked(Exception):
    """robots.txt disallows the URL, or it sits behind a login."""


class PoliteSession:
    def __init__(self, user_agent: str, min_delay: float = 1.5, timeout: float = 30, retries: int = 2):
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.timeout = timeout
        self.retries = retries
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()
        self.request_count = 0

    # ----- robots.txt ------------------------------------------------------
    def _robots_for(self, url: str) -> RobotFileParser | None:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        with self._meta_lock:
            if base in self._robots:
                return self._robots[base]
        rp = RobotFileParser()
        try:
            r = self._session.get(base + "/robots.txt", timeout=self.timeout)
            if r.status_code >= 500:
                rp.disallow_all = True            # server error: play safe, don't crawl
            elif r.status_code >= 400:
                rp.allow_all = True               # no robots.txt (404/403): crawling allowed
            else:
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            rp.disallow_all = True                # unreachable: don't crawl this run
        with self._meta_lock:
            self._robots[base] = rp
        return rp

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        return rp is None or rp.can_fetch(self.user_agent, url)

    # ----- rate limiting ---------------------------------------------------
    def _wait_turn(self, url: str) -> threading.Lock:
        host = urlparse(url).netloc
        with self._meta_lock:
            lock = self._host_locks.setdefault(host, threading.Lock())
        lock.acquire()
        rp = self._robots_for(url)
        delay = self.min_delay
        try:
            cd = rp.crawl_delay(self.user_agent) if rp else None
            if cd:
                delay = max(delay, float(cd))
        except Exception:
            pass
        wait = self._last_hit.get(host, 0) + delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        return lock

    # ----- requests --------------------------------------------------------
    def request(self, method: str, url: str, **kw) -> requests.Response:
        if not self.allowed(url):
            raise Blocked(f"robots.txt disallows {url}")
        kw.setdefault("timeout", self.timeout)
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            lock = self._wait_turn(url)
            try:
                self.request_count += 1
                r = self._session.request(method, url, **kw)
            except requests.RequestException as e:
                last_exc = e
                r = None
            finally:
                self._last_hit[urlparse(url).netloc] = time.monotonic()
                lock.release()
            if r is not None:
                if _looks_like_login(r):
                    raise Blocked(f"{url} redirected to a login page; skipping")
                if r.status_code not in (429, 500, 502, 503, 504):
                    return r
                last_exc = requests.HTTPError(f"HTTP {r.status_code} for {url}")
            time.sleep(2 ** attempt * 2)      # backoff: 2s, 4s, 8s...
        raise last_exc or requests.RequestException(f"failed: {url}")

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)


_LOGIN_RE = re.compile(r"/(login|signin|sign-in|sso|auth/realms|oauth2?/authorize)\b", re.I)


def _looks_like_login(r: requests.Response) -> bool:
    """True if we were redirected to a sign-in page (content behind a login)."""
    if r.history and _LOGIN_RE.search(urlparse(r.url).path):
        return True
    return r.status_code == 401
