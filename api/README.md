# Scrapling Scrape API

A thin, self-contained REST wrapper around [Scrapling](https://github.com/D4Vinci/Scrapling).
Send one URL, get back **HTML / text / markdown**. It auto-escalates through
Scrapling's three fetch tiers and can route through an **Apify proxy** for
geo-blocked sites. Built for calling from **n8n** and deploying on **Coolify**.

> **It lives outside the library on purpose.** This folder only *imports*
> `scrapling` as a pinned dependency (see [requirements.txt](requirements.txt)) and
> never edits the upstream source. You can keep pulling from `D4Vinci/Scrapling`
> without conflicts. To upgrade Scrapling, bump the pin and re-test.

## How routing works

Two **orthogonal** axes — they fix different problems:

| Axis | Options | Fixes |
|------|---------|-------|
| **Engine** | `http` → `dynamic` → `stealth` | JavaScript rendering, anti-bot (Cloudflare) |
| **Proxy** | `off` / `on` / `auto` | Geo-blocks, IP bans, rate-limits |

With `engine=auto` the service walks the engine ladder and **stops at the first
rung that returns real content** (2xx, no challenge fingerprint, non-trivial
body). The proxy is a separate sticky decision:

- `proxy=off` — never use it.
- `proxy=on` — use it on every rung.
- `proxy=auto` — start without; the moment a **geo/IP signal** appears (HTTP 451/429,
  or a "not available in your region" page) it switches the proxy on for the rest
  of the run. A final `stealth + proxy` fallback covers the geo **and** anti-bot case.

Every rung tried is returned in `attempts[]` so you can see exactly how a URL was routed.

If you already know what a site needs, set `engine`/`proxy`/`country` explicitly to
skip the ladder — fastest and cheapest.

## API

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET`  | `/health` | no | Liveness probe (Coolify) |
| `GET`  | `/` | no | Service info + the deployed `scrapling_version` |
| `POST` | `/scrape` | yes | Main endpoint |
| `GET`  | `/scrape` | yes | Convenience (query params) |
| `GET`  | `/docs` | no | Interactive OpenAPI docs |

Auth: send `X-API-Key: <API_KEY>`. (If `API_KEY` is unset the API is open — fine
for local dev, **set it before exposing publicly**.)

### Request (POST `/scrape`)

```json
{
  "url": "https://example.com",
  "format": "markdown",        // html | text | markdown   (default: markdown)
  "engine": "auto",            // auto | http | dynamic | stealth
  "proxy": "auto",             // off | on | auto
  "country": "US",             // optional, Apify geo-targeting
  "css_selector": null,        // optional, extract only matching elements
  "main_content_only": true    // strip to <body>, drop script/style/hidden noise
}
```

### Response

```json
{
  "url": "https://example.com",
  "ok": true,
  "status": 200,
  "engine_used": "stealth",
  "proxy_used": true,
  "format": "markdown",
  "content": "# Example ...",
  "attempts": [
    {"engine": "http", "proxy": false, "status": 403, "outcome": "challenge", "elapsed_ms": 412, "error": null},
    {"engine": "stealth", "proxy": true, "status": 200, "outcome": "ok", "elapsed_ms": 24180, "error": null}
  ],
  "elapsed_ms": 24592
}
```

## Run locally

```bash
cd api
cp .env.example .env        # then edit it
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium && python -m patchright install chromium
uvicorn app.main:app --reload --port 8000
```

```bash
# fast HTTP tier
curl -s -H "X-API-Key: $API_KEY" \
  "http://localhost:8000/scrape?url=https://quotes.toscrape.com/&format=markdown"

# full auto + proxy on a Cloudflare page
curl -s -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"url":"https://nopecha.com/demo/cloudflare","engine":"auto","proxy":"auto"}' \
  http://localhost:8000/scrape
```

## Docker (local)

`scrapling` is installed by the **Dockerfile** (`pip install -r requirements.txt`),
not by compose — compose only builds & runs that image.

```bash
cp .env.example .env                       # set API_KEY etc.
# uncomment the `ports:` block in docker-compose.yml (Coolify uses `expose` instead)
docker compose up --build                  # -> http://localhost:8000
```

## Deploy on Coolify

Use the **Dockerfile build pack** (not Docker Compose). Coolify's compose deploys have a known
bug where the Traefik `loadbalancer.server.port` label isn't generated for a custom port, which
yields a **"no available server"** error; the Dockerfile build pack sets the port explicitly and
avoids it.

1. **New Resource → Application → (Public/Private) Git Repository**, branch **`feat/scrape-api`**.
2. **Build Pack: `Dockerfile`.**
3. **Base Directory: `/api`** → Coolify builds `/api/Dockerfile` with context `/api`.
4. **Ports Exposes: `8000`** — the key field; this is the backend port Coolify routes to.
5. **Domain: `https://<your-domain>`** — no `:8000` (the Ports Exposes field already sets the port).
6. **Custom Docker Options: `--shm-size=1g`** — Chromium needs more than Docker's 64MB default `/dev/shm`.
7. **Environment Variables**: `API_KEY` (generate one), `APIFY_PROXY_PASSWORD`, optional `DEFAULT_PROXY=auto`.
8. *(optional)* Health check: path `/health`, port `8000`.
9. Deploy → cert provisions in ~1 min → `https://<your-domain>/health` → `{"status":"ok"}`.

**Troubleshooting "no available server":** the app itself is fine if `curl localhost:8000/health`
works from the service's terminal in Coolify — it's purely proxy/port routing. With the Dockerfile
build pack make sure **Ports Exposes = 8000**. (The Compose build pack instead needs the port on the
domain, `https://host:8000`, but can still hit the label bug above.)

> Local testing still uses the compose file: uncomment its `ports:` block and run
> `docker compose up --build` (it already sets `shm_size: 1gb`).

> The image bundles Chromium (~1–1.5 GB); the first build takes a few minutes.

## Calling from n8n

Use the **HTTP Request** node:
- Method `POST`, URL `https://<your-coolify-host>/scrape`
- Header `X-API-Key: <your key>`
- Body type *JSON*: `{ "url": "{{ $json.url }}", "format": "markdown", "engine": "auto", "proxy": "auto" }`
- Read the result from `{{ $json.content }}`.

The stealth tier can take ~30–90 s (a Cloudflare solve measured 75 s on the Coolify host), so raise the node's timeout accordingly
(Settings → Timeout) to comfortably exceed `REQUEST_MAX_SECONDS`.

## Notes & limits

- **SSRF**: the service fetches arbitrary URLs. It accepts only `http`/`https`
  and the HTTP tier uses `follow_redirects="safe"` (rejects redirects to private
  IPs). Keep the API behind its API key; consider an allow-list if exposed widely.
- **Concurrency**: browser tiers are capped by `MAX_CONCURRENT_BROWSERS`. Scale by
  running more containers, not more uvicorn workers.
- **Proxy cost**: a browser pulls every asset through the proxy. `country` support
  depends on your Apify plan; the default `auto` username uses datacenter IPs.
- Content extraction reuses `scrapling.core.shell.Convertor` (the same path
  Scrapling's MCP server uses), so output matches the `scrapling extract` CLI.
