# Dependencies

Nothing is vendored and nothing is implicit — this is the complete list, and every
version is pinned exactly. Runtime images come from the Compose file; language
dependencies from [`backend/requirements.txt`](../backend/requirements.txt) and
[`frontend/package.json`](../frontend/package.json). Bumping them is
[`upgrade.sh`](../upgrade.sh)'s job — see [Upgrading](upgrading.md).

## Runtime images

| Image | Version | Role |
|---|---|---|
| `python` | 3.14-slim | Backend, Celery worker, Celery beat |
| `node` | 24-alpine | Next.js build and server (multi-stage, standalone output) |
| `postgres` | 18-alpine | Ledger of record — ACID, row-level locking |
| `redis` | 8-alpine | Celery broker, market-data cache, rate limits, WebAuthn challenges |

## Backend — Python

| Package | Version | What it does here |
|---|---|---|
| `fastapi` | 0.141.1 | HTTP routing, dependency injection, OpenAPI generation |
| `uvicorn[standard]` | 0.52.4 | ASGI server |
| `SQLAlchemy` | 2.0.52 | ORM and the `SELECT … FOR UPDATE` locking the ledger relies on |
| `psycopg[binary]` | 3.3.4 | PostgreSQL driver |
| `pydantic` | 2.13.5 | Request/response validation, every API boundary |
| `pydantic-settings` | 2.15.0 | Typed settings from the environment (`app/config.py`) |
| `email-validator` | 2.3.0 | Backs Pydantic's `EmailStr` |
| `redis` | 8.1.0 | Cache, rate limiter, and challenge/OTP store |
| `celery` | 5.6.3 | Scheduled orders, recurring buys, dividends, statements |
| `argon2-cffi` | 25.1.0 | Argon2id password hashing |
| `PyJWT` | 2.13.0 | Access, refresh, MFA and email-action tokens |
| `pyotp` | 2.10.0 | TOTP verification for authenticator apps |
| `cryptography` | 50.0.1 | Fernet sealing of TOTP secrets; TLS for SMTP |
| `webauthn` | 3.0.0 | Passkey registration and authentication ceremonies |
| `httpx` | 0.28.1 | Market-data provider HTTP client |
| `segno` | 1.6.6 | QR code for authenticator enrolment |
| `reportlab` | 5.0.1 | Monthly account statement PDFs |
| `openpyxl` | 3.1.5 | Excel exports of orders, transactions and dividends |

Development only (`requirements-dev.txt`): `pytest` 9.1.1 — the suite runs on SQLite
with the synthetic market-data provider, so it needs no services.

## Frontend — npm

| Package | Version | What it does here |
|---|---|---|
| `next` | 16.3.3 | App Router, server components, the `/api` proxy to the backend |
| `react` · `react-dom` | 19.2.8 | UI runtime |
| `recharts` | 3.10.1 | Portfolio, allocation and performance charts |

| Dev dependency | Version | Role |
|---|---|---|
| `typescript` | 7.0.2 | Native/Go compiler; `tsc --noEmit` is the typecheck gate |
| `tailwindcss` · `@tailwindcss/postcss` | 4.3.3 | Styling |
| `postcss` | 8.5.26 | CSS pipeline |
| `@types/node` | 24.13.3 | Node type definitions |
| `@types/react` · `@types/react-dom` | 19.2.18 · 19.2.5 | React type definitions |

There is **no** UI component library, CSS-in-JS runtime, state manager, data-fetching
library or icon package: the API client is ~600 lines of `fetch` in
[`frontend/lib/api.ts`](../frontend/lib/api.ts), and components are plain React.

