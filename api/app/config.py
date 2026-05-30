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

    # --- Fetch tuning -----------------------------------------------------
    http_timeout: int = 30               # seconds, HTTP (Fetcher) tier
    http_impersonate: str = "chrome"     # curl_cffi TLS fingerprint
    browser_network_idle: bool = True
    solve_cloudflare: bool = True
    min_html_chars: int = 200            # a 2xx page with less body than this is treated as empty / needs-JS
    max_concurrent_browsers: int = 2     # cap simultaneous browser launches (memory guard)
    request_max_seconds: int = 240       # overall per-request escalation budget (covers slow stealth sites)


@lru_cache
def get_settings() -> Settings:
    return Settings()
