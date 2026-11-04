# Scrapling Scrape API

A thin, self-contained REST wrapper around [Scrapling](https://github.com/D4Vinci/Scrapling).
Send one URL, get back **HTML / text / markdown**. It auto-escalates through
Scrapling's three fetch tiers and can route through an **Apify proxy** for
geo-blocked sites. Built for calling from **n8n** and deploying on **Coolify** (or **Dokploy**).

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

- `http`: a plain request with Chrome's network fingerprint (curl_cffi). Milliseconds.
- `dynamic`: a real browser that runs the page's JavaScript.
- `stealth`: the same browser plus Scrapling's Cloudflare solver. Since Scrapling v0.5 both
  browser tiers run on the stealth (patchright) browser. `dynamic` skips the solver, because the
  solver costs ~5 s even on pages without a challenge (31 s → 126 s measured on gazeta.ru).

With `engine=auto` the service walks the engine ladder and **stops at the first
rung that returns real content**: a 2xx with at least `MIN_CONTENT_CHARS` (200) of visible
text. A challenge (403, "Just a moment…") jumps straight to `stealth`. The proxy is a
separate sticky decision:

- `proxy=off` — never use it.
- `proxy=on` — use it on every rung.
- `proxy=auto` — start without; the moment a **geo/IP signal** appears (HTTP 451/429,
  or a "not available in your region" page) it switches the proxy on for the rest
  of the run. A final `stealth + proxy` fallback covers the geo **and** anti-bot case.

Every rung tried is returned in `attempts[]` so you can see exactly how a URL was routed.
Its `outcome` is one of `ok`, `challenge`, `geo_block`, `empty`, `error` or `local_error`.
`local_error` means **this server** couldn't run the fetch (the browser failed to start or
died), not that the site blocked it.

### The browser (`BROWSER_MODE`)

- **`persistent`** (default). The browser tiers share one long-running browser whose tabs are
  reused, instead of launching Chromium for every attempt. Measured on 8 fetches: 0.76 s instead of
  2.0 s median, and 10 processes spawned instead of 78.
  - **Proxies:** a browser has a fixed proxy, so there is one for direct requests and one for the
    Apify proxy (started on first use). Requests with a specific `country` get a one-off browser.
  - **Cookies:** requests on the same browser share cookies. A solved Cloudflare clearance is
    reused, which makes repeat visits faster, but different callers visiting the same site share
    its cookies.
  - **Recycling:** the browser is replaced every `BROWSER_RECYCLE_AFTER` (200) pages, which bounds
    memory and cookie build-up, and immediately if it dies. In-flight requests finish on the old one.
- **`per_request`**: a fresh browser for every attempt (the behaviour before v0.5). The fallback
  if the persistent browser ever misbehaves.


If you already know what a site needs, set `engine`/`proxy`/`country` explicitly to
skip the ladder — fastest and cheapest.

## API

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET`  | `/health` | no | Liveness probe (Coolify) |
| `GET`  | `/health/browser` | no | Can a browser still be launched? 503 while failing — for an uptime monitor |
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
  "main_content_only": true,   // strip to <body>, drop script/style/hidden noise
  "lean": false,               // browser tiers skip heavy resources to save proxy bandwidth (see below)
  "pierce_shadow": false       // include open Shadow DOM (web components); browser tiers only
}
```

**`lean`** (default `false`, server default `DEFAULT_LEAN`). The browser tiers then block images,
media, fonts, stylesheets and ad hosts, and don't wait for network idle (pages with streaming players
never go idle). Instead they pause `LEAN_WAIT_MS` (2000 ms) for JS-built content. The `<head>` and the
rendered DOM are kept. If a lean rung comes back empty or blocked, the same rung is retried once in
full before escalating, so callers need no fallback of their own. One exception: in `engine=auto`, a
dynamic `challenge` escalates to stealth directly. `attempts[].lean` shows which mode produced each
result. The http tier is unaffected.

Measured on 2026-11-04, bytes through a counting proxy, `engine=dynamic`:

| Page | Full | Lean | Note |
|---|---|---|---|
| gazeta.ru | 5.44 MB, 20 s | 1.44 MB, 3 s | streaming player; same 121 links |
| edition.cnn.com | 13.4 MB | 7.1 MB | mostly JS, which lean keeps |
| aftonbladet.se | 2.6 MB | 1.3 MB | |
| svt.se | 1.3 MB | 0.9 MB | RSS `<link>` kept |
| tass.ru | 3.6 MB | — | lean gets an error page → automatic full retry |

The Cloudflare demo still solves with lean on (7–18 s, versus ~70 s in full mode).
Re-measured on v0.5 through the proxy session: gazeta.ru 5.4 MB → 1.1 MB, 20 s → 2.5 s, same 122 links.

