"""FastAPI surface for the scrape service.

Endpoints:
  GET  /health         liveness probe (no auth) — for Coolify
  GET  /health/browser can a browser still be launched? (no auth) — for an uptime monitor
  GET  /               service info (no auth)
  POST /scrape         main endpoint (JSON body)        [auth]
  GET  /scrape         convenience for quick tests/n8n  [auth]
  GET  /docs           interactive OpenAPI docs (FastAPI)
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from scrapling import __version__ as SCRAPLING_VERSION

from .config import get_settings
from .models import Engine, Format, ProxyMode, ScrapeRequest, ScrapeResponse
from .proxy import proxy_available
from .scraper import browser_slots, scrape
from . import netguard, watchdog
from .browser import init_pool
from .egress import get_egress, init_egress

log = logging.getLogger("scrapling_api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if not s.api_key:
        if not s.allow_unauthenticated:
            # Refuse to run open by accident: anyone who can reach the port could make this
            # server fetch pages for them. uvicorn exits (code 3) and the deploy fails visibly.
            raise RuntimeError("API_KEY is not set. Set it, or set ALLOW_UNAUTHENTICATED=true "
                               "to run an open API on purpose (local development).")
        log.warning("API_KEY is not set and ALLOW_UNAUTHENTICATED=true — the API is OPEN.")
    if not proxy_available():
        log.warning("APIFY_PROXY_PASSWORD is not set — proxy modes 'on'/'auto' will run without a proxy.")
    log.info("Scrapling Scrape API ready on scrapling %s (engine=%s, proxy=%s, format=%s).",
             SCRAPLING_VERSION, s.default_engine, s.default_proxy, s.default_format)
    egress = init_egress()  # before any browser or curl: it is their only way out
    pool = init_pool(s)
    await pool.start()
    probe_server = await watchdog.start_probe_server()
    egress.exempt("127.0.0.1", watchdog.probe_port())  # the watchdog's own page, on this container's loopback
    task = None
    if s.watchdog_enabled:
        task = asyncio.create_task(watchdog.run(s))
        log.info("Browser watchdog on: probe every %ss, restart after %d consecutive failures.",
                 s.watchdog_interval_seconds, s.watchdog_max_failures)
    yield
    if task:
        task.cancel()
    probe_server.close()
    await pool.close()
    egress.stop()


app = FastAPI(
    title="Scrapling Scrape API",
    version="1.0.0",
    description="Send a URL, get HTML / text / markdown. Auto-escalates Fetcher → DynamicFetcher → "
                "StealthyFetcher and optionally routes through an Apify proxy for geo-blocked sites.",
    lifespan=lifespan,
)


async def require_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> None:
    """Enforce the API key when one is configured; otherwise allow (dev mode)."""
    expected = get_settings().api_key
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key (header: X-API-Key).")


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok"}


@app.get("/health/browser", tags=["meta"])
async def health_browser() -> JSONResponse:
    """200 while the last browser probe passed (or none has run yet), 503 while failing."""
    report = watchdog.report(get_settings())
    report["slots"] = browser_slots()
    report["egress"] = get_egress().stats()  # refused = connections to non-public addresses stopped
    return JSONResponse(report, status_code=503 if report["status"] == "failing" else 200)


@app.get("/", tags=["meta"])
async def root() -> dict:
    s = get_settings()
    return {
        "name": "Scrapling Scrape API",
        "version": app.version,
        # The pinned upstream version, so a deploy can be verified over HTTP.
        "scrapling_version": SCRAPLING_VERSION,
        "docs": "/docs",
        "proxy_configured": proxy_available(),
        "browser_mode": s.browser_mode,
        "defaults": {"engine": s.default_engine, "proxy": s.default_proxy, "format": s.default_format},
    }


async def _scrape(req: ScrapeRequest) -> ScrapeResponse:
    s = get_settings()
    try:
        await netguard.acheck_url(str(req.url))
    except netguard.UnsafeDestination as e:
        # Refused before anything is fetched. Redirects met later are refused in the ladder
        # and come back as a normal 200 response with outcome "unsafe_destination".
        raise HTTPException(status_code=400, detail={
            "error": "unsafe_destination",
            "message": f"This URL is not fetched: {e}.",
        })
    return await scrape(
        url=str(req.url),
        fmt=(req.format or Format(s.default_format)).value,
        engine_mode=req.engine or Engine(s.default_engine),
        proxy_mode=req.proxy or ProxyMode(s.default_proxy),
        country=req.country,
        css_selector=req.css_selector,
        main_content_only=req.main_content_only if req.main_content_only is not None else s.main_content_only,
        lean=req.lean if req.lean is not None else s.default_lean,
        pierce_shadow=req.pierce_shadow,
    )


@app.post("/scrape", response_model=ScrapeResponse, dependencies=[Depends(require_api_key)], tags=["scrape"])
async def scrape_post(req: ScrapeRequest) -> ScrapeResponse:
    return await _scrape(req)


@app.get("/scrape", response_model=ScrapeResponse, dependencies=[Depends(require_api_key)], tags=["scrape"])
async def scrape_get(
    url: str = Query(..., description="The page to fetch (http/https)."),
    format: Optional[Format] = None,
    engine: Optional[Engine] = None,
    proxy: Optional[ProxyMode] = None,
    country: Optional[str] = None,
    css_selector: Optional[str] = None,
    main_content_only: Optional[bool] = None,
    lean: Optional[bool] = None,
    pierce_shadow: bool = False,
) -> ScrapeResponse:
    req = ScrapeRequest(
        url=url, format=format, engine=engine, proxy=proxy, country=country, css_selector=css_selector,
        main_content_only=main_content_only, lean=lean, pierce_shadow=pierce_shadow,
    )
    return await _scrape(req)
