"""The browser behind the dynamic and stealth tiers.

Scrapling v0.5 runs all browser fetching on its stealth (patchright) backend. Two modes:

* ``persistent`` (default): long-lived ``AsyncStealthySession``s, so there is no Chromium launch
  per fetch and far fewer processes. A session runs on one persistent browser context with a
  FIXED proxy (Scrapling can't switch the proxy per fetch in that mode), so there is one session
  per proxy: one for direct requests, and one for the default Apify proxy, started on first use.
  Requests for any other proxy (a specific ``country``) get a one-off browser. Requests on the
  same session share cookies (e.g. a solved Cloudflare clearance carries over). A session is
  replaced after ``browser_recycle_after`` pages, or as soon as its browser is found dead.
* ``per_request``: a one-off browser for every fetch (the pre-v0.5 behaviour).

Three rules keep memory bounded. Each one closes a leak found in production (Nov 2026):

1. Every fetch gets a fresh tab, closed when the fetch ends. Scrapling would keep the tab for
   reuse with the last page still loaded and running (scripts, ads, video).
2. Every fetch has a deadline. A page that freezes its renderer makes Playwright's
   ``page.content()`` wait forever; without a deadline that fetch, its tab, its concurrency slot
   and, after the next recycle, its whole browser were never released.
3. A replaced browser always goes away: when its last fetch ends or after a grace period,
   whichever comes first, and any process that outlives the close is killed.
"""

import asyncio
import logging
import time
from contextlib import suppress
from typing import Optional

from scrapling.fetchers import AsyncStealthySession

from . import proctree
from .config import Settings
from .proxy import build_proxy_url, proxy_available

log = logging.getLogger("scrapling_api.browser")

# Error text meaning the session's browser is gone and the session must be replaced.
_DEAD_BROWSER_MARKERS = ("has been closed", "browser closed", "connection closed", "target crashed")

_SESSION_START_SECONDS = 60  # a browser that takes longer to start is given up on
_TAB_CLOSE_SECONDS = 10      # a tab that takes longer to close gets its whole browser replaced
_SESSION_CLOSE_SECONDS = 20  # a browser that takes longer to close is killed
_EXIT_WAIT_SECONDS = 3       # what a closed browser's processes get to exit on their own
_UNWIND_SECONDS = 5          # what a cancelled call into the browser gets to clean up


class _Abandoned(Exception):
    """A call into the browser did not finish in time and was left behind."""

    def __init__(self, unwound: bool):
        super().__init__("call into the browser abandoned")
        self.unwound = unwound  # False: even its cleanup after cancellation did not finish


def _driver_pid(session: AsyncStealthySession) -> Optional[int]:
    """Pid of the session's Playwright driver, the parent of its Chromium (private API, best effort)."""
    try:
        return session.playwright._impl_obj._connection._transport._proc.pid
    except AttributeError:
        return None


class _Slot:
    """One browser session and its bookkeeping."""

    def __init__(self, session: AsyncStealthySession, proxied: bool, one_off: bool):
        self.session = session
        self.proxied = proxied
        self.one_off = one_off
        self.served = 0
        self.inflight = 0
        self.dead = False            # the browser is gone: replace the session
        self.idle = asyncio.Event()  # set while no fetch is running on it
        self.idle.set()
        self.retired_at: Optional[float] = None
        self.closer: Optional[asyncio.Future] = None
        self.driver = proctree.identify(_driver_pid(session))


