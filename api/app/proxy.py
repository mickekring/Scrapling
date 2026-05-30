"""Apify proxy URL construction.

A single proxy URL string works for all three Scrapling tiers: Fetcher passes it
to curl_cffi directly, and the browser fetchers run it through
`construct_proxy_dict()` into Playwright's format. We assemble it server-side so
the Apify password never has to be URL-encoded by the caller and never leaves
the server in a request body.
"""

from typing import Optional

from .config import get_settings


def proxy_available() -> bool:
    """True if a proxy password is configured."""
    return bool(get_settings().apify_proxy_password)


def build_proxy_url(country: Optional[str] = None) -> Optional[str]:
    """Build the Apify proxy URL, or None if no proxy is configured.

    Username encodes the config: `auto` for default behaviour, or `country-XX`
    (optionally combined with the configured base username) for geo-targeting.
    """
    s = get_settings()
    if not s.apify_proxy_password:
        return None

    username = (s.apify_proxy_username or "auto").strip()
    if country:
        token = f"country-{country.strip().upper()}"
        username = token if username == "auto" else f"{username},{token}"

    return f"http://{username}:{s.apify_proxy_password}@{s.apify_proxy_host}:{s.apify_proxy_port}"
