"""Container health check: exit 0 if the API answers on /health, 1 otherwise.

The image has no curl or wget, so the Dockerfile's HEALTHCHECK runs this instead.
/health is deliberately cheap (it only proves the web server answers); a browser that
has stopped working is the watchdog's job, see app/watchdog.py.

http.client on purpose, NOT urllib.request: urllib treats every environment variable
that ends in `_proxy` as a proxy setting, so this service's own DEFAULT_PROXY=auto made
it send the request to a proxy host named "auto". That took a deployment out of routing
(Nov 2026). http.client never looks at the environment.

A failure prints its reason, which `docker inspect` shows under .State.Health.Log.
"""

import http.client
import sys

try:
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=8)
    connection.request("GET", "/health")
    status = connection.getresponse().status
except Exception as e:  # noqa: BLE001 — refused, timed out, bad answer: all unhealthy
    sys.exit(f"healthcheck: {type(e).__name__}: {e}")
if status != 200:
    sys.exit(f"healthcheck: /health answered HTTP {status}")