class BrowserPool:
    def __init__(self, s: Settings):
        self.s = s
        self.persistent = s.browser_mode == "persistent"
        self._current: dict[Optional[str], _Slot] = {}  # proxy url (None = direct) -> session in use
        self._retired: list[_Slot] = []                 # replaced or one-off sessions being closed
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Future] = set()
        # No fetch outlives the request budget, so a retired browser is idle well within it.
        # The extra minute only matters if that bookkeeping is ever wrong.
        self._retire_grace = s.request_max_seconds + 60
        self.recycles = 0
        self.fetch_timeouts = 0
        self.forced_closes = 0
        self.killed_processes = 0

    def _persistent_key(self, proxy_url: Optional[str]) -> tuple[bool, Optional[str]]:
        """Which persistent session serves this proxy, if any."""
        if proxy_url is None:
            return True, None
        if proxy_available() and proxy_url == build_proxy_url(None):
            return True, proxy_url
        return False, None

    def _spawn(self, coro) -> asyncio.Future:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # nobody may be left to collect it
        return task

    async def _bounded(self, coro, seconds: float):
        """Run a call into the browser for at most ``seconds``, then leave it behind.

        A plain timeout is not enough: it cancels once, and the library's cleanup after that may
        await the same unresponsive browser with no limit at all. So the call runs as its own
        task, gets a moment to unwind after being cancelled, and is abandoned if it doesn't.
        """
        task = self._spawn(coro)
        try:
            done, _ = await asyncio.wait({task}, timeout=seconds)
            if not done:
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=_UNWIND_SECONDS)
                raise _Abandoned(unwound=bool(done))
        except asyncio.CancelledError:
            task.cancel()  # the caller is being cancelled: take the call down with it
            raise
        return task.result()

    async def _open(self, proxy_url: Optional[str], one_off: bool = False) -> _Slot:
        # A persistent session gets one tab more than the scrape limit (enforced in scraper.py),
        # reserved for the watchdog probe: under full load a probe that had to wait for a tab
        # would fail and could restart a perfectly healthy server.
        max_pages = 1 if one_off else max(1, self.s.max_concurrent_browsers) + 1
        session = AsyncStealthySession(headless=True, max_pages=max_pages,
                                       retries=self.s.browser_retries, proxy=proxy_url)
        try:
            await self._bounded(session.start(), _SESSION_START_SECONDS)
        except _Abandoned:
            proctree.kill(proctree.tree(proctree.identify(_driver_pid(session))))
            raise RuntimeError(f"browser did not start within {_SESSION_START_SECONDS}s") from None
        slot = _Slot(session, bool(proxy_url), one_off)
        session.context.on("close", lambda *_: setattr(slot, "dead", True))
        if not one_off:
            log.info("Browser session started (%s, max_pages=%d).",
                     "via proxy" if proxy_url else "direct", max_pages)
        return slot

    async def start(self) -> None:
        if self.persistent:
            async with self._lock:
                self._current[None] = await self._open(None)

    async def close(self) -> None:
        """Shut down: close every browser, in use or retired."""
        slots = [*self._current.values(), *self._retired]
        self._current.clear()
        if slots:  # _close is safe to run twice, so a retired browser's own closer may be at it too
            await asyncio.wait([self._spawn(self._close(slot)) for slot in slots],
                               timeout=_SESSION_CLOSE_SECONDS + _EXIT_WAIT_SECONDS + 5)
        for task in list(self._tasks):
            task.cancel()

    async def _acquire(self, proxy_url: Optional[str], one_off: bool) -> _Slot:
        if one_off:
            slot = await self._open(proxy_url, one_off=True)
        else:
            async with self._lock:
                slot = self._current.get(proxy_url)
                reason = None
                if slot is None:
                    reason = "first use"
                elif slot.dead:
                    reason = "browser died"
                elif slot.served >= self.s.browser_recycle_after:
                    reason = f"served {slot.served} pages"
                if reason:
                    fresh = await self._open(proxy_url)  # first: if this fails, nothing has changed
                    if slot is not None:
                        self.recycles += 1
                        log.info("Replacing browser session (%s).", reason)
                        self._retire(slot)
                    slot = self._current[proxy_url] = fresh
        slot.served += 1
        slot.inflight += 1
        slot.idle.clear()
        return slot

    def _release(self, slot: _Slot) -> None:
        # No awaits: this must also run to the end when the caller is being cancelled.
        slot.inflight -= 1
        if slot.inflight <= 0:
            slot.idle.set()
        if slot.one_off:
            self._retire(slot)

    def _retire(self, slot: _Slot) -> None:
        slot.retired_at = time.monotonic()
        self._retired.append(slot)
        self._spawn(self._close_when_idle(slot))

    async def _close_when_idle(self, slot: _Slot) -> None:
        try:
            try:
                async with asyncio.timeout(self._retire_grace):
                    await slot.idle.wait()
            except TimeoutError:
                self.forced_closes += 1
                log.warning("A replaced browser still had %d fetch(es) running after %ds; closing it anyway.",
                            slot.inflight, self._retire_grace)
            await self._close(slot)
        finally:
            with suppress(ValueError):
                self._retired.remove(slot)

    async def _close(self, slot: _Slot) -> None:
        """Close a session and make sure its processes are gone."""
        procs = proctree.tree(slot.driver)  # before closing: afterwards the tree can't be walked
        if slot.closer is None:
            slot.closer = self._spawn(slot.session.close())
        # asyncio.wait, not a timeout around close(): a close() that hangs in its own cleanup
        # would ignore the cancellation and hang this task with it.
        done, _ = await asyncio.wait({slot.closer}, timeout=_SESSION_CLOSE_SECONDS)
        error = slot.closer.exception() if done and not slot.closer.cancelled() else None
        if not done or error:
            log.warning("A browser session did not close cleanly (%s).",
                        f"{type(error).__name__}: {error}" if error else f"no answer in {_SESSION_CLOSE_SECONDS}s")
        else:
            waited = 0.0
            while proctree.alive(procs) and waited < _EXIT_WAIT_SECONDS:
                await asyncio.sleep(0.2)
                waited += 0.2
        if killed := proctree.kill(procs):
            self.killed_processes += killed
            log.warning("Killed %d browser process(es) that outlived their session.", killed)

    async def _close_tab(self, slot: _Slot, tab) -> None:
        try:
            await self._bounded(tab.close(), _TAB_CLOSE_SECONDS)
        except Exception as e:  # noqa: BLE001 — whatever went wrong, the tab may still be open
            if not slot.dead and slot.retired_at is None:
                log.warning("A tab did not close (%s: %s); replacing its browser.", type(e).__name__, e)
            slot.dead = True

    async def _fetch_in_fresh_tab(self, slot: _Slot, url: str, opts: dict):
        tabs: list = []  # one per attempt Scrapling makes

        async def remember(page) -> None:  # Scrapling calls this with the tab before navigating
            tabs.append(page)

        try:
            return await slot.session.fetch(url, page_setup=remember, **opts)
        except asyncio.CancelledError:
            # Cut off mid-fetch. If Scrapling had not yet handed over the tab of the attempt it was
            # on, that tab is one we can't find: it may be open, or sit in the pool as busy for
            # good. Replacing the browser is the only way to be rid of it.
            if all(tab.is_closed() for tab in tabs):
                slot.dead = True
            raise
        finally:
            # Scrapling has just put the tab back in its pool, marked ready for the next request,
            # with this page still loaded and running; after a cancelled fetch that even includes
            # a frozen tab. Take it out of the pool before anything else can run, then close it.
            pool = slot.session.page_pool
            for info in [i for i in pool.pages if any(i.page is tab for tab in tabs)]:
                pool.remove_page(info)
            for tab in tabs:
                if not tab.is_closed():
                    self._spawn(self._close_tab(slot, tab))
            # Every other fetch running on this browser holds at most one pool entry.
            if len(pool.pages) >= slot.inflight:
                slot.dead = True  # more than that: a tab nobody will release is stuck in the pool

    async def fetch(self, url: str, proxy_url: Optional[str], *, max_seconds: float, **opts):
        """One browser fetch, abandoned after ``max_seconds``.

        ``opts`` are per-fetch Scrapling options (no session-level ones).
        """
        use_session, key = self._persistent_key(proxy_url) if self.persistent else (False, None)
        # Scrapling 0.5 bug (still in 0.5.1): on a reused tab, the first fetch after a Cloudflare
        # solve silently loses its shadow-DOM snapshot (status and HTML are fine). pierce_shadow is
        # opt-in and rare, so it always gets a fresh browser. (Tabs are no longer reused here, which
        # may make this unnecessary; not re-tested, so the workaround stays.)
        if opts.get("pierce_shadow"):
            use_session = False
        slot = await self._acquire(key if use_session else proxy_url, one_off=not use_session)
        try:
            return await self._bounded(self._fetch_in_fresh_tab(slot, url, opts), max_seconds)
        except _Abandoned as e:
            self.fetch_timeouts += 1
            if not e.unwound:
                slot.dead = True  # still stuck in its own cleanup: nothing more to expect from this browser
            why = "the browser stopped answering" if slot.dead else "the page never finished"
            raise TimeoutError(f"browser fetch abandoned after {max_seconds:.0f}s ({why})") from None
        except Exception as e:
            if any(m in str(e).lower() for m in _DEAD_BROWSER_MARKERS):
                slot.dead = True  # replaced on the next acquire
            raise
        finally:
            self._release(slot)

    def stats(self) -> dict:
        now = time.monotonic()
        info: dict = {"mode": self.s.browser_mode}
        if self.persistent:
            info["recycle_after"] = self.s.browser_recycle_after
            info["recycles"] = self.recycles
        # Browsers being closed. Normally 0 or briefly more; a count or an age that stays up
        # means browsers are not going away.
        info["retired_pending"] = len(self._retired)
        info["retired_oldest_seconds"] = round(max((now - s.retired_at for s in self._retired), default=0))
        info["fetch_timeouts"] = self.fetch_timeouts
        info["forced_closes"] = self.forced_closes
        info["killed_processes"] = self.killed_processes
        if self.persistent:
            info["sessions"] = {
                ("proxy" if key else "direct"): {"pages_served": slot.served, **slot.session.get_pool_stats()}
                for key, slot in self._current.items()
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
