"""Which destinations may this service fetch? Only public internet addresses.

The service fetches URLs that come from strangers (feeds, social posts) from inside a hosting
network. Without this check, a link that points or redirects to an internal address
(10.x, 127.0.0.1, 169.254.169.254, a tailnet address, a Docker service name) would be fetched
from the inside and handed back to the caller as an article.

A destination is allowed only if EVERY address its host resolves to is a public one, or lies in
a network the operator listed in ALLOW_PRIVATE_NETWORKS. These checks give a clear answer early
(a refused URL, a refused redirect); the rule is enforced on every connection by the egress
filter (egress.py). A host that cannot be resolved is not refused here: the fetch then fails on
its own.
"""

import asyncio
import ipaddress
import logging
import socket
import time
from functools import lru_cache
from typing import Optional, Union
from urllib.parse import urlsplit

from .config import get_settings

IP = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
log = logging.getLogger("scrapling_api.netguard")

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_CACHE_SECONDS = 30  # short: an answer is only trusted briefly (DNS rebinding)
_cache: dict[str, tuple[float, Optional[str]]] = {}  # host -> (expires, reason it is refused or None)


class UnsafeDestination(Exception):
    """The destination is not a public internet address. Carries the reason as its message."""


@lru_cache
def _allowed_networks() -> tuple:
    entries = [e.strip() for e in get_settings().allow_private_networks.split(",") if e.strip()]
    return tuple(ipaddress.ip_network(e, strict=False) for e in entries)


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> Optional[ipaddress.IPv4Address]:
    """The IPv4 address an IPv6 address stands for, if any (::ffff:a.b.c.d, 6to4, Teredo, NAT64)."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public(address: Union[str, IP]) -> bool:
    """True for an address on the public internet, or in a network the operator allowed."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0]) if isinstance(address, str) else address
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and (v4 := _embedded_ipv4(ip)) is not None:
        ip = v4
    if any(ip.version == net.version and ip in net for net in _allowed_networks()):
        return True
    # is_global is False for private, loopback, link-local, CGNAT (100.64/10), reserved and
    # unspecified addresses. Multicast is "global" to Python, and never a web page.
    return ip.is_global and not ip.is_multicast


def _refusal(host: str, addresses: list[str]) -> Optional[str]:
    bad = [a for a in addresses if not is_public(a)]
    if not bad:
        return None
    # The address itself only goes to the log: callers needn't learn the internal network's layout.
    log.info("Refusing %s: it resolves to %s.", host, bad[0])
    return f"{host} resolves to an address that is not public"


def _resolve(host: str) -> list[str]:
    """Every address the host resolves to (blocking). getaddrinfo also parses odd numeric
    forms the way the fetchers would: 2130706433, 0x7f.1, 127.1."""
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})


def check_host(host: str) -> None:
    """Raise UnsafeDestination unless every address of the host is public (blocking)."""
    host = host.strip("[]").rstrip(".").lower()
    now = time.monotonic()
    cached = _cache.get(host)
    if cached is None or cached[0] < now:
        try:
            reason = _refusal(host, _resolve(host))
        except (OSError, UnicodeError):
            return  # cannot be resolved: nothing to refuse (and nothing to cache)
        if len(_cache) > 5000:
            _cache.clear()
        cached = _cache[host] = (now + _CACHE_SECONDS, reason)
    if cached[1]:
        raise UnsafeDestination(cached[1])


def check_url(url: str) -> None:
    """Raise UnsafeDestination unless the URL is http(s) to a public host (blocking)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeDestination(f"only http and https are fetched, not {parts.scheme or 'this'}")
    if not parts.hostname:
        raise UnsafeDestination("the URL has no host")
    check_host(parts.hostname)


async def acheck_url(url: str) -> None:
    """check_url without blocking the event loop (DNS runs in a worker thread)."""
    await asyncio.get_running_loop().run_in_executor(None, check_url, url)


async def acheck_host(host: str) -> None:
    cached = _cache.get(host.strip("[]").rstrip(".").lower())
    if cached is not None and cached[0] >= time.monotonic():
        if cached[1]:
            raise UnsafeDestination(cached[1])
        return  # answered from the cache without a thread hop: this runs for every request a page makes
    await asyncio.get_running_loop().run_in_executor(None, check_host, host)
