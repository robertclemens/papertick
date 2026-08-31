from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Principal, owned_account, require_read, require_trade
from app.models import Account, OptionPosition, OptionTransaction, PositionSide
from app.rate_limit import rate_limiter
from app.schemas import (
    ChainOut,
    OptionCloseIn,
    OptionOrderIn,
    OptionOrderResultOut,
    OptionPositionOut,
    OptionPositionViewOut,
    OptionTransactionOut,
)
from app.services import options as opt
from app.services.market_data import MarketDataError, market_data

router = APIRouter(prefix="/options", tags=["options"])

EDUCATION = {
    "what_is_an_option": (
        "An option is a contract on 100 shares of a stock or ETF. A CALL is the right to BUY "
        "those shares at a fixed 'strike' price until the expiration date; a PUT is the right "
        "to SELL them. Buyers pay a premium for that right and can never lose more than the "
        "premium. Sellers collect the premium but take on the matching OBLIGATION."
    ),
    "supported_strategies": (
        "PaperTick supports the strategies of a typical brokerage options Level 1-2 approval: "
        "buying calls and puts, covered calls (selling a call against 100 owned shares per "
        "contract), and cash-secured puts (selling a put with the full strike x 100 reserved "
        "in cash). Naked/margined short options are not offered."
    ),
    "pricing_note": (
        "Option premiums here are model-derived (Black-Scholes on the live underlying price "
        "with a per-symbol volatility model), not exchange quotes."
    ),
}


@router.get("/education")
def education() -> dict:
    """Static primer on what an option contract is, which strategies
    PaperTick supports (long calls/puts, covered calls, cash-secured puts),
    and a reminder that premiums are Black-Scholes model prices on the live
    underlying, not real exchange quotes."""
    return EDUCATION


@router.get("/expirations/{underlying}")
def expirations(underlying: str = Path(min_length=1, max_length=12),
                principal: Principal = Depends(require_read),
                db: Session = Depends(get_db)) -> dict:
    """Listed expiration dates for an underlying: six weekly Fridays, six
    monthly third-Fridays, and the next two January LEAPS, each rolled back to
    the prior trading day if it falls on a holiday. Auto-registers the
    underlying first if it is not yet a known asset."""
    from app.services.trading import require_asset

    require_asset(db, underlying.upper())
    db.commit()
    return {"underlying": underlying.upper(), "expirations": opt.list_expirations()}


@router.get("/chain/{underlying}", response_model=ChainOut,
            dependencies=[Depends(rate_limiter("chain", 60, 60))])
def get_chain(expiry: date, underlying: str = Path(min_length=1, max_length=12),
              principal: Principal = Depends(require_read),
              db: Session = Depends(get_db)) -> ChainOut:
    """Full option chain for an underlying at one expiration: bid/ask/mid,
    implied volatility, delta, theta, and ITM/OTM status for both the call and
    put at each listed strike, priced off the live underlying quote."""
    result = opt.chain(db, underlying.upper(), expiry)
    db.commit()
    return result


@router.post("/orders", response_model=OptionOrderResultOut, status_code=201,
             dependencies=[Depends(rate_limiter("option-orders", 60, 60))])
def place_option_order(data: OptionOrderIn, principal: Principal = Depends(require_trade),
                       db: Session = Depends(get_db)) -> OptionOrderResultOut:
    """Open or add to an option position: buy a call/put, sell a covered call
    against owned shares, or sell a cash-secured put with the strike x 100
    reserved from buying power. Rejected outright with a 422 if the market is
    closed rather than queued, and the required collateral or covering shares
    are checked before the trade is accepted."""
    account = owned_account(data.account_id, principal, db)
    position, txn, explanation = opt.open_position(db, account, data)
    return OptionOrderResultOut(
        position=OptionPositionOut.model_validate(position),
        transaction=OptionTransactionOut.model_validate(txn),
        explanation=explanation,
    )


def _owned_position(position_id: str, principal: Principal, db: Session) -> OptionPosition:
    pos = db.get(OptionPosition, position_id)
    if pos is None:
        raise HTTPException(status_code=404, detail="Option position not found")
    owned_account(pos.account_id, principal, db)
    return pos


