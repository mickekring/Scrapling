"""The escalation engine.

Two orthogonal axes:
  * ENGINE  (http -> dynamic -> stealth)  defeats JS rendering and anti-bot.
  * PROXY   (off/on)                       defeats geo-blocks / IP bans / rate-limits.

`engine=auto` walks the engine ladder and stops at the first rung that returns
*real* content. The proxy is a separate, sticky decision: forced on (`proxy=on`),
forced off (`proxy=off`), or `proxy=auto` — start without it and switch it on
(for the rest of the run) the moment a geo/IP signal appears. A final
`stealth + proxy` fallback covers the geo+anti-bot worst case.

Content extraction reuses `Convertor._extract_content` — the exact call
Scrapling's own MCP server makes (scrapling/core/ai.py::_translate_response) —
so output matches the library's `scrapling extract` CLI.
"""

import time
from enum import Enum
from typing import Optional, Tuple

import anyio
from starlette.concurrency import run_in_threadpool

from scrapling.fetchers import Fetcher, DynamicFetcher, StealthyFetcher
from scrapling.core.shell import Convertor  # NOTE: private-ish reuse; pinned scrapling version, see api/README.md

from .config import Settings, get_settings
from .models import Attempt, Engine, ProxyMode, ScrapeResponse
from .proxy import build_proxy_url, proxy_available

# Signatures that mean "this isn't the page you asked for".
_CHALLENGE_MARKERS = (
    "just a moment", "challenge-platform", "cf-chl", "cf_chl", "_cf_chl_opt",
    "checking your browser", "turnstile", "verify you are human", "ddos-guard",
)
_GEO_MARKERS = (
    "not available in your country", "not available in your region",
    "not available in your location", "access from your country",
    "geo-blocked", "geoblocked", "content is not available in your",
    "not available in the country", "this content isn't available in your",
)

# One shared limiter (not loop-bound) caps concurrent browser launches.
_browser_limiter = anyio.CapacityLimiter(max(1, get_settings().max_concurrent_browsers))


class Outcome(str, Enum):
    OK = "ok"                # real content
    CHALLENGE = "challenge"  # anti-bot / JS wall -> escalate engine toward stealth
    GEO = "geo_block"        # geo / IP / rate-limit -> enable proxy
    EMPTY = "empty"          # 2xx but ~empty body -> escalate engine (likely needs JS)
    ERROR = "error"          # fetch raised / 5xx / proxy failure


# --- sync fetchers (run in a worker thread; they manage their own event loop) --

def _fetch_http(url: str, proxy_url: Optional[str], s: Settings):
    return Fetcher.get(
        url, impersonate=s.http_impersonate, stealthy_headers=True,
        follow_redirects="safe", timeout=s.http_timeout, proxy=proxy_url,
    )


def _fetch_dynamic(url: str, proxy_url: Optional[str], s: Settings):
    return DynamicFetcher.fetch(
        url, headless=True, network_idle=s.browser_network_idle, proxy=proxy_url,
    )


def _fetch_stealth(url: str, proxy_url: Optional[str], s: Settings):
    return StealthyFetcher.fetch(
        url, headless=True, network_idle=s.browser_network_idle,
        solve_cloudflare=s.solve_cloudflare, google_search=False, proxy=proxy_url,
    )


_FETCHERS = {"http": _fetch_http, "dynamic": _fetch_dynamic, "stealth": _fetch_stealth}


def _looks(markers: Tuple[str, ...], html: str) -> bool:
    low = html.lower()
    return any(m in low for m in markers)


def _visible_text(page) -> str:
    """Real, human-visible text — excludes scripts/styles/interstitial markup."""
    if page is None:
        return ""
    return page.get_all_text(strip=True, ignore_tags=("script", "style", "noscript", "svg", "iframe", "template"))


def _classify(page, html: str, status: Optional[int], err: Optional[str], s: Settings) -> Outcome:
    if err is not None or page is None:
        return Outcome.ERROR

    # Gate success on real VISIBLE TEXT, not raw markup. A 200 that is only a JS
    # shell, or a 200-status anti-bot interstitial, carries plenty of markup but
    # ~no text -- it must NOT count as success; it should escalate to a browser.
    content_ok = len(_visible_text(page).strip()) >= s.min_content_chars

    # Decide SUCCESS first. A 2xx with real text is the page -- and a *solved*
    # anti-bot challenge lands here too, so incidental challenge words in
    # legitimate content can't be mistaken for an unsolved wall.
    if status is not None and 200 <= status < 300 and content_ok:
        return Outcome.OK

    # Everything below is a block, an interstitial, or a thin/empty body.
    if status in (451, 429):
        return Outcome.GEO            # geo / IP rate-limit -> a different IP may help
    if _looks(_GEO_MARKERS, html):
        return Outcome.GEO
    if _looks(_CHALLENGE_MARKERS, html):
        return Outcome.CHALLENGE      # anti-bot wall -> escalate engine toward stealth
    if status in (401, 402, 403, 407):
        return Outcome.CHALLENGE      # ambiguous block -> escalate engine toward stealth
    if status is not None and status >= 500:
        return Outcome.ERROR
    if status is not None and 200 <= status < 400:
        return Outcome.EMPTY          # 2xx but thin body -> likely needs JS
    return Outcome.ERROR


