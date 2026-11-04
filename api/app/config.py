"""Environment-driven settings (12-factor). All values come from env vars /
the .env file, so nothing secret is ever committed."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Auth -------------------------------------------------------------
    # Clients must send this as the `X-API-Key` header. If left blank the API
    # is OPEN (handy for local dev) and a loud warning is logged at startup.
    api_key: str = ""

    # --- Apify proxy (datacenter / "ordinary", username = auto) ----------
    # Leave the password blank to disable proxying entirely.
    apify_proxy_password: str = ""
    apify_proxy_host: str = "proxy.apify.com"
    apify_proxy_port: int = 8000
    apify_proxy_username: str = "auto"  # base username; `country-XX` is appended when requested

    # --- Request defaults (overridable per request) -----------------------
    default_format: str = "markdown"     # html | text | markdown
    default_engine: str = "auto"         # auto | http | dynamic | stealth
    default_proxy: str = "off"           # off | on | auto
    main_content_only: bool = True
    default_lean: bool = False

    # --- Fetch tuning -----------------------------------------------------
    http_timeout: int = 30               # seconds, HTTP (Fetcher) tier
    http_impersonate: str = "chrome"     # curl_cffi TLS fingerprint
    browser_network_idle: bool = True
    solve_cloudflare: bool = True
    browser_retries: int = 2             # per browser-fetch internal retries (Scrapling default 3). Each retry is a full ~60-130s cycle on hard sites, so 3 can blow past client timeouts; 2 keeps a single call bounded. Let n8n retry the rest.
    min_content_chars: int = 200         # a 2xx page with less VISIBLE TEXT than this is "empty" (JS shell / 200-status anti-bot interstitial) -> escalate
    max_concurrent_browsers: int = 2     # cap simultaneous browser launches (memory guard)
    request_max_seconds: int = 240       # overall per-request escalation budget (covers slow stealth sites)
    lean_wait_ms: int = 2000             # lean browser rungs skip network idle; this fixed pause lets JS-built content render instead

    # --- Browser watchdog (see app/watchdog.py) ---------------------------
    # Periodically launches a headless browser; after N consecutive failures the
    # process exits so Docker's restart policy starts a fresh container.
    watchdog_enabled: bool = True
    watchdog_interval_seconds: int = 300
    watchdog_max_failures: int = 3       # ~15 min of failures at the default interval
    watchdog_probe_timeout_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
