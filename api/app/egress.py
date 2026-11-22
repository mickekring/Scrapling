"""Egress filter: the only way out for fetches that don't use the Apify proxy.

Every connection the service makes on its own goes through this small forward proxy on
127.0.0.1: the browser's (pages, iframes, every kind of worker, popups, WebSockets) and curl's.
For each one it resolves the host itself, refuses unless every address is public
(netguard.is_public, which honours ALLOW_PRIVATE_NETWORKS), and connects to an address it
checked. So nothing a page does can reach an internal address, not even a request whose answer
would never be read, and a DNS server that answers differently the second time (rebinding)
changes nothing.

Fetches through the Apify proxy don't come here: the proxy resolves and connects on its own
network, which is not ours.

It runs on its own thread and event loop, so the API's loop (which also parses pages) never
holds up anyone's download. HTTPS and WebSockets arrive as CONNECT tunnels, plain HTTP as
absolute-URI requests, each forwarded on its own connection ("Connection: close").
"""

import asyncio
import ipaddress
import logging
import socket
import threading
import time
from contextlib import suppress
from typing import Optional
from urllib.parse import urlsplit

from . import netguard

log = logging.getLogger("scrapling_api.egress")

REFUSED_HEADER = "X-Egress-Refused"  # on the 403 this proxy answers with when it refuses
_HEAD_LIMIT = 64 * 1024
_HEAD_SECONDS = 30
_CONNECT_SECONDS = 10
_IDLE_SECONDS = 300           # a connection with no traffic either way for this long is closed
_MAX_CONNECTIONS = 2000
_DNS_CACHE_SECONDS = 30
_HOP_BY_HOP = (b"proxy-", b"connection:", b"keep-alive:")


