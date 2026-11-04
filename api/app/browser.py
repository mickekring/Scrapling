"""The browser behind the dynamic and stealth tiers.

Scrapling v0.5 runs all browser fetching on its stealth (patchright) backend. Two modes:

* ``persistent`` (default): long-lived ``AsyncStealthySession``s whose tabs are reused across
  requests, so there is no Chromium launch per fetch and far fewer processes. A session runs on
  one persistent browser context with a FIXED proxy (Scrapling can't switch the proxy per fetch
  in that mode), so there is one session per proxy: one for direct requests, and one for the
  default Apify proxy, started on first use. Requests for any other proxy (a specific
  ``country``) launch a one-off browser. Requests on the same session share cookies (e.g. a
  solved Cloudflare clearance carries over). A session is replaced after
  ``browser_recycle_after`` pages, or as soon as its browser is found dead; the old one is
  closed once its in-flight fetches finish.
* ``per_request``: launch a fresh browser for every fetch (the pre-v0.5 behaviour).
"""

import asyncio
import logging
from typing import Optional

from starlette.concurrency import run_in_threadpool

from scrapling.fetchers import AsyncStealthySession, StealthyFetcher

from .config import Settings
from .proxy import build_proxy_url, proxy_available

log = logging.getLogger("scrapling_api.browser")

# Error text meaning the session's browser is gone and the session must be replaced.
_DEAD_BROWSER_MARKERS = ("has been closed", "browser closed", "connection closed", "target crashed")


class BrowserPool:
    def __init__(self, s: Settings):
        self.s = s
        self.persistent = s.browser_mode == "persistent"
        self._sessions: dict[Optional[str], AsyncStealthySession] = {}  # proxy url (None = direct) -> session
        self._served: dict[Optional[str], int] = {}
        self._inflight: dict[int, int] = {}       # id(session) -> fetches running on it
        self._dead: set[int] = set()              # id(session) whose browser is gone
        self._retired: dict[int, AsyncStealthySession] = {}
        self._lock = asyncio.Lock()
        self.recycles = 0

    def _persistent_key(self, proxy_url: Optional[str]) -> tuple[bool, Optional[str]]:
        """Which persistent session serves this proxy, if any."""
        if proxy_url is None:
            return True, None
        if proxy_available() and proxy_url == build_proxy_url(None):
            return True, proxy_url
        return False, None

    async def _open(self, proxy_url: Optional[str]) -> AsyncStealthySession:
        # One tab more than the scrape limit (enforced in scraper.py), reserved for the watchdog
        # probe: under full load a probe that had to wait for a tab would fail and could restart
        # a perfectly healthy server.
        session = AsyncStealthySession(
            headless=True, max_pages=max(1, self.s.max_concurrent_browsers) + 1,
            retries=self.s.browser_retries, proxy=proxy_url,
        )
        await session.start()
        key = id(session)
        session.context.on("close", lambda *_: self._dead.add(key))
        log.info("Browser session started (%s, max_pages=%d).",
                 "via proxy" if proxy_url else "direct", self.s.max_concurrent_browsers + 1)
        return session

    async def start(self) -> None:
        if self.persistent:
            async with self._lock:
                self._sessions[None] = await self._open(None)
                self._served[None] = 0

    async def close(self) -> None:
        for session in [*self._sessions.values(), *self._retired.values()]:
            try:
                await session.close()
            except Exception:  # noqa: BLE001 — shutting down anyway
                pass
        self._sessions.clear()
        self._retired.clear()

    async def _acquire(self, key: Optional[str]) -> AsyncStealthySession:
        async with self._lock:
            current = self._sessions.get(key)
            reason = None
            if current is None:
                reason = "first use"
            elif id(current) in self._dead:
                reason = "browser died"
            elif self._served.get(key, 0) >= self.s.browser_recycle_after:
                reason = f"served {self._served[key]} pages"
            if reason:
                if current is not None:
                    self._retired[id(current)] = current
                    self.recycles += 1
                    log.info("Replacing browser session (%s).", reason)
                self._sessions[key] = current = await self._open(key)
                self._served[key] = 0
                await self._close_idle_retired()
            self._served[key] += 1
            self._inflight[id(current)] = self._inflight.get(id(current), 0) + 1
            return current

    async def _release(self, session: AsyncStealthySession) -> None:
        async with self._lock:
            key = id(session)
            self._inflight[key] = self._inflight.get(key, 1) - 1
            await self._close_idle_retired()

    async def _close_idle_retired(self) -> None:
        for key, session in list(self._retired.items()):
            if self._inflight.get(key, 0) <= 0:
                del self._retired[key]
                self._inflight.pop(key, None)
                self._dead.discard(key)
                try:
                    await session.close()
                except Exception:  # noqa: BLE001 — a dead browser may fail to close cleanly
                    pass

    async def fetch(self, url: str, proxy_url: Optional[str], **opts):
        """One browser fetch. ``opts`` are per-fetch Scrapling options (no session-level ones)."""
        use_session, key = self._persistent_key(proxy_url) if self.persistent else (False, None)
        # Scrapling 0.5 bug: on a reused tab, the first fetch after a Cloudflare solve silently
        # loses its shadow-DOM snapshot (status and HTML are fine). pierce_shadow is opt-in and
        # rare, so it always gets a fresh browser.
        if opts.get("pierce_shadow"):
            use_session = False
        if not use_session:
            return await run_in_threadpool(
                lambda: StealthyFetcher.fetch(url, headless=True, proxy=proxy_url,
                                              retries=self.s.browser_retries, **opts)
            )
        session = await self._acquire(key)
        try:
            return await session.fetch(url, **opts)
        except Exception as e:
            if any(m in str(e).lower() for m in _DEAD_BROWSER_MARKERS):
                self._dead.add(id(session))  # replaced on the next acquire
            raise
        finally:
            await self._release(session)

    def stats(self) -> dict:
        info: dict = {"mode": self.s.browser_mode}
        if self.persistent:
            info["recycle_after"] = self.s.browser_recycle_after
            info["recycles"] = self.recycles
            info["retired_pending"] = len(self._retired)
            info["sessions"] = {
                ("proxy" if key else "direct"): {"pages_served": self._served.get(key, 0), **session.get_pool_stats()}
                for key, session in self._sessions.items()
            }
        return info


_pool: Optional[BrowserPool] = None


def get_pool() -> BrowserPool:
    if _pool is None:
        raise RuntimeError("Browser pool not initialised (app lifespan not running)")
    return _pool


def init_pool(s: Settings) -> BrowserPool:
    global _pool
    _pool = BrowserPool(s)
    return _pool
