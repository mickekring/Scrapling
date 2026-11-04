"""Browser watchdog: restart the container when the browser tiers stop working.

`/health` only proves the web server answers. The failure that actually took the
service down (Oct 2026) left the HTTP tier working while no browser could be
spawned for 30 hours, and Docker never restarts a container just because it is
unhealthy. So this module periodically fetches a tiny page served by this process
on 127.0.0.1 (no internet involved) through the SAME browser path the scrape tiers
use (app/browser.py), and after `watchdog_max_failures` consecutive failures exits
the process with a non-zero code. tini then exits and the container's restart
policy starts a fresh one.
"""

import asyncio
import logging
import os
import time
from typing import Optional

import anyio

from .browser import get_pool
from .config import Settings

log = logging.getLogger("scrapling_api.watchdog")

_FIRST_PROBE_DELAY = 30  # seconds after startup, so /health/browser has data early
_PROBE_MARKER = "scrapling-watchdog-probe"
_PROBE_PAGE = (f"<!doctype html><html><head><title>probe</title></head>"
               f"<body><p id=probe>{_PROBE_MARKER}</p></body></html>").encode()
_probe_port: Optional[int] = None


class _State:
    last_probe_at: Optional[float] = None
    last_ok_at: Optional[float] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0


state = _State()


async def _serve_probe_page(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                     b"Content-Length: " + str(len(_PROBE_PAGE)).encode() + b"\r\nConnection: close\r\n\r\n"
                     + _PROBE_PAGE)
        await writer.drain()
    except Exception:  # noqa: BLE001 — a broken probe connection just fails that probe
        pass
    finally:
        writer.close()


async def start_probe_server() -> asyncio.base_events.Server:
    """Serve the probe page on 127.0.0.1 at a random free port (never exposed outside the container)."""
    global _probe_port
    server = await asyncio.start_server(_serve_probe_page, "127.0.0.1", 0)
    _probe_port = server.sockets[0].getsockname()[1]
    return server


async def _probe_browser(timeout_ms: int) -> None:
    """Fetch the probe page through the scrape tiers' browser path. Raises on any failure."""
    if _probe_port is None:
        raise RuntimeError("probe server not started")
    page = await get_pool().fetch(f"http://127.0.0.1:{_probe_port}/", None, network_idle=False,
                                  google_search=False, timeout=timeout_ms)
    if _PROBE_MARKER not in page.get_all_text(strip=True):
        raise RuntimeError("probe page rendered unexpected content")


async def probe_once(s: Settings) -> bool:
    """Run one probe with a hard timeout; record the result. Never raises."""
    timeout = s.watchdog_probe_timeout_seconds
    state.last_probe_at = time.time()
    try:
        with anyio.fail_after(timeout):
            await _probe_browser(timeout * 1000)
    except Exception as e:  # noqa: BLE001 — any failure to launch/render counts
        state.consecutive_failures += 1
        state.last_error = f"{type(e).__name__}: {e}"[:500]
        log.warning("Browser probe failed (%d/%d): %s", state.consecutive_failures,
                    s.watchdog_max_failures, state.last_error)
        return False
    if state.consecutive_failures:
        log.info("Browser probe recovered after %d failure(s).", state.consecutive_failures)
    state.consecutive_failures = 0
    state.last_error = None
    state.last_ok_at = time.time()
    return True


async def run(s: Settings) -> None:
    """Probe forever; exit the process after too many consecutive failures."""
    await asyncio.sleep(min(_FIRST_PROBE_DELAY, s.watchdog_interval_seconds))
    while True:
        await probe_once(s)
        if state.consecutive_failures >= s.watchdog_max_failures:
            log.critical("Browser probe failed %d times in a row — exiting so the container restarts. "
                         "Last error: %s", state.consecutive_failures, state.last_error)
            logging.shutdown()
            os._exit(1)  # non-zero, immediate: in-flight browser work is already failing
        await asyncio.sleep(s.watchdog_interval_seconds)


def _read_cgroup(name: str) -> Optional[str]:
    try:
        with open(f"/sys/fs/cgroup/{name}") as f:
            return f.read().strip()
    except OSError:
        return None


def _zombie_count() -> Optional[int]:
    if not os.path.isdir("/proc/self"):
        return None
    zombies = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                # state is the field after the parenthesised command name
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    zombies += 1
        except (OSError, IndexError):
            continue
    return zombies


def report(s: Settings) -> dict:
    """Snapshot for /health/browser. Contains no secrets."""
    pids_current = _read_cgroup("pids.current")
    pids_max = _read_cgroup("pids.max")

    def iso(ts: Optional[float]) -> Optional[str]:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None

    if not s.watchdog_enabled:
        status = "disabled"
    elif state.last_probe_at is None:
        status = "starting"
    else:
        status = "ok" if state.consecutive_failures == 0 else "failing"
    return {
        "status": status,
        "last_probe_at": iso(state.last_probe_at),
        "last_ok_at": iso(state.last_ok_at),
        "consecutive_failures": state.consecutive_failures,
        "restart_after_failures": s.watchdog_max_failures,
        "last_error": state.last_error,
        "browser": get_pool().stats(),
        "pids": {
            "current": int(pids_current) if pids_current and pids_current.isdigit() else None,
            "max": int(pids_max) if pids_max and pids_max.isdigit() else pids_max,
        },
        "zombies": _zombie_count(),
    }
