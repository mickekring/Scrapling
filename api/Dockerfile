# Self-contained image for the Scrapling Scrape API.
# Build context = this /api folder (it does NOT need the scrapling source tree;
# scrapling is installed as a pinned PyPI dependency).
#
#   docker build -t scrapling-api ./api
#
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 1) Python deps (cached layer)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 2) Browsers + system libs for the Dynamic/Stealthy tiers.
#    playwright --with-deps installs the OS libraries; patchright reuses the same
#    PLAYWRIGHT_BROWSERS_PATH for StealthyFetcher.
RUN python -m playwright install --with-deps chromium \
 && python -m patchright install chromium

# 3) A real init as PID 1. Chromium's exited helpers (chrome, chrome_crashpad) are
#    reparented to PID 1 when their browser closes; uvicorn never wait()s for them,
#    so without an init they pile up as zombies until the container's pids limit is
#    hit and no browser can be spawned. tini reaps them.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# 4) Application code
COPY app ./app

EXPOSE 8000

# Single worker on purpose: browser fetches are heavy and are gated by an
# in-process CapacityLimiter. Scale out with more containers, not more workers.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
