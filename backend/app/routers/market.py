from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import Principal, require_read
from app.models import Asset, utcnow
from app.rate_limit import rate_limiter
from app.schemas import AssetOut, CandleOut, HistoryOut, MarketStatusOut, QuoteOut
from app.services import market_calendar as cal
from app.services import settlement
from app.services.market_data import EPOCH, MarketDataError, market_data
from app.services.trading import require_asset

router = APIRouter(prefix="/market", tags=["market"],
                   dependencies=[Depends(require_read)])


def _require_asset(db: Session, ticker: str) -> Asset:
    """Known asset, or auto-registration of any validated US-listed symbol."""
    asset = require_asset(db, ticker)
    db.commit()
    return asset


def _like(term: str) -> str:
    """Escape a user substring for LIKE/ILIKE.

    Unescaped, `%` and `_` are wildcards the caller gets to inject: a query of
    a single `%` matches the whole table on every request.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/status", response_model=MarketStatusOut)
def market_status(principal: Principal = Depends(require_read),
                  db: Session = Depends(get_db)) -> MarketStatusOut:
    """Current NYSE session state — open or closed right now, the next open and
    close times, and whether this deployment enforces market hours and allows
    backdated trades. Check this before assuming a market order will fill
    immediately rather than queue for the next open.

    `allow_backdated_trades` answers "can I place an as-of order right now",
    which is a property of the caller's active scenario, not of the deployment.

    Also carries `refresh_seconds` / `refresh_reason`: how often a client
    showing prices should re-price, decided here because this is where the
    trading calendar lives. It is 0 while the market is shut, because nothing a
    refresh could discover has changed."""
    now = utcnow()
    settings = get_settings()
    cadence, reason = cal.refresh_cadence(
        now, settings.market_refresh_seconds, settings.enforce_market_hours
    )
    return MarketStatusOut(
        is_open=cal.is_market_open(now),
        is_trading_day=cal.is_trading_day(now.astimezone(cal.ET).date()),
        next_open=cal.next_market_open(now),
        next_close=cal.next_market_close(now),
        enforce_market_hours=get_settings().enforce_market_hours,
        allow_backdated_trades=bool(principal.scenario and principal.scenario.allow_backdated),
        server_time=now,
        refresh_seconds=cadence,
        refresh_reason=reason,
        quote_cache_seconds=get_settings().quote_cache_seconds,
    )


@router.get("/providers")
def provider_health(principal: Principal = Depends(require_read)) -> dict:
    """Which market-data providers are active, and whether each has been
    verified to publish prices on the convention this engine requires
    (split-adjusted, not dividend-adjusted).

    `status` is `ok` (measured and correct), `wrong` (measured and on another
    convention — quarantined, its prices are not used), `unknown` (the last
    check could not complete; still used), or `unverified` (not checked yet).
    """
    from app.services import convention
    from app.services.market_data import market_data

    out = []
    for provider in market_data._chain():
        verdict = convention.stored(provider.name)
        out.append({
            "provider": provider.name,
            "status": verdict.status if verdict else "unverified",
            "detail": verdict.detail if verdict else "",
            "checked_at": verdict.checked_at if verdict else None,
        })
    return {
        "required_convention": convention.REQUIRED,
        "quarantine_enabled": get_settings().convention_quarantine,
        "providers": out,
    }


@router.get("/verify/{ticker}", dependencies=[Depends(rate_limiter("md-verify", 20, 300))])
def verify_price(ticker: str = Path(min_length=1, max_length=12),
                 on: date | None = None,
                 principal: Principal = Depends(require_read)) -> dict:
    """Check one of our closing prices against an independent source.

    A diagnostic, not a routine check: it reaches a second vendor (Nasdaq's
    free endpoint) that shares no code, no convention and no vendor with our
    chain. Reach for it when a price looks wrong.

    Limits worth knowing: the reference only carries about seven years of
    history, and it publishes raw prices, so a symbol that has split since the
    date being checked will disagree by exactly the split ratio. That is the
    reference being unadjusted, not our price being wrong — the response says
    so when it sees a whole-number ratio.
    """
    from app.services.oracle import compare_close

    result = compare_close(ticker, on or (utcnow().date() - timedelta(days=1)))
    return {
        "ticker": result.ticker,
        "date": result.on,
        "our_close": result.ours,
        "reference_close": result.theirs,
        "difference_pct": result.difference_pct,
        "verdict": result.verdict,
        "note": result.note,
        "reference": "nasdaq (free, unadjusted for splits)",
    }


@router.get("/search", dependencies=[Depends(rate_limiter("md-search", 60, 60))])
def search_symbols(
    q: str = Query(min_length=1, max_length=60),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Search by ticker OR company/fund name. Merges the local asset table with
    the live provider's symbol search, local matches first."""
    term = _like(q)
    like_t = f"%{term.upper()}%"
    local = db.execute(
        select(Asset)
        .where(
            or_(Asset.ticker.like(like_t, escape="\\"),
                Asset.name.ilike(f"%{term}%", escape="\\")),
            Asset.ticker != settlement.TICKER,  # the settlement fund is not tradable
        )
        .order_by(Asset.ticker)
        .limit(8)
    ).scalars().all()
    out = [
        {"ticker": a.ticker, "name": a.name, "type": a.asset_class.value, "registered": True}
        for a in local
    ]
    seen = {a.ticker for a in local}
    for row in market_data.search_symbols(q):
        if row["ticker"] in seen:
            continue
        out.append({
            "ticker": row["ticker"],
            "name": row["name"],
            "type": row["type"],
            "registered": False,
            "exchange": row.get("exchange", ""),
        })
    return out[:12]


