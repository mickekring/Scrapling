# Self-contained image for the Scrapling Scrape API.
# Build context = this /api folder (it does NOT need the scrapling source tree;
# scrapling is installed as a pinned PyPI dependency).
#
#   docker build -t scrapling-api ./api
#
# Python 3.13: the newest version upstream Scrapling tests, with prebuilt wheels for every
# dependency on amd64 and arm64 (3.15 has no orjson wheel yet). 3.12 no longer gets the
# CPython fixes scanners flag. Debian is pinned (trixie) so a new release can't slip in.
FROM python:3.13-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 1) Python deps (cached layer)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 2) Browsers + system libs for the Dynamic/Stealthy tiers.
#    Since Scrapling v0.5 every browser fetch runs on patchright, so only its Chromium is
#    installed (--with-deps adds the OS libraries). The playwright package is still pulled
#    in by scrapling[fetchers], but its browser is not needed.
RUN python -m patchright install --with-deps chromium

# 3) Debian security fixes, and a real init as PID 1.
#    The official python image can lag Debian's security updates (Nov 2026: zlib1g and
#    apt), so upgrade its packages here. Docker reuses this layer from cache, so to pick
#    up newer fixes, rebuild without the build cache.
#    Chromium's exited helpers (chrome, chrome_crashpad) are reparented to PID 1 when
#    their browser closes; uvicorn never wait()s for them, so without an init they pile
#    up as zombies until the container's pids limit is hit and no browser can be
#    spawned. tini reaps them.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# 4) Application code
COPY app ./app
COPY healthcheck.py ./

EXPOSE 8000

# Lets Docker (and Coolify's proxy and rolling deploys) see whether the API answers.
# A script, because the image has no curl or wget. Lenient on purpose: a container that
# fails this is taken out of routing, and a busy server may answer slowly.
# On Coolify this is used only while the dashboard's own Healthcheck is disabled.
# Test it with the production environment variables set (see healthcheck.py for why).
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

# Single worker on purpose: browser fetches are heavy and are gated by an
# in-process CapacityLimiter. Scale out with more containers, not more workers.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
