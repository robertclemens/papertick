from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_read
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
    asset = require_asset(db, ticker.upper())
    db.commit()
    return asset


@router.get("/status", response_model=MarketStatusOut)
def market_status() -> MarketStatusOut:
    """Current NYSE session state — open or closed right now, the next open and
    close times, and whether this deployment enforces market hours and allows
    backdated trades. Check this before assuming a market order will fill
    immediately rather than queue for the next open."""
    now = utcnow()
    return MarketStatusOut(
        is_open=cal.is_market_open(now),
        is_trading_day=cal.is_trading_day(now.astimezone(cal.ET).date()),
        next_open=cal.next_market_open(now),
        next_close=cal.next_market_close(now),
        enforce_market_hours=get_settings().enforce_market_hours,
        allow_backdated_trades=get_settings().allow_backdated_trades,
        server_time=now,
    )


@router.get("/search", dependencies=[Depends(rate_limiter("md-search", 60, 60))])
def search_symbols(
    q: str = Query(min_length=1, max_length=60),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Search by ticker OR company/fund name. Merges the local asset table with
    the live provider's symbol search, local matches first."""
    like_t = f"%{q.upper()}%"
    local = db.execute(
        select(Asset)
        .where(
            or_(Asset.ticker.like(like_t), Asset.name.ilike(f"%{q}%")),
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
        like = f"%{query.upper()}%"
        q = q.where(or_(Asset.ticker.like(like), Asset.name.ilike(f"%{query}%")))
    return [AssetOut.model_validate(a) for a in db.execute(q.limit(50)).scalars()]


@router.get("/quote/{ticker}", response_model=QuoteOut,
            dependencies=[Depends(rate_limiter("quote", 120, 60))])
def get_quote(ticker: str, db: Session = Depends(get_db)) -> QuoteOut:
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
    ticker: str,
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
