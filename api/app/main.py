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
from .scraper import scrape
from . import watchdog

log = logging.getLogger("scrapling_api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if not s.api_key:
        log.warning("API_KEY is not set — the API is OPEN. Set API_KEY before exposing this publicly.")
    if not proxy_available():
        log.warning("APIFY_PROXY_PASSWORD is not set — proxy modes 'on'/'auto' will run without a proxy.")
    log.info("Scrapling Scrape API ready on scrapling %s (engine=%s, proxy=%s, format=%s).",
             SCRAPLING_VERSION, s.default_engine, s.default_proxy, s.default_format)
    task = None
    if s.watchdog_enabled:
        task = asyncio.create_task(watchdog.run(s))
        log.info("Browser watchdog on: probe every %ss, restart after %d consecutive failures.",
                 s.watchdog_interval_seconds, s.watchdog_max_failures)
    yield
    if task:
        task.cancel()


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
        "defaults": {"engine": s.default_engine, "proxy": s.default_proxy, "format": s.default_format},
    }


async def _scrape(req: ScrapeRequest) -> ScrapeResponse:
    s = get_settings()
    return await scrape(
        url=str(req.url),
        fmt=(req.format or Format(s.default_format)).value,
        engine_mode=req.engine or Engine(s.default_engine),
        proxy_mode=req.proxy or ProxyMode(s.default_proxy),
        country=req.country,
        css_selector=req.css_selector,
        main_content_only=req.main_content_only if req.main_content_only is not None else s.main_content_only,
        lean=req.lean if req.lean is not None else s.default_lean,
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
) -> ScrapeResponse:
    req = ScrapeRequest(
        url=url, format=format, engine=engine, proxy=proxy,
        country=country, css_selector=css_selector, main_content_only=main_content_only, lean=lean,
    )
    return await _scrape(req)
