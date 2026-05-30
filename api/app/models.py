"""Request / response schemas for the scrape endpoint."""

from enum import Enum
from typing import List, Optional

from pydantic import AnyHttpUrl, BaseModel, Field


class Format(str, Enum):
    html = "html"
    text = "text"
    markdown = "markdown"


class Engine(str, Enum):
    auto = "auto"        # escalate http -> dynamic -> stealth, stop at first real success
    http = "http"        # Fetcher (curl_cffi), no browser
    dynamic = "dynamic"  # DynamicFetcher (Playwright Chromium)
    stealth = "stealth"  # StealthyFetcher (patchright + Cloudflare solver)


class ProxyMode(str, Enum):
    off = "off"    # never use the proxy
    on = "on"      # always use the proxy
    auto = "auto"  # start without; switch on if a geo/IP block is detected (sticky)


class ScrapeRequest(BaseModel):
    url: AnyHttpUrl = Field(description="The page to fetch (http/https only).")
    format: Optional[Format] = Field(None, description="Output format. Defaults to the server's DEFAULT_FORMAT.")
    engine: Optional[Engine] = Field(None, description="Fetch tier or 'auto' to escalate. Defaults to DEFAULT_ENGINE.")
    proxy: Optional[ProxyMode] = Field(None, description="Proxy behaviour. Defaults to DEFAULT_PROXY.")
    country: Optional[str] = Field(None, description="ISO country code for Apify geo-targeting, e.g. 'US'. Requires proxy support on your plan.")
    css_selector: Optional[str] = Field(None, description="Optional CSS selector to extract only matching elements.")
    main_content_only: Optional[bool] = Field(None, description="Strip to <body> and remove script/style/hidden noise. Defaults to MAIN_CONTENT_ONLY.")


class Attempt(BaseModel):
    """One rung of the escalation ladder — surfaced for observability/debugging."""

    engine: str
    proxy: bool
    status: Optional[int] = None
    outcome: str
    elapsed_ms: int
    error: Optional[str] = None


class ScrapeResponse(BaseModel):
    url: str
    ok: bool = Field(description="True if a rung returned real content (2xx, no challenge, non-trivial body).")
    status: Optional[int]
    engine_used: Optional[str]
    proxy_used: bool
    format: str
    content: str
    attempts: List[Attempt] = Field(description="Every rung tried, in order — shows how the URL was routed.")
    elapsed_ms: int