@router.post("/positions/{position_id}/close", response_model=OptionOrderResultOut)
def close_option_position(position_id: str, data: OptionCloseIn,
                          principal: Principal = Depends(require_trade),
                          db: Session = Depends(get_db)) -> OptionOrderResultOut:
    """Close some or all contracts of an existing position at the current
    model price — sell to close a long, buy to close a short — realizing the
    gain or loss and, for a short put, releasing its reserved collateral.
    Rejected outright with a 422 if the market is closed."""
    pos = _owned_position(position_id, principal, db)
    txn, explanation = opt.close_position(db, pos, data.contracts)
    remaining = db.get(OptionPosition, position_id)
    return OptionOrderResultOut(
        position=OptionPositionOut.model_validate(remaining) if remaining else None,
        transaction=OptionTransactionOut.model_validate(txn),
        explanation=explanation,
    )


@router.post("/positions/{position_id}/exercise", response_model=OptionOrderResultOut)
def exercise_option_position(position_id: str, data: OptionCloseIn,
                             principal: Principal = Depends(require_trade),
                             db: Session = Depends(get_db)) -> OptionOrderResultOut:
    """Exercise a long option before it expires: a call buys the underlying
    shares at the strike (the cash must be available) and folds the premium
    paid into their cost basis, while a put sells owned shares at the strike
    and the premium reduces the sale proceeds. Irreversible, and fails if the
    contract has already expired or the needed cash or shares are unavailable."""
    pos = _owned_position(position_id, principal, db)
    txn, explanation = opt.exercise(db, pos, data.contracts)
    remaining = db.get(OptionPosition, position_id)
    return OptionOrderResultOut(
        position=OptionPositionOut.model_validate(remaining) if remaining else None,
        transaction=OptionTransactionOut.model_validate(txn),
        explanation=explanation,
    )


@router.get("/positions", response_model=list[OptionPositionViewOut])
def list_option_positions(account_id: str | None = None,
                          principal: Principal = Depends(require_read),
                          db: Session = Depends(get_db)):
    """Open option positions across the caller's accounts, each marked to the
    current model price with live unrealized gains, days to expiry, and
    ITM/OTM status. A position is silently skipped if its underlying's quote
    is unavailable."""
    q = (
        select(OptionPosition)
        .join(Account, Account.id == OptionPosition.account_id)
        .where(Account.user_id == principal.user.id,
               Account.scenario_id == principal.scenario_id)
        .order_by(OptionPosition.expiry)
    )
    if account_id:
        q = q.where(OptionPosition.account_id == account_id)
    out: list[OptionPositionViewOut] = []
    for pos in db.execute(q).scalars():
        try:
            spot = market_data.quote(pos.underlying).price
        except MarketDataError:
            continue
        quote = opt.option_quote(pos.underlying, pos.right, Decimal(pos.strike), pos.expiry, spot)
        c100 = Decimal(pos.contracts) * Decimal(100)
        value = quote.mid * c100
        sign = 1 if pos.side == PositionSide.LONG else -1
        unreal = (quote.mid - Decimal(pos.avg_premium)) * c100 * sign
        out.append(OptionPositionViewOut(
            position=OptionPositionOut.model_validate(pos),
            mark=quote.mid,
            market_value=(value * sign).quantize(Decimal("0.01")),
            unrealized_gains=unreal.quantize(Decimal("0.01")),
            underlying_price=spot,
            days_to_expiry=(pos.expiry - date.today()).days,
            itm=quote.itm,
        ))
    return out


@router.get("/transactions", response_model=list[OptionTransactionOut])
def list_option_transactions(account_id: str | None = None,
                             limit: int = Query(default=100, ge=1, le=500),
                             principal: Principal = Depends(require_read),
                             db: Session = Depends(get_db)):
    """History of option fills, closes, exercises, assignments, and
    expirations for the caller's accounts, most recent first, optionally
    filtered to one account."""
    q = (
        select(OptionTransaction)
        .join(Account, Account.id == OptionTransaction.account_id)
        .where(Account.user_id == principal.user.id,
               Account.scenario_id == principal.scenario_id)
        .order_by(OptionTransaction.executed_at.desc())
        .limit(limit)
    )
    if account_id:
        q = q.where(OptionTransaction.account_id == account_id)
    return [OptionTransactionOut.model_validate(t) for t in db.execute(q).scalars()]
