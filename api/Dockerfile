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

# 3) Application code
COPY app ./app

EXPOSE 8000

# Single worker on purpose: browser fetches are heavy and are gated by an
# in-process CapacityLimiter. Scale out with more containers, not more workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