**`pierce_shadow`** (default `false`). Content inside open Shadow DOM (web components) is invisible
to a normal page capture. With this flag it is included, as `<shadow-root>` elements. Shadow DOM
only exists after JavaScript runs, so with `engine=auto` the http tier is skipped. These requests
always get a one-off browser: in Scrapling 0.5, the first fetch on a reused tab after a Cloudflare
solve loses its shadow content.

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
    {"engine": "http", "proxy": false, "status": 403, "lean": false, "outcome": "challenge", "elapsed_ms": 412, "error": null},
    {"engine": "stealth", "proxy": true, "status": 200, "lean": false, "outcome": "ok", "elapsed_ms": 24180, "error": null}
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

**Troubleshooting: build fails at "Downloading Chrome for Testing … timed out".** The usual cause is a
server with broken IPv6: it has an IPv6 address, but IPv6 connections hang. Coolify's build uses the
server's own network, and Playwright's downloader there doesn't fall back to IPv4, so it times out. apt
and pip in the same build work fine. To confirm, run this on the server:
`curl -6 -sS -o /dev/null --max-time 10 https://cdn.playwright.dev/`. A timeout confirms it. Fix it on
the server by switching IPv6 off, which persists across reboots:

```bash
printf 'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\n' \
  > /etc/sysctl.d/99-disable-ipv6.conf && sysctl --system
```

Only do this if the domain's DNS has no AAAA record pointing at the server. Plain `curl` tests can look
fine, because curl falls back to IPv4. Test the host network the way the build does:
`docker run --rm --network host python:3.12-slim sh -c "pip install -q playwright && python -m playwright install chromium"`.

> Local testing still uses the compose file: uncomment its `ports:` block and run
> `docker compose up --build` (it already sets `shm_size: 1gb`).

> The image bundles Chromium (~1–1.5 GB); the first build takes a few minutes.

## Deploy on Dokploy

Dokploy splits Coolify's single *Base Directory* into **Dockerfile Path** and **Docker Context Path**,
and the context path is joined to the repo root *without* the Build Path. Set it explicitly:

1. **Build Type: `Dockerfile`**, branch **`feat/scrape-api`**.
2. **Dockerfile Path: `api/Dockerfile`** (or Build Path `/api` + `Dockerfile` — both resolve the same).
3. **Docker Context Path: `api`** — the Dockerfile COPYs `requirements.txt` and `app/` relative to it.
4. The rest as for Coolify: expose port `8000`, give Chromium `--shm-size=1g`, set the env vars.

**Do not leave Docker Context Path empty, and do not put `.` in it.** A literal `.` is always the
**repo root**, where `requirements.txt` and `app/` don't exist. An *empty* field has meant different
things in different Dokploy releases — the Dockerfile's own folder in some, the repo root in others,
and it is slated to become the repo root for good
([Dokploy#5417](https://github.com/Dokploy/dokploy/issues/5417)). An empty field is what broke a
deploy in September 2026 that had worked untouched three weeks earlier. `api` is the only value
that is stable across versions.

**Symptom of the wrong context:** the build log shows `load .dockerignore … transferring context: 978B`
(that is the repo-root `.dockerignore`, 936 bytes — the `api/` one is 89 bytes), then `load build
context … 2B`, and `COPY requirements.txt .` fails with `"/requirements.txt": not found`.

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
- **Self-healing**: `/health` only proves the web server answers. It stayed green through a 30-hour
  outage in which no browser could be launched. So the app also runs a watchdog. Every
  `WATCHDOG_INTERVAL_SECONDS` (300) it launches a headless browser on a local page. After
  `WATCHDOG_MAX_FAILURES` (3) failures in a row it exits with code 1, and the container's restart policy
  starts a fresh one. Docker and Coolify never restart a container just for being *unhealthy*, which
  is why the app exits on its own. `GET /health/browser` shows the last result, the error, the pids
  used vs the limit, and the zombie count. To get told when this happens, point an uptime monitor
  (e.g. Uptime Kuma) at it. For the restart to happen, the container's restart policy must not be
  `no`. Check with `docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' <container>`.
- **Zombie reaping**: the image runs `tini` as PID 1. Without an init, Chromium's exited helper
  processes accumulate as zombies (~80/min under load) until the container's pids limit is hit
  and the browser tiers stop working while the HTTP tier still answers. If you override the
  entrypoint, keep an init (`--init` / `init: true`).
- **Scrapling v0.5 output changes** (compared with 0.4.15): Markdown from several selected
  elements now has blank lines between them, and hidden-content cleaning keeps more visible
  elements (e.g. `opacity:0.95`) and removes more hidden ones. Exact-output comparisons may differ.
- **Concurrency**: browser tiers are capped by `MAX_CONCURRENT_BROWSERS`. Scale by
  running more containers, not more uvicorn workers.
- **Proxy cost**: a browser pulls every asset through the proxy. `country` support
  depends on your Apify plan; the default `auto` username uses datacenter IPs.
- Content extraction reuses `scrapling.core.shell.Convertor` (the same path
  Scrapling's MCP server uses), so output matches the `scrapling extract` CLI.
