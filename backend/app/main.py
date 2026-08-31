import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
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
    openapi_url="/api/openapi.json",
)

settings = get_settings()
docs_ui.install(app, settings.frontend_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Scenario-Id"],
    expose_headers=["X-Scenario-Id", "X-Scenario-Name"],
)


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
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
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
