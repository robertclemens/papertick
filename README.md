# PaperTick

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)
![Node 24 LTS](https://img.shields.io/badge/node-24_LTS-green.svg)
![Docker Compose](https://img.shields.io/badge/docker-compose-2496ED.svg)

A production-grade **mock investment platform**: paper trading, historical backtesting,
IRS-aware account buckets, scheduled auto-investing, and a fully agent-accessible REST API.
No real money, real market discipline.

## Contents

- [Stack](#stack)
- [Project structure](#project-structure)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Features](#features)
- [Accounts & sign-in](#accounts--sign-in)
- [Security posture](#security-posture)
- [Using the API as an agent](#using-the-api-as-an-agent)
- [Market data modes](#market-data-modes)
- [Development](#development)
- [Services (docker-compose)](#services-docker-compose)
- [Deploying to production](#deploying-to-production)
- [Reverse proxy](#reverse-proxy)
- [Upgrading](#upgrading)
- [License](#license)
- [Contributing](#contributing)

## Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 16 (App Router) · React 19 · Tailwind CSS 4 · Recharts 3 · TypeScript 7 |
| Backend | Python 3.14 · FastAPI · SQLAlchemy 2 · Pydantic v2 |
| Database | PostgreSQL 18 (ACID ledger, `SELECT … FOR UPDATE` locking) |
| Queue / cache | Redis 8 + Celery (worker + beat) |
| Market data | Yahoo Finance (live) with deterministic synthetic fallback (offline-safe) |
| Infra | Docker Compose (rootless-Docker friendly, no privileged ports) |

Kept current with [`upgrade.sh`](upgrade.sh) — see [Upgrading](#upgrading).

## Project structure

```
papertick/
├── docker-compose.yml     # 6 services: db, redis, backend, worker, beat, frontend
├── .env.example           # every configuration variable, documented
├── backend/
│   ├── app/
│   │   ├── main.py        # FastAPI app, middleware, router registration
│   │   ├── config.py      # Settings (pydantic-settings, env-driven)
│   │   ├── models.py      # SQLAlchemy ORM models
│   │   ├── schemas.py     # Pydantic request/response models
│   │   ├── security.py    # Argon2id, JWTs, API keys, TOTP sealing
│   │   ├── deps.py        # auth dependencies, scopes, scenario resolution
│   │   ├── docs_ui.py     # themed, self-hosted Swagger UI + ReDoc
│   │   ├── init_db.py     # schema create/migrate + reference-data seed
│   │   ├── routers/       # one module per API resource
│   │   ├── services/      # business logic (trading, IRS rules, metrics, …)
│   │   └── workers/       # Celery app + scheduled tasks
│   ├── tests/             # pytest suite (sqlite + synthetic market data)
│   └── scripts/           # one-off/import scripts (e.g. Vanguard CSV import)
└── frontend/
    ├── app/               # Next.js App Router pages
    ├── components/        # shared React components
    └── lib/               # API client, formatting, client-side helpers
```

## Quick start

Requires Docker with the Compose plugin (`docker compose version`). No other
local dependencies are needed — Postgres, Redis, and both apps run in containers.

```bash
git clone https://github.com/robertclemens/papertick.git
cd papertick
cp .env.example .env
# fill in the three required secrets:
#   POSTGRES_PASSWORD=$(openssl rand -hex 24)
#   REDIS_PASSWORD=$(openssl rand -base64 36)
#   SECRET_KEY=$(openssl rand -hex 48)
docker compose up -d --build
```

- Web UI: http://localhost:3000
- API docs (OpenAPI/Swagger): http://localhost:3000/api/docs
- Health: http://localhost:3000/healthz

Everything is served through the `frontend` container on port 3000 (change it with
`FRONTEND_PORT`). The backend's own port is deliberately never published —
`frontend` proxies `/api/*` and `/healthz` to it over the internal Docker network,
which is also what makes the auth cookies first-party.

Create an account in the UI (password: 12+ chars, letters + digits), deposit cash into a
bucket, and trade. Watch startup with `docker compose logs -f backend`; the backend
healthcheck (and everything that depends on it) won't pass until schema init finishes.

## Configuration

Every setting is documented inline in [`.env.example`](.env.example) — copy it to `.env`
and edit. It is split into **required for production** first, then **optional** settings
that all have working defaults. Grouped by concern:

*Required for production*

- **Secrets** — `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `SECRET_KEY` (all three are
  needed for the stack to start at all, in any environment)
- **Environment** — `ENV` (development/production), `COOKIE_SECURE`, `FRONTEND_ORIGIN`,
  `ALLOWED_HOSTS`
- **Email** — `SMTP_*`, for the verification links that `ENV=production` makes mandatory

*Optional*

- **Deployment shape** — `BASE_PATH` (serve from a sub-folder — see
  [Reverse proxy](#reverse-proxy)), `FRONTEND_PORT`, `TRUSTED_PROXY_CIDRS`
- **Passkeys (WebAuthn)** — relying-party identity
- **Market data** — provider selection and optional paid API keys (see
  [Market data modes](#market-data-modes))
- **Market emulation** — `ENFORCE_MARKET_HOURS`, `ALLOW_BACKDATED_TRADES`
- **Simulated trading costs** — fees, slippage, risk-free rate, settlement yield,
  scenario retention
- **Sessions & login security** — token lifetimes, lockout thresholds
- **Optional demo account** — seeded only if `DEMO_MODE=true`

`docker-compose.yml` passes every one of these through to the `backend`, `worker`, and
`beat` containers with matching defaults, so `.env` is the single source of truth —
nothing needs editing in the compose file itself for normal configuration changes. The
two exceptions are `DATABASE_URL` and `REDIS_URL`, which compose builds from the
passwords above; they are documented at the bottom of `.env.example` for the case where
a backend process is run outside Compose.

## Features

- **Real-market emulation** — NYSE calendar and hours (9:30–16:00 ET, DST-correct,
  computed holidays): market orders placed off-hours queue for the next open;
  mutual funds are forward-priced at the daily closing NAV (4 PM ET cutoff), never
  intraday; limit orders only work while the market trades. Set
  `ENFORCE_MARKET_HOURS=false` for an always-open sandbox.
- **Settlement fund** — uninvested cash is not a bare balance: it sits in a federal
  money market fund modeled on VMFXX at a stable $1.00 NAV. Deposits and sale
  proceeds sweep in, purchases and withdrawals sweep out, and the balance accrues
  the fund's dividend daily, paid on the last day of each month. No settlement or
  clearing holds are modeled — swept-in cash is available immediately.
- **Dividends** — real dividend history is credited for shares held on each
  ex-date, including full backfill for backdated (backtest) positions, and flows
  into balances, performance, and the tax report. Reconciliation is idempotent and
  re-runs every few hours.
- **Tax lots & taxable income** — full lot tracking (fees in basis, backtests earn
  their real holding period), every sale split into short-/long-term gains, and a
  per-year tax report (gains, dividend income, IRA contributions by designation,
  withdrawals) with CSV export.
- **Cost-basis elections** (taxable brokerage only — IRAs have no capital-gains
  treatment and always use FIFO) — FIFO (default), LIFO, HIFO, MinTax (losses
  first), Average Cost (mutual funds only, per IRS; re-bases remaining shares),
  and Specific Lot ID with a per-sale lot picker. Set per account, per fund
  (Vanguard-style), or overridden on any single sale. Methods stay changeable
  and apply to future sales; average cost locks the averaged basis of existing
  shares once a sale has used it (IRS §1.1012-1(e)), and SpecID with no lots
  named at sale time falls back to FIFO. The method actually used is recorded
  on every order.
- **Options** — calls & puts on any underlying: long options, covered calls, and
  cash-secured puts (collateral reserved from buying power). Black-Scholes chains
  priced off the live underlying, weekly/monthly/LEAPS listings, automatic
  ITM exercise & assignment at expiration with IRS-style basis/proceeds
  adjustments, and plain-English explanations of every contract's rights,
  obligations, max loss and breakeven.
- **Statements** — monthly PDFs generated on the 1st for each completed month plus
  year-end statements with the full tax summary; letter-size, PaperTick masthead,
  archived immutably and downloadable from the Statements page.
- **IRS limit auto-maintenance** — a daily task carries the latest official
  contribution limits forward into new tax years as "projected" (flagged in the
  API/UI) until official figures are seeded, so limits never go missing at
  year-roll.
- **Open symbol universe** — any US-listed USD equity/ETF/mutual fund validated by
  the live data source is auto-registered on first use; unknown symbols are
  rejected. A curated seed carries category/region/expense-ratio/prospectus data.
  Search accepts **company or fund names** as well as tickers ("apple" → AAPL,
  "contrafund" → FCNTX), filtered to US listings.
- **Account buckets** — Taxable, Roth IRA, Traditional IRA, Rollover IRA per user.
- **Scenarios** — every account, order and statement lives in an independent
  "what-if" track (`X-Scenario-Id`); copy the current one, try a different
  strategy, and delete it without touching real data. Deletions are soft
  (30-day recovery window by default) and a deleted scenario is frozen — it
  stops trading, accruing dividends, and earning, without being purged.
- **IRS rule engine** — annual IRA limits shared across all IRAs (with age-50+ catch-up
  from your date of birth), prior-year designation between Jan 1 and Tax Day,
  rollovers exempt, over-limit deposits blocked with actionable errors.
  Limits are data (`irs_limits` table), seeded for 2024–2026.
- **Trading engine** — market & limit orders, buy/sell by dollars or shares
  (fractional to 6 dp), configurable slippage + flat fee, average-cost basis,
  realized/unrealized gains. All fills run inside DB transactions with row locks.
- **Backtesting** — place an order `as_of` a past date ("pretend I invested then");
  it fills at that day's close and flows into today's balances and performance.
  Off by default — set `ALLOW_BACKDATED_TRADES=true`.
- **Scheduled & recurring investing** — one-off future orders and recurring rules
  (daily, weekly, biweekly, monthly, quarterly, annually) executed by the Celery
  worker; rules can be edited (amount, symbol, cadence) with changes applying to
  future runs only, and failures are recorded on the order and counted on the rule.
  An optional "fund to my contribution limit" mode divides remaining IRA room
  across the runs left in the year.
- **Buying power & external funding** — available-to-trade is cash minus
  short-put collateral minus cash already committed to open orders, so the same
  dollars can never back two pending trades; resting sell orders reserve their
  shares the same way. Committed cash is also excluded from withdrawals and is
  surfaced on the dashboard, account pages, and trade ticket alongside the open
  orders holding it. When an account is short, the order draws the shortfall
  from the linked external bank and records it as a cash transfer; in an IRA
  those transfers count as contributions and stop at the annual limit.
  Per-account toggle.
- **Time in force** — limit orders carry an expiry (Day, or good-till-canceled
  at 30/60/90/180 days or 1 year, defaulting to 60 days). A worker sweep lapses
  them at expiry and releases the cash or shares they were holding.
- **Metrics** — portfolio value series rebuilt from the ledger, time-weighted return,
  annualized IRR (XIRR), cost basis, fees, net deposits.
- **Exports** — every History view downloads as CSV or Excel over the window the
  table is showing; a whole scenario exports to a JSON file signed with
  `SECRET_KEY`, so a hand-edited file is rejected on import.
- **Agentic API** — every feature is reachable over REST, documented by a live
  OpenAPI spec. Scoped API keys (`read`, `trade`) via `Authorization: Bearer ptk_…`
  cover reading and trading; creating accounts, managing scenarios and changing
  credentials need a signed-in session (see [below](#using-the-api-as-an-agent)).

## Accounts & sign-in

- **Passkeys (WebAuthn)** — register any number of passkeys and sign in
  passwordlessly with a device screen lock, biometric, or security key.
  Discoverable credentials, so no username is typed. Requires a secure context
  (HTTPS, or `localhost` in development).
- **Optional MFA** — TOTP authenticator apps with QR enrollment. Never required:
  each account chooses passkeys, TOTP, both, or neither.
- **Email verification** — required in `ENV=production` for signup and for email
  changes (the address only changes once the link is clicked); skipped in
  development. With no SMTP configured, links are written to the backend log.
- **Profile edits** — change email (password-confirmed) and date of birth. A
  birthdate change is checked against contribution history first and must be
  confirmed if it would move you across the age-50 catch-up threshold or turn a
  past contribution into an over-contribution.

## Security posture

- Argon2id password hashing (64 MiB, t=3) with strength checks and rehash-on-login.
- Short-lived JWT access tokens + rotating refresh tokens (hashed at rest,
  reuse detection revokes the whole session family).
- httpOnly, SameSite=Lax cookies, first-party via the Next.js proxy; `Secure` when
  `COOKIE_SECURE=true` behind TLS. The refresh cookie is scoped to the one route
  that consumes it, and a server-side origin check backs up SameSite on every
  cookie-authenticated state change.
- TOTP MFA — secret encrypted at rest (Fernet key HKDF-derived from `SECRET_KEY`),
  QR enrollment, password + code required to disable.
- Redis rate limiting on auth/trading endpoints; login lockout after repeated failures;
  timing-equalized login for unknown emails. Redis itself requires a password, so a
  process that reaches the network cannot clear a lockout or set a fill price.
- API keys stored as SHA-256 hashes; plaintext shown exactly once; scoped; revocable.
- The backend port is never published: only `frontend` is reachable from the host,
  so there is no route around TLS or the ingress. `ALLOWED_HOSTS` rejects a spoofed
  `Host`, and `X-Forwarded-For` is believed only from CIDRs you list in
  `TRUSTED_PROXY_CIDRS` (nothing, by default), so a caller cannot choose its own
  rate-limit bucket.
- Strict Pydantic validation with bounds on every money/shares input; request bodies
  capped at 8 MiB before parsing; only US-listed USD symbols the market-data source
  can confirm become tradable. Security headers and a strict CSP on both services;
  Swagger UI and ReDoc are served from the backend's own origin rather than a CDN;
  non-root containers with `no-new-privileges` and dropped capabilities.
- In `ENV=production` the API docs, ReDoc and the OpenAPI spec are served only to an
  authenticated caller — the full map of the attack surface is not handed to
  anonymous visitors.

## Using the API as an agent

```bash
# create a key in the UI (API Keys page), then:
export PT_KEY=ptk_...
export PT_URL=http://localhost:3000      # or https://yourdomain.example

curl -s -H "Authorization: Bearer $PT_KEY" $PT_URL/api/v1/portfolio/summary

# buy $500 of VOO at the current price
curl -s -X POST -H "Authorization: Bearer $PT_KEY" -H "Content-Type: application/json" \
  -d '{"account_id":"<uuid>","ticker":"VOO","side":"BUY","quantity_type":"DOLLARS","quantity":"500"}' \
  $PT_URL/api/v1/orders

# backtest: pretend you bought $10k of AAPL three years ago
# (needs ALLOW_BACKDATED_TRADES=true)
curl -s -X POST -H "Authorization: Bearer $PT_KEY" -H "Content-Type: application/json" \
  -d '{"account_id":"<uuid>","ticker":"AAPL","side":"BUY","quantity_type":"DOLLARS","quantity":"10000","as_of":"2023-08-29"}' \
  $PT_URL/api/v1/orders

# $500 of VOO every 1st of the month
curl -s -X POST -H "Authorization: Bearer $PT_KEY" -H "Content-Type: application/json" \
  -d '{"account_id":"<uuid>","ticker":"VOO","amount":"500","cadence":"MONTHLY","day_of_month":1}' \
  $PT_URL/api/v1/schedules
```

Note the base URL: the API is reached **through the frontend origin**, not a separate
backend port. Requests go to the same host and scheme the web UI is served on.

**Scopes.** An API key carries `read` and/or `trade` — enough to query everything and
to deposit, withdraw, trade, and schedule. Administration (creating or editing
accounts, cost-basis settings, scenarios, API keys, MFA and passkeys) requires the
`manage` scope, which only an interactive session holds. In practice: set the
accounts up in the UI, then let the agent run inside them.

Every response carries `X-Scenario-Id`/`X-Scenario-Name` so a caller always knows which
portfolio track answered; pin one explicitly with an `X-Scenario-Id` header or
`?scenario_id=` query param. The full, always-current contract is the live OpenAPI spec —
`GET /api/openapi.json`, or browse it interactively at `/api/docs` (Swagger) or
`/api/redoc` (ReDoc). Under `ENV=production` all three require an authenticated
caller, so pass your API key when fetching the spec.

## Market data modes

`MARKET_DATA_PROVIDER` in `.env`:

- `auto` (default) — provider chain: Polygon (if `POLYGON_API_KEY` set) → Alpaca
  (if `ALPACA_API_KEY_ID`/`ALPACA_API_SECRET` set) → Yahoo Finance (free) →
  synthetic. A failing source enters a 5-minute cool-down.
- `polygon` / `alpaca` — force a paid source (real-time where your plan allows).
- `yahoo` — free default; near-real-time but unofficial: may be delayed and has
  no SLA. Every quote is labeled with the provider that served it.
- `synthetic` — deterministic per-ticker geometric-brownian price paths and
  quarterly dividends since 2015. Identical across processes, no network needed.

Historical closes are **split-adjusted** everywhere (backtests through stock
splits keep correct share math). No free source guarantees exact real-time data —
for that, plug in a paid key.

## Development

```bash
# backend tests (sqlite + synthetic data, no services needed)
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q

# frontend
cd frontend && npm install && npm run dev   # expects a backend on localhost:8000
```

`npm run dev` proxies to `BACKEND_URL` (default `http://localhost:8000`). Compose does
not publish the backend's port, so either run the API yourself —

```bash
cd backend && SECRET_KEY=$(openssl rand -hex 48) .venv/bin/uvicorn app.main:app --reload
```

— or add `ports: ["127.0.0.1:8000:8000"]` to the `backend` service in a
`docker-compose.override.yml` while you work.

Schema is created and seeded automatically on backend start (`app/init_db.py`).
The tradable universe and IRS limits are seed data — extend them in `init_db.py`.

## Services (docker-compose)

| Service | Role | Port |
|---|---|---|
| `frontend` | Next.js UI; proxies `/api/*` and `/healthz` to the backend | `FRONTEND_PORT` (3000) → 3000 |
| `backend` | FastAPI (uvicorn, 2 workers) | internal only — not published |
| `worker` | Celery worker — fills scheduled/recurring/limit orders | — |
| `beat` | Celery beat — 60 s order sweep plus the daily/periodic maintenance jobs | — |
| `db` | PostgreSQL 18 + `pgdata` volume | internal only |
| `redis` | Cache, rate limits, Celery broker + `redisdata` volume | internal only |

## Deploying to production

1. Set `ENV=production` — this turns on required email verification for signup
   and email changes, and stops serving the API docs and OpenAPI spec anonymously.
2. Set `COOKIE_SECURE=true` and put the stack behind TLS — neither container
   terminates TLS itself, so a reverse proxy in front of the `frontend`
   service handles that; see [Reverse proxy](#reverse-proxy) for concrete
   Caddy/nginx/Apache configs.
3. Set `FRONTEND_ORIGIN` to the real public origin — it drives CORS, the
   cross-origin guard, WebAuthn relying-party checks, and links in verification
   emails. Scheme, host and port only, never a path; passkeys need a secure
   context, so this must be `https://` outside of `localhost` development. If the
   app is served from a sub-folder, that prefix goes in `BASE_PATH` instead.
4. Set `ALLOWED_HOSTS` to the hostname(s) the deployment answers to, so a spoofed
   `Host` header is rejected outright.
5. Configure `SMTP_*` so verification emails actually send (unset SMTP just
   logs the link server-side, which is fine for a private/demo deployment but
   not for real signups).
6. Generate fresh, unique secrets for that deployment — never reuse the
   `.env` from development. `POSTGRES_PASSWORD`, `REDIS_PASSWORD` and `SECRET_KEY`
   as shown in [Quick start](#quick-start); rotating `SECRET_KEY` invalidates all
   sessions, enrolled TOTP secrets and existing scenario exports, so treat it as a
   real credential.
7. Leave `DEMO_MODE=false` (the default) — it exists for local trial accounts only.
8. `docker compose up -d --build`. `pgdata`/`redisdata` are named volumes;
   back them up like any other stateful service.

## Reverse proxy

The `frontend` container is the **only** thing a reverse proxy needs to point at.
Its Next.js rewrites (`frontend/next.config.mjs`) already forward `/api/*` and
`/healthz` to the `backend` service over the internal Docker network — that's also
why auth cookies work as first-party (see [Security posture](#security-posture)).
So one upstream, one TLS certificate, one vhost: the web UI, the OpenAPI docs
(`/api/docs`, `/api/redoc`), and every `/api/v1/...` call an agent makes all arrive
through `frontend:3000`. There is no separate proxy path to configure for
`backend:8000` — compose does not publish it at all.

If the proxy runs on the same host as `docker compose`, bind the published port to
loopback so only the proxy — not the world — can reach it directly, e.g. in a
`docker-compose.override.yml`:

```yaml
services:
  frontend:
    ports:
      - "127.0.0.1:3000:3000"
```

Whichever proxy you use, forward `Host`, `X-Forwarded-For`, and `X-Forwarded-Proto`.
The app functions without them — `FRONTEND_ORIGIN` is a static setting, not derived
from request headers — but they are what upstream logging and client IPs are built
from, and `X-Forwarded-For` is required if you ever set `TRUSTED_PROXY_CIDRS`.
Get a certificate from Let's Encrypt (or any ACME CA) for the hostname you serve;
Caddy does this automatically.

### Two shapes

- **Own hostname** — `https://papertick.example`. Nothing extra to configure; this
  is the default.
- **Sub-folder of a shared domain** — `https://domain.example/papertick/`. Set
  `BASE_PATH=/papertick` in `.env` and rebuild (`docker compose up -d --build`):
  the prefix is compiled into the frontend bundle and route manifest, so a plain
  restart will not pick it up. The proxy must then pass the prefix through
  **unchanged** — do not strip it. Each config below shows both shapes.

`FRONTEND_ORIGIN` stays a bare origin in both cases (`https://domain.example`) —
an `Origin` header never carries a path, and CORS, the cross-origin guard and
WebAuthn all compare against it.

> **Sub-folder trade-off.** Cookies and passkeys are scoped to a *host*, not a
> path. On a shared domain the WebAuthn relying party is `domain.example`, so a
> passkey registered for PaperTick is offered by every app on that domain, and
> any of them can set cookies PaperTick will receive. PaperTick scopes its own
> session cookie down to `BASE_PATH`, but that is defense in depth, not isolation.
> A dedicated hostname is the stronger boundary; use a sub-folder when sharing a
> certificate or a single public entry point matters more.

### Caddy

Own hostname:

```caddyfile
yourdomain.example {
    reverse_proxy 127.0.0.1:3000
}
```

Sub-folder — `handle` (not `handle_path`, which would strip the prefix), with a
matcher that covers `/papertick` itself as well as everything under it:

```caddyfile
domain.example {
    @papertick path /papertick /papertick/*
    handle @papertick {
        reverse_proxy 127.0.0.1:3000
    }

    # whatever else this domain serves
    handle {
        reverse_proxy 127.0.0.1:8080
    }
}
```

Caddy provisions and renews the TLS certificate itself and sets the forwarding
headers above by default.

### nginx

Own hostname:

```nginx
server {
    listen 80;
    server_name yourdomain.example;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name yourdomain.example;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.example/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.example/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Sub-folder — inside the existing `443` server block for `domain.example`:

```nginx
    # ^~ so a regex location elsewhere in the vhost cannot claim these URLs.
    location ^~ /papertick {
        # No path on the proxy_pass URL: nginx then forwards the request URI
        # untouched. Adding a trailing "/" would strip /papertick and every
        # asset, route and API call underneath it would 404.
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
```

Provision the certificate with `certbot --nginx -d yourdomain.example` (or your
ACME client of choice) before the `443` block will start.

### Apache httpd

Requires `proxy`, `proxy_http`, `ssl`, `headers`, and `rewrite` enabled
(`a2enmod proxy proxy_http ssl headers rewrite` on Debian/Ubuntu).

Own hostname:

```apacheconf
<VirtualHost *:80>
    ServerName yourdomain.example
    RewriteEngine On
    RewriteCond %{HTTPS} off
    RewriteRule ^ https://%{HTTP_HOST}%{REQUEST_URI} [L,R=301]
</VirtualHost>

<VirtualHost *:443>
    ServerName yourdomain.example

    SSLEngine on
    SSLCertificateFile    /etc/letsencrypt/live/yourdomain.example/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/yourdomain.example/privkey.pem

    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:3000/
    ProxyPassReverse / http://127.0.0.1:3000/
    RequestHeader set X-Forwarded-Proto "https"
</VirtualHost>
```

Sub-folder — inside the existing `443` vhost for `domain.example`. The prefix
appears on **both** sides of `ProxyPass`, which is what keeps it on the
forwarded request:

```apacheconf
    ProxyPreserveHost On
    ProxyPass        /papertick http://127.0.0.1:3000/papertick
    ProxyPassReverse /papertick http://127.0.0.1:3000/papertick
    RequestHeader set X-Forwarded-Proto "https"
```

`ProxyPreserveHost` supplies `Host`; `mod_proxy` sets `X-Forwarded-For` on its own.

### Then

In `.env`, set `COOKIE_SECURE=true`, point `FRONTEND_ORIGIN` at the public origin,
list the hostname in `ALLOWED_HOSTS`, and add `BASE_PATH` if you went the
sub-folder route:

```dotenv
# own hostname
FRONTEND_ORIGIN=https://yourdomain.example
ALLOWED_HOSTS=yourdomain.example
COOKIE_SECURE=true
BASE_PATH=

# sub-folder
FRONTEND_ORIGIN=https://domain.example
ALLOWED_HOSTS=domain.example
COOKIE_SECURE=true
BASE_PATH=/papertick
```

Then rebuild — `docker compose up -d --build`. All four matter before cookies,
CORS and WebAuthn will accept the new origin (see
[Deploying to production](#deploying-to-production)), and `BASE_PATH` only takes
effect through a rebuild.

Agents then use `https://yourdomain.example/api/v1/...` (or
`https://domain.example/papertick/api/v1/...`) in place of `localhost:3000` in the
[API examples above](#using-the-api-as-an-agent) — same routes, same responses,
just through the proxied origin.

## Upgrading

Pulling a newer version of this repo and rebuilding is normally all that's needed:

```bash
git pull
diff .env.example .env   # check for newly-added variables and pick values for them
docker compose up -d --build
```

- **Schema changes are automatic.** `entrypoint.sh` runs `python -m app.init_db`
  before the API starts, which creates any new tables and applies small,
  idempotent migrations (column renames, new enum values) to existing ones —
  see the module docstring in `backend/app/init_db.py`. There's no separate
  migration command to remember.
- **There is no down-migration path.** Schema changes are additive and forward-only,
  so `pg_dump` the `db` volume before an upgrade that touches anything you'd
  regret losing:
  `docker compose exec db pg_dump -U papertick papertick > backup.sql`.
- **Always rebuild the frontend on a real upgrade** (`--build`, not just `up -d`).
  `BACKEND_URL`, `BASE_PATH` and other frontend config are baked in at image build
  time via Next.js, so a plain restart won't pick up compose or `.env` changes on
  that side.
- **New environment variables** ship with a default in both `.env.example` and
  `docker-compose.yml`, so an upgrade won't break an existing `.env` that's
  missing them — diff the two files after pulling to see what's new and worth
  setting explicitly.

### Dependency upgrades

[`upgrade.sh`](upgrade.sh) bumps Python packages, npm packages, and (under `--major`)
the four Docker base images, gated behind a test run (`pytest`, `tsc --noEmit`,
`next build`, then a containerized `/healthz` check) that auto-rolls-back on failure:

```bash
./upgrade.sh --check          # print the full dependency report and stop
./upgrade.sh                  # bump within current majors
./upgrade.sh --major          # cross major versions too (review changelogs after)
./upgrade.sh --major --force  # also lift the deliberate version holds (see the script header)
./upgrade.sh --skip-tests     # skip the test gate, so a broken upgrade is not caught
```

Every run, `--check` included, starts by printing the same full report of what is
outdated; `--check` simply stops there.

**Postgres major-version upgrades need a manual data migration** — a Postgres data
directory isn't binary-compatible across major versions, so swapping the image tag
alone will not work (the container simply refuses to start against an incompatible
volume; Postgres 18's image additionally changed its expected mount point from
`.../postgresql/data` to `.../postgresql` — already reflected in this repo's
`docker-compose.yml`). `upgrade.sh` deliberately does not automate this. The safe
sequence, if you're bumping the `db` image to a new major:

```bash
docker compose exec -T db pg_dump -U papertick -d papertick --format=custom > backup.dump
# point docker-compose.yml's pgdata volume at a new name so the old volume is left
# untouched as a fallback, then bring the new image up on the fresh volume:
docker compose up -d db
docker compose cp backup.dump db:/tmp/backup.dump
docker compose exec -T db pg_restore -U papertick -d papertick --no-owner --role=papertick /tmp/backup.dump
```

Verify row counts against the old volume before removing it — `docker run --rm -v
<old-volume>:<mountpoint> postgres:<old-tag> ...` spins up a throwaway read-only check.
Mount it where that image expects its data directory: `/var/lib/postgresql/data` up to
Postgres 17, `/var/lib/postgresql` from 18 on.

## License

[MIT](LICENSE) — see the `LICENSE` file for the full text.

## Contributing

Issues and pull requests are welcome. For anything non-trivial, please open an
issue first to discuss the approach before sending a PR. Run the backend test
suite (`cd backend && .venv/bin/python -m pytest tests/ -q`) before submitting.