class EgressProxy:
    def __init__(self) -> None:
        self.port: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[asyncio.base_events.Server] = None
        self._exempt: set[tuple[str, int]] = set()
        self._dns: dict[str, tuple[float, list[str]]] = {}
        self._refused_hosts: dict[str, float] = {}  # host -> when it was last refused
        self.active = 0
        self.total = 0
        self.refused = 0
        self.failed = 0

    @property
    def url(self) -> str:
        if self.port is None:
            raise RuntimeError("Egress filter not started")
        return f"http://127.0.0.1:{self.port}"

    def exempt(self, host: str, port: int) -> None:
        """Let one exact internal address through (the watchdog's own probe page)."""
        self._exempt.add((host, port))

    def start(self) -> None:
        ready = threading.Event()
        problem: list[BaseException] = []

        def run() -> None:
            self._loop = loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                self._server = loop.run_until_complete(
                    asyncio.start_server(self._handle, "127.0.0.1", 0, limit=_HEAD_LIMIT, backlog=1024))
                self.port = self._server.sockets[0].getsockname()[1]
            except BaseException as e:  # noqa: BLE001 — reported to start() below
                problem.append(e)
                ready.set()
                return
            ready.set()
            loop.run_forever()

        threading.Thread(target=run, name="egress-filter", daemon=True).start()
        if not ready.wait(10) or problem:
            raise RuntimeError(f"egress filter did not start: {problem[0] if problem else 'timeout'}")
        log.info("Egress filter on 127.0.0.1:%d: only public addresses are reachable.", self.port)

    def stop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def refused_recently(self, host: Optional[str], within: float = 120) -> bool:
        """Did this filter refuse a connection to the host lately? Lets a fetch that failed
        because of it be reported as a refusal rather than a network error."""
        when = self._refused_hosts.get((host or "").strip("[]").rstrip(".").lower())
        return when is not None and time.monotonic() - when < within

    def stats(self) -> dict:
        return {"active": self.active, "total": self.total, "refused": self.refused, "failed": self.failed}

    # --- one client connection ----------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.total += 1
        if self.active >= _MAX_CONNECTIONS:
            await self._answer(writer, 503, "too many connections")
            return
        self.active += 1
        upstream_writer = None
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEAD_SECONDS)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ConnectionError):
                return
            line, _, rest = head.partition(b"\r\n")
            try:
                method, target, version = line.decode("latin-1").split(" ", 2)
            except ValueError:
                await self._answer(writer, 400, "bad request line")
                return

            if method == "CONNECT":
                host, port = _split_host_port(target)
                first = b""
            else:
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    await self._answer(writer, 400, "only absolute http:// URLs and CONNECT")
                    return
                host, port = parts.hostname, parts.port or 80
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                headers = [h for h in rest.split(b"\r\n") if h and not h.lower().startswith(_HOP_BY_HOP)]
                first = (f"{method} {path} {version}\r\n".encode("latin-1")
                         + b"".join(h + b"\r\n" for h in headers) + b"Connection: close\r\n\r\n")
            if not host or not port:
                await self._answer(writer, 400, "no host")
                return

            try:
                addresses = await self._resolve(host, port)
            except netguard.UnsafeDestination as e:
                self.refused += 1
                if len(self._refused_hosts) > 5000:
                    self._refused_hosts.clear()
                self._refused_hosts[host.strip("[]").rstrip(".").lower()] = time.monotonic()
                log.info("Refused %s:%d (%s).", host, port, e)
                await self._answer(writer, 403, str(e), refused=True)
                return
            except OSError:
                self.failed += 1
                await self._answer(writer, 502, f"cannot resolve {host}")
                return

            try:
                upstream_reader, upstream_writer = await _connect(addresses, port)
            except OSError as e:
                self.failed += 1
                await self._answer(writer, 502, f"cannot connect to {host}:{port} ({type(e).__name__})")
                return

            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                upstream_writer.write(first)
            await _relay(reader, writer, upstream_reader, upstream_writer)
        except Exception:  # noqa: BLE001 — one broken connection must never take the filter down
            log.debug("Egress connection failed.", exc_info=True)
        finally:
            self.active -= 1
            for w in (writer, upstream_writer):
                if w is not None:
                    with suppress(Exception):
                        w.close()

    async def _resolve(self, host: str, port: int) -> list[str]:
        """The addresses to connect to. Raises UnsafeDestination unless all of them are public."""
        host = host.strip("[]").rstrip(".").lower()
        if (host, port) in self._exempt:
            return [host]
        now = time.monotonic()
        cached = self._dns.get(host)
        if cached and cached[0] > now:
            addresses = cached[1]
        else:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(info[4][0] for info in infos))
            if len(self._dns) > 5000:
                self._dns.clear()
            self._dns[host] = (now + _DNS_CACHE_SECONDS, addresses)
        if not addresses or not all(netguard.is_public(a) for a in addresses):
            raise netguard.UnsafeDestination(f"{host} resolves to an address that is not public")
        # IPv4 first: some hosts have an IPv6 address that hangs instead of failing.
        return sorted(addresses, key=lambda a: ipaddress.ip_address(a.split("%")[0]).version)

    async def _answer(self, writer: asyncio.StreamWriter, code: int, reason: str, refused: bool = False) -> None:
        text = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway", 503: "Service Unavailable"}[code]
        extra = f"{REFUSED_HEADER}: {reason}\r\n" if refused else ""
        with suppress(Exception):
            writer.write(f"HTTP/1.1 {code} {text}\r\n{extra}Content-Length: 0\r\nConnection: close\r\n\r\n".encode("latin-1", "replace"))
            await writer.drain()


def _split_host_port(target: str) -> tuple[str, int]:
    host, _, port = target.rpartition(":")
    try:
        return host.strip("[]"), int(port)
    except ValueError:
        return "", 0


async def _connect(addresses: list[str], port: int):
    error: OSError = OSError("no address")
    for address in addresses[:3]:
        try:
            return await asyncio.wait_for(asyncio.open_connection(address, port, limit=_HEAD_LIMIT), _CONNECT_SECONDS)
        except (OSError, TimeoutError) as e:
            error = e if isinstance(e, OSError) else OSError(f"timed out connecting to {address}")
    raise error


async def _relay(client_reader, client_writer, upstream_reader, upstream_writer) -> None:
    """Copy bytes both ways until either side closes or the connection is idle too long."""
    last = [time.monotonic()]

    async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        while data := await src.read(65536):
            last[0] = time.monotonic()
            dst.write(data)
            await dst.drain()

    async def idle() -> None:
        while time.monotonic() - last[0] < _IDLE_SECONDS:
            await asyncio.sleep(15)

    tasks = [asyncio.ensure_future(c) for c in (pipe(client_reader, upstream_writer),
                                                pipe(upstream_reader, client_writer), idle())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


_proxy: Optional[EgressProxy] = None


def get_egress() -> EgressProxy:
    if _proxy is None:
        raise RuntimeError("Egress filter not initialised (app lifespan not running)")
    return _proxy


def init_egress() -> EgressProxy:
    global _proxy
    _proxy = EgressProxy()
    _proxy.start()
    return _proxy