def _extract(page, fmt: str, css_selector: Optional[str], main_content_only: bool) -> str:
    return "".join(
        Convertor._extract_content(
            page, extraction_type=fmt, css_selector=css_selector, main_content_only=main_content_only
        )
    )


async def _run_one(url: str, engine: str, proxy_url: Optional[str], s: Settings):
    """Run a single (engine, proxy) fetch in a worker thread. Never raises."""
    fn = _FETCHERS[engine]
    t0 = time.monotonic()
    try:
        if engine == "http":
            page = await run_in_threadpool(fn, url, proxy_url, s)
        else:
            async with _browser_limiter:  # gate heavy browser launches
                page = await run_in_threadpool(fn, url, proxy_url, s)
        html = str(page.html_content)
        return page, getattr(page, "status", None), html, None, int((time.monotonic() - t0) * 1000)
    except Exception as e:  # noqa: BLE001 — surface as a recorded attempt, keep escalating
        return None, None, "", f"{type(e).__name__}: {e}", int((time.monotonic() - t0) * 1000)


async def scrape(
    *,
    url: str,
    fmt: str,
    engine_mode: Engine,
    proxy_mode: ProxyMode,
    country: Optional[str],
    css_selector: Optional[str],
    main_content_only: bool,
) -> ScrapeResponse:
    s = get_settings()
    start = time.monotonic()
    deadline = start + s.request_max_seconds
    attempts: list[Attempt] = []

    have_proxy = proxy_available()
    proxy_on = proxy_mode == ProxyMode.on
    is_auto = engine_mode == Engine.auto
    sequence = ["http", "dynamic", "stealth"] if is_auto else [engine_mode.value]

    def proxy_url_for(on: bool) -> Optional[str]:
        return build_proxy_url(country) if on else None

    last = {"page": None, "status": None, "engine": None, "proxy": False}
    done: set[Tuple[str, bool]] = set()

    async def attempt(engine: str, use_proxy: bool):
        page, status, html, err, ms = await _run_one(url, engine, proxy_url_for(use_proxy), s)
        outcome = _classify(page, html, status, err, s)
        attempts.append(Attempt(engine=engine, proxy=use_proxy, status=status, outcome=outcome.value, elapsed_ms=ms, error=err))
        if page is not None:
            last.update(page=page, status=status, engine=engine, proxy=use_proxy)
        return outcome

    def finish(ok: bool):
        page = last["page"]
        content = _extract(page, fmt, css_selector, main_content_only) if page is not None else ""
        return ScrapeResponse(
            url=url, ok=ok, status=last["status"], engine_used=last["engine"],
            proxy_used=bool(last["proxy"]), format=fmt, content=content,
            attempts=attempts, elapsed_ms=int((time.monotonic() - start) * 1000),
        )

    idx = 0
    while idx < len(sequence):
        if time.monotonic() > deadline:
            break
        engine = sequence[idx]
        key = (engine, proxy_on)
        if key in done:
            idx += 1
            continue
        done.add(key)

        outcome = await attempt(engine, proxy_on)
        if outcome == Outcome.OK:
            return finish(True)

        # geo/IP block: turn the proxy on (sticky) and retry the SAME rung once.
        if outcome == Outcome.GEO and proxy_mode == ProxyMode.auto and have_proxy and not proxy_on:
            proxy_on = True
            continue
        # anti-bot wall: skip straight to the stealth rung.
        if outcome == Outcome.CHALLENGE and is_auto and engine != "stealth":
            idx = sequence.index("stealth")
            continue
        idx += 1

    # Nuclear fallback (auto + proxy=auto): stealth THROUGH the proxy — handles geo+anti-bot.
    if (is_auto and proxy_mode == ProxyMode.auto and have_proxy
            and ("stealth", True) not in done and time.monotonic() <= deadline):
        if await attempt("stealth", True) == Outcome.OK:
            return finish(True)

    return finish(False)