@router.get("/assets", response_model=list[AssetOut])
def list_assets(
    query: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
):
    """Tradable universe, optionally filtered by ticker or name substring,
    sorted by ticker and capped at 50 rows. The settlement fund is excluded —
    it is held automatically and is never bought or sold directly."""
    # the settlement fund is held, never bought or sold, so it is not offered
    q = select(Asset).where(Asset.ticker != settlement.TICKER).order_by(Asset.ticker)
    if query:
        term = _like(query)
        q = q.where(or_(Asset.ticker.like(f"%{term.upper()}%", escape="\\"),
                        Asset.name.ilike(f"%{term}%", escape="\\")))
    return [AssetOut.model_validate(a) for a in db.execute(q.limit(50)).scalars()]


@router.get("/quote/{ticker}", response_model=QuoteOut,
            dependencies=[Depends(rate_limiter("quote", 120, 60))])
def get_quote(ticker: str = Path(min_length=1, max_length=12),
              db: Session = Depends(get_db)) -> QuoteOut:
    """Latest price for a ticker, auto-registering it first if it is not
    already known (any validated US-listed symbol qualifies). Includes the
    previous close, the percent change computed from it, and which upstream
    provider served the quote."""
    asset = _require_asset(db, ticker)
    try:
        q = market_data.quote(asset.ticker)
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    change = None
    if q.prev_close:
        change = float((q.price - q.prev_close) / q.prev_close * 100)
    return QuoteOut(
        ticker=asset.ticker, price=q.price, prev_close=q.prev_close,
        change_pct=change, as_of=q.as_of, provider=q.provider,
    )


@router.get("/history/{ticker}", response_model=HistoryOut,
            dependencies=[Depends(rate_limiter("history", 60, 60))])
def get_history(
    ticker: str = Path(min_length=1, max_length=12),
    start: date | None = None,
    end: date | None = None,
    db: Session = Depends(get_db),
) -> HistoryOut:
    """Daily split-adjusted closing prices for a ticker over [start, end],
    auto-registering an unknown ticker first. Defaults to the trailing year
    ending today; the range cannot extend past today or before the platform's
    data floor."""
    asset = _require_asset(db, ticker)
    today = date.today()
    end = min(end or today, today)
    start = start or end - timedelta(days=365)
    if start < EPOCH:
        start = EPOCH
    if start >= end:
        raise HTTPException(status_code=422, detail="start must be before end")
    try:
        candles, provider = market_data.history(asset.ticker, start, end)
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return HistoryOut(
        ticker=asset.ticker,
        provider=provider,
        candles=[CandleOut(date=d, close=p) for d, p in candles],
    )
