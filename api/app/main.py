"""FastAPI surface for the scrape service.

Endpoints:
  GET  /health         liveness probe (no auth) — for Coolify
  GET  /               service info (no auth)
  POST /scrape         main endpoint (JSON body)        [auth]
  GET  /scrape         convenience for quick tests/n8n  [auth]
  GET  /docs           interactive OpenAPI docs (FastAPI)
"""

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from .config import get_settings
from .models import Engine, Format, ProxyMode, ScrapeRequest, ScrapeResponse
from .proxy import proxy_available
from .scraper import scrape

log = logging.getLogger("scrapling_api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if not s.api_key:
        log.warning("API_KEY is not set — the API is OPEN. Set API_KEY before exposing this publicly.")
    if not proxy_available():
        log.warning("APIFY_PROXY_PASSWORD is not set — proxy modes 'on'/'auto' will run without a proxy.")
    log.info("Scrapling Scrape API ready (engine=%s, proxy=%s, format=%s).",
             s.default_engine, s.default_proxy, s.default_format)
    yield


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


@app.get("/", tags=["meta"])
async def root() -> dict:
    s = get_settings()
    return {
        "name": "Scrapling Scrape API",
        "version": app.version,
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
) -> ScrapeResponse:
    req = ScrapeRequest(
        url=url, format=format, engine=engine, proxy=proxy,
        country=country, css_selector=css_selector, main_content_only=main_content_only,
    )
    return await _scrape(req)
