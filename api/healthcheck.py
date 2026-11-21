"""Container health check: exit 0 if the API answers on /health, 1 otherwise.

The image has no curl or wget, so the Dockerfile's HEALTHCHECK runs this instead.
/health is deliberately cheap (it only proves the web server answers); a browser that
has stopped working is the watchdog's job, see app/watchdog.py.
"""

import sys
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=8) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:  # noqa: BLE001 — refused, timed out, bad answer: all unhealthy
    sys.exit(1)
