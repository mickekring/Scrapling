"""Apify proxy URL construction.

A single proxy URL string works for all three Scrapling tiers: Fetcher passes it
to curl_cffi directly, and the browser fetchers run it through
`construct_proxy_dict()` into Playwright's format. We assemble it server-side so
the Apify password never has to be URL-encoded by the caller and never leaves
the server in a request body.
"""

import re
from typing import Optional
from urllib.parse import quote

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
        # Two letters only: anything else could rewrite the proxy URL itself (`X@10.0.0.5:8080/#`
        # made an internal host the proxy). The request model enforces this too.
        if not re.fullmatch(r"[A-Za-z]{2}", country.strip()):
            raise ValueError("country must be a two-letter code")
        token = f"country-{country.strip().upper()}"
        username = token if username == "auto" else f"{username},{token}"

    # Percent-encoded: curl and Scrapling decode the credentials again.
    return (f"http://{quote(username, safe=',-_')}:{quote(s.apify_proxy_password, safe='')}"
            f"@{s.apify_proxy_host}:{s.apify_proxy_port}")
