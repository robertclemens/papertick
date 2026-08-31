import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import docs_ui
from app.config import get_settings
from app.db import get_engine
from app.rate_limit import get_redis
from app.routers import (
    accounts,
    api_keys,
    auth,
    exports,
    market,
    options,
    orders,
    passkeys_router,
    portfolio,
    scenarios,
    schedules,
    statements,
    tax,
)
from app.services.market_data import MarketDataError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

settings = get_settings()

# Outside production the docs and spec stay open: they are how the API is
# explored. In production they sit behind the same auth as everything else.
_EXPOSE_DOCS_ANON = not settings.is_production

app = FastAPI(
    title="PaperTick API",
    version="1.0.0",
    description=(
        "Paper-trading, backtesting and wealth-management simulation platform.\n\n"
        "**Authentication** — a session (cookie / Bearer JWT) or an API key "
        "(`Authorization: Bearer ptk_...`) carrying `read` and/or `trade` scopes.\n\n"
        "**Scenarios** — every account, holding, order and statement belongs to a "
        "scenario: an independent track of the same portfolio. Send "
        "`X-Scenario-Id: <id>` (or `?scenario_id=<id>`) to pick one; omit it and "
        "your default scenario is used. `GET /api/v1/scenarios` lists them, and "
        "every response carries `X-Scenario-Id` naming the scenario that answered, "
        "so a call is never ambiguous about which track it hit. Naming a scenario "
        "you do not own returns 404 rather than falling back."
    ),
    # the stock docs pages are replaced by themed ones (app/docs_ui.py)
    docs_url=None,
    redoc_url=None,
    # The spec is the full map of the attack surface — every route, bound and
    # business rule. Agentic callers need it, anonymous ones do not, so in
    # production it is served only to an authenticated caller (see docs_ui).
    openapi_url="/api/openapi.json" if _EXPOSE_DOCS_ANON else None,
)

docs_ui.install(app, settings.frontend_origin, public=_EXPOSE_DOCS_ANON)

# A request whose Host we do not recognise is rejected outright, so a spoofed
# Host can never reach routing, a cache, or a generated URL.
if settings.allowed_hosts.strip():
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[h.strip() for h in settings.allowed_hosts.split(",") if h.strip()],
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Scenario-Id"],
    expose_headers=["X-Scenario-Id", "X-Scenario-Name"],
)


# JSON API responses render nothing, so the policy can be maximally strict.
# The docs pages set their own, looser policy (see app/docs_ui.py).
API_CSP = (
    "default-src \'none\'; frame-ancestors \'none\'; base-uri \'none\'; "
    "form-action \'none\'; sandbox"
)
PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
    "interest-cohort=()"
)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MAX_BODY_BYTES = 8 * 1024 * 1024


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """Refuse an oversized body before it is buffered and parsed.

    Without this, an unauthenticated caller can make the server allocate and
    deserialise an arbitrarily large document — and the scenario-import route
    then instantiates a row object per element.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return JSONResponse(status_code=413,
                                    content={"detail": "Request body too large"})
        except ValueError:
            return JSONResponse(status_code=400,
                                content={"detail": "Malformed Content-Length"})
    return await call_next(request)


@app.middleware("http")
async def csrf_origin_guard(request: Request, call_next):
    """Reject cross-origin state-changing requests that ride on cookies.

    SameSite=Lax already blocks the cross-site POST, but it is site-scoped
    rather than origin-scoped and it is a browser-side control we do not get to
    verify. This is the server-side half. API-key callers are exempt: they
    carry an explicit credential rather than ambient cookie authority, which is
    what CSRF abuses.
    """
    if request.method in UNSAFE_METHODS:
        auth = request.headers.get("authorization", "")
        is_bearer = auth.lower().startswith("bearer ")
        origin = request.headers.get("origin")
        if not is_bearer and origin and origin.rstrip("/") != settings.frontend_origin.rstrip("/"):
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-origin request rejected"},
            )
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # Tell the caller which track answered. Agents and CLIs pick a scenario by
    # header and would otherwise have no confirmation of which one they hit —
    # and "which portfolio did I just trade in" is not a question to guess at.
    scenario = getattr(request.state, "scenario", None)
    if scenario is not None:
        response.headers["X-Scenario-Id"] = scenario.id
        response.headers["X-Scenario-Name"] = scenario.name
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = PERMISSIONS_POLICY
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers.setdefault("Content-Security-Policy", API_CSP)
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains; preload"
        )
    return response


@app.exception_handler(MarketDataError)
async def market_data_error_handler(request: Request, exc: MarketDataError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


API = "/api/v1"
app.include_router(auth.router, prefix=API)
app.include_router(passkeys_router.router, prefix=API)
app.include_router(api_keys.router, prefix=API)
app.include_router(accounts.router, prefix=API)
app.include_router(market.router, prefix=API)
app.include_router(orders.router, prefix=API)
app.include_router(schedules.router, prefix=API)
app.include_router(portfolio.router, prefix=API)
app.include_router(options.router, prefix=API)
app.include_router(scenarios.router, prefix=API)
app.include_router(statements.router, prefix=API)
app.include_router(exports.router, prefix=API)
app.include_router(tax.router, prefix=API)


@app.get("/healthz", tags=["health"])
def healthz() -> dict:
    checks = {"database": "ok", "redis": "ok"}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        checks["database"] = "down"
    try:
        get_redis().ping()
    except Exception:
        checks["redis"] = "down"
    status = 200 if all(v == "ok" for v in checks.values()) else 503
    return JSONResponse(status_code=status, content={"status": "ok" if status == 200 else "degraded", **checks})
