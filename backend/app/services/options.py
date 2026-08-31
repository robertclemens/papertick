"""Options: chain generation, pricing, and the full contract lifecycle.

Chains are generated locally and priced with Black-Scholes off the LIVE
underlying quote (per-ticker deterministic implied volatility with a smile and
term structure), so every quote is clearly model-derived — real underlying
price, modeled premium. Standard listings: weekly Fridays, monthly third
Fridays, and January LEAPS; strikes on exchange-style increments around spot.

Supported strategies (typical brokerage options Level 1-2):
  - long calls / long puts (buy to open, sell to close, exercise)
  - covered calls  (short call fully covered by 100 shares/contract)
  - cash-secured puts (collateral = strike x 100 reserved from buying power)

Expiration processing (worker, after the close): ITM long options auto-exercise
when funded (else cash-settle intrinsic), short options are assigned, OTM
options expire. IRS-style treatment: exercised call premium raises the stock
basis; exercised/assigned put premium adjusts proceeds/basis; short option
gains are always short-term.
"""

import logging
import math
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import for_update

from app.config import get_settings
from app.models import (
    Account,
    OptionAction,
    OptionPosition,
    OptionRight,
    OptionTransaction,
    Order,
    OrderSide,
    OrderSource,
    OrderType,
    Position,
    PositionSide,
    QuantityType,
    TaxLot,
    utcnow,
)
from app.schemas import ChainOut, ChainRowOut, OptionOrderIn, OptionQuoteOut
from app.services import market_calendar as cal
from app.services.market_data import MarketDataError, market_data, _u01
from app.services.trading import (
    LONG_TERM_DAYS,
    execute_fill,
    q_money,
    q_price,
    require_asset,
    reserved_cash,
)

log = logging.getLogger("papertick.options")

ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")


# ------------------------------------------------------------- listings

def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def _listed(d: date) -> date:
    """Options expiring on a holiday move to the prior trading day."""
    while not cal.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def list_expirations(today: date | None = None) -> list[date]:
    today = today or date.today()
    out: set[date] = set()
    d = today + timedelta(days=(4 - today.weekday()) % 7 or 7)  # next Friday
    for _ in range(6):  # six weeklies
        out.add(_listed(d))
        d += timedelta(days=7)
    y, m = today.year, today.month
    for _ in range(6):  # six monthlies
        tf = _third_friday(y, m)
        if tf > today:
            out.add(_listed(tf))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    for dy in (1, 2):  # January LEAPS
        out.add(_listed(_third_friday(today.year + dy, 1)))
    return sorted(x for x in out if x > today)


def strike_grid(spot: Decimal) -> list[Decimal]:
    s = float(spot)
    if s < 25:
        step = Decimal("0.5")
    elif s < 100:
        step = Decimal("1")
    elif s < 250:
        step = Decimal("5")
    else:
        step = Decimal("10")
    lo = Decimal(str(s * 0.75)).quantize(step, ROUND_HALF_UP)
    hi = Decimal(str(s * 1.25)).quantize(step, ROUND_HALF_UP)
    out = []
    k = lo
    while k <= hi:
        if k > 0:
            out.append(k.quantize(CENT))
        k += step
    return out


# ------------------------------------------------------------- pricing

def _sigma(ticker: str, strike: Decimal, spot: Decimal, t_years: float) -> float:
    base = 0.15 + _u01(ticker, "iv") * 0.35  # 15%..50% per ticker, deterministic
    moneyness = abs(math.log(float(strike) / float(spot))) if spot > 0 else 0.0
    smile = 1.0 + 0.6 * moneyness
    term = 1.0 + 0.03 / (t_years + 0.08)
    return min(base * smile * term, 4.0)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs(spot: float, strike: float, t: float, r: float, sigma: float, right: OptionRight):
    """Black-Scholes price, delta, theta(per day)."""
    t = max(t, 1.0 / 365 / 24)
    sq = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + sigma * sigma / 2) * t) / sq
    d2 = d1 - sq
    pdf_d1 = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
    if right == OptionRight.CALL:
        price = spot * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
        delta = _norm_cdf(d1)
        theta = (-spot * pdf_d1 * sigma / (2 * math.sqrt(t))
                 - r * strike * math.exp(-r * t) * _norm_cdf(d2)) / 365
    else:
        price = strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0
        theta = (-spot * pdf_d1 * sigma / (2 * math.sqrt(t))
                 + r * strike * math.exp(-r * t) * _norm_cdf(-d2)) / 365
    return max(price, 0.0), delta, theta


def _t_years(expiry: date, now: datetime | None = None) -> float:
    now = now or utcnow()
    expiry_dt = cal.market_close_at(expiry)
    return max((expiry_dt - now).total_seconds() / (365.0 * 86400), 1.0 / 365 / 24)


def option_quote(underlying: str, right: OptionRight, strike: Decimal,
                 expiry: date, spot: Decimal, now: datetime | None = None) -> OptionQuoteOut:
    t = _t_years(expiry, now)
    sigma = _sigma(underlying, strike, spot, t)
    price, delta, theta = _bs(float(spot), float(strike), t, get_settings().risk_free_rate, sigma, right)
    mid = Decimal(str(round(max(price, 0.01), 4)))
    spread = max(Decimal("0.01"), (mid * Decimal("0.03")).quantize(CENT, ROUND_HALF_UP))
    bid = max(ZERO, (mid - spread).quantize(CENT, ROUND_HALF_UP))
    ask = (mid + spread).quantize(CENT, ROUND_HALF_UP)
    itm = spot > strike if right == OptionRight.CALL else spot < strike
    return OptionQuoteOut(
        bid=bid, ask=ask, mid=mid.quantize(CENT, ROUND_HALF_UP),
        iv=round(sigma, 4), delta=round(delta, 4), theta=round(theta, 4), itm=itm,
    )


def chain(db: Session, underlying: str, expiry: date) -> ChainOut:
    require_asset(db, underlying)
    if expiry not in list_expirations():
        raise HTTPException(status_code=422, detail="Not a listed expiration; see /options/expirations")
    try:
        spot = market_data.quote(underlying).price
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    rows = [
        ChainRowOut(
            strike=k,
            call=option_quote(underlying, OptionRight.CALL, k, expiry, spot),
            put=option_quote(underlying, OptionRight.PUT, k, expiry, spot),
        )
        for k in strike_grid(spot)
    ]
    return ChainOut(
        underlying=underlying, spot=spot, expiry=expiry,
        days_to_expiry=(expiry - date.today()).days, rows=rows,
    )


# ------------------------------------------------------------- helpers

def _contract_label(o: OptionPosition | OptionOrderIn) -> str:
    return f"{o.underlying} {o.expiry} ${Decimal(o.strike):.2f} {o.right.value}"


def _shares_covering_short_calls(db: Session, account_id: str, underlying: str,
                                 exclude: OptionPosition | None = None) -> Decimal:
    rows = db.execute(
        select(OptionPosition).where(
            OptionPosition.account_id == account_id,
            OptionPosition.underlying == underlying,
            OptionPosition.right == OptionRight.CALL,
            OptionPosition.side == PositionSide.SHORT,
        )
    ).scalars().all()
    return sum((Decimal(p.contracts) * HUNDRED for p in rows if exclude is None or p.id != exclude.id), ZERO)


def _hours_gate(now: datetime) -> None:
    if get_settings().enforce_market_hours and not cal.is_market_open(now):
        nxt = cal.next_market_open(now)
        raise HTTPException(
            status_code=422,
            detail=(
                f"Options orders can only be placed while the market is open. "
                f"Next session: {nxt.isoformat()} (or set ENFORCE_MARKET_HOURS=false for sandbox mode)."
            ),
        )


def _record_txn(db: Session, account_id: str, *, underlying: str, right: OptionRight,
                strike: Decimal, expiry: date, action: OptionAction, contracts: int,
                premium: Decimal, cash_effect: Decimal, fees: Decimal,
                realized: Decimal | None = None, realized_st: Decimal | None = None,
                realized_lt: Decimal | None = None, underlying_price: Decimal | None = None,
                as_of: date | None = None) -> OptionTransaction:
    txn = OptionTransaction(
        account_id=account_id, underlying=underlying, right=right, strike=strike,
        expiry=expiry, action=action, contracts=contracts, premium=premium,
        cash_effect=cash_effect, fees=fees, realized_gains=realized,
        realized_st=realized_st, realized_lt=realized_lt,
        underlying_price=underlying_price, as_of=as_of or date.today(),
    )
    db.add(txn)
    return txn


def _split_term(realized: Decimal, opened_on: date, as_of: date, short_side: bool) -> tuple[Decimal, Decimal]:
    """Short option results are always short-term (IRS §1234)."""
    if short_side or (as_of - opened_on).days <= LONG_TERM_DAYS:
        return realized, ZERO
    return ZERO, realized


# ------------------------------------------------------------- open / close

def open_position(db: Session, account: Account, data: OptionOrderIn,
                  now: datetime | None = None) -> tuple[OptionPosition, OptionTransaction, str]:
    now = now or utcnow()
    _hours_gate(now)
    asset = require_asset(db, data.underlying)
    if asset.asset_class.value == "MUTUAL_FUND":
        raise HTTPException(status_code=422, detail="Options are not listed on mutual funds")
    if data.expiry not in list_expirations():
        raise HTTPException(status_code=422, detail="Not a listed expiration; see /options/expirations")
    try:
        spot = market_data.quote(data.underlying).price
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not (spot * Decimal("0.2") <= data.strike <= spot * Decimal("3")):
        raise HTTPException(status_code=422, detail=f"Strike out of the listed range for spot ${spot}")

    q = option_quote(data.underlying, data.right, data.strike, data.expiry, spot, now)
    fee = q_money(Decimal(get_settings().option_fee_per_contract) * data.contracts)
    c100 = Decimal(data.contracts) * HUNDRED

    locked = db.execute(
        for_update(select(Account).where(Account.id == account.id))
    ).scalar_one()
    side = PositionSide.LONG if data.action == "BUY_TO_OPEN" else PositionSide.SHORT
    today = now.date()

    if side == PositionSide.LONG:
        debit = q_money(q.ask * c100) + fee
        available = Decimal(locked.settlement_balance) - reserved_cash(db, locked.id)
        if available < debit:
            raise HTTPException(status_code=422, detail=f"Insufficient buying power: need ${debit}, available ${q_money(available)}")
        locked.settlement_balance = Decimal(locked.settlement_balance) - debit
        prem_basis = q_price((q.ask * c100 + fee) / c100)
        cash_effect = -debit
        action = OptionAction.BUY_TO_OPEN
        explanation = (
            f"Bought {data.contracts} {data.right.value.lower()} contract(s) on {data.underlying} "
            f"(each contract controls 100 shares). You now hold the RIGHT — not the obligation — to "
            f"{'BUY' if data.right == OptionRight.CALL else 'SELL'} {int(c100)} shares at ${data.strike} "
            f"any time until {data.expiry}. Premium paid ${debit} is your maximum loss. "
            f"Breakeven at expiry: ${q_money(data.strike + prem_basis) if data.right == OptionRight.CALL else q_money(data.strike - prem_basis)}."
        )
        collateral_add = ZERO
    else:
        credit_gross = q_money(q.bid * c100)
        credit = credit_gross - fee
        if credit <= 0:
            raise HTTPException(status_code=422, detail="Premium would not cover fees")
        if data.right == OptionRight.CALL:
            stock = db.execute(
                select(Position).where(Position.account_id == locked.id, Position.ticker == data.underlying)
            ).scalar_one_or_none()
            held = Decimal(stock.shares) if stock else ZERO
            covering = _shares_covering_short_calls(db, locked.id, data.underlying)
            if held - covering < c100:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Covered calls only: selling {data.contracts} contract(s) requires "
                        f"{int(c100)} uncommitted shares of {data.underlying}; you have "
                        f"{max(held - covering, ZERO)} available ({held} held, {covering} already covering calls)."
                    ),
                )
            collateral_add = ZERO
            explanation = (
                f"Sold {data.contracts} COVERED CALL contract(s) on {data.underlying}: you keep the "
                f"${credit} premium, but until {data.expiry} you are OBLIGATED to sell {int(c100)} of "
                f"your shares at ${data.strike} if the buyer exercises (likely when the stock is above "
                f"the strike). Upside above ${data.strike} is capped; the premium is yours either way."
            )
        else:
            collateral_add = q_money(data.strike * c100)
            available = Decimal(locked.settlement_balance) - reserved_cash(db, locked.id)
            if available < collateral_add:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Cash-secured puts only: ${collateral_add} must be reserved as collateral; "
                        f"available buying power is ${q_money(available)}."
                    ),
                )
            explanation = (
                f"Sold {data.contracts} CASH-SECURED PUT contract(s) on {data.underlying}: you keep the "
                f"${credit} premium, and until {data.expiry} you are OBLIGATED to buy {int(c100)} shares "
                f"at ${data.strike} if assigned (likely when the stock is below the strike). "
                f"${collateral_add} of cash is reserved as collateral. Effective purchase price if "
                f"assigned: ${q_money(data.strike - (credit / c100))} per share."
            )
        locked.settlement_balance = Decimal(locked.settlement_balance) + credit
        prem_basis = q_price(credit / c100)
        cash_effect = credit
        action = OptionAction.SELL_TO_OPEN

    position = db.execute(
        for_update(select(OptionPosition).where(
            OptionPosition.account_id == locked.id,
            OptionPosition.underlying == data.underlying,
            OptionPosition.right == data.right,
            OptionPosition.strike == data.strike,
            OptionPosition.expiry == data.expiry,
            OptionPosition.side == side,
        ))
    ).scalar_one_or_none()
    if position is None:
        position = OptionPosition(
            account_id=locked.id, underlying=data.underlying, right=data.right,
            strike=data.strike, expiry=data.expiry, side=side,
            contracts=data.contracts, avg_premium=prem_basis,
            collateral=collateral_add, opened_on=today,
        )
        db.add(position)
    else:
        old_c = Decimal(position.contracts)
        new_c = old_c + data.contracts
        position.avg_premium = q_price((old_c * Decimal(position.avg_premium) + Decimal(data.contracts) * prem_basis) / new_c)
        position.contracts = int(new_c)
        position.collateral = Decimal(position.collateral) + collateral_add

    txn = _record_txn(
        db, locked.id, underlying=data.underlying, right=data.right, strike=data.strike,
        expiry=data.expiry, action=action, contracts=data.contracts, premium=prem_basis,
        cash_effect=cash_effect, fees=fee, underlying_price=spot, as_of=today,
    )
    db.commit()
    return position, txn, explanation


def close_position(db: Session, position: OptionPosition, contracts: int,
                   now: datetime | None = None) -> tuple[OptionTransaction, str]:
    now = now or utcnow()
    _hours_gate(now)
    if contracts > position.contracts:
        raise HTTPException(status_code=422, detail=f"Position holds {position.contracts} contract(s)")
    try:
        spot = market_data.quote(position.underlying).price
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    q = option_quote(position.underlying, position.right, Decimal(position.strike),
                     position.expiry, spot, now)
    fee = q_money(Decimal(get_settings().option_fee_per_contract) * contracts)
    c100 = Decimal(contracts) * HUNDRED
    today = now.date()

    account = db.execute(
        for_update(select(Account).where(Account.id == position.account_id))
    ).scalar_one()

    if position.side == PositionSide.LONG:
        proceeds = q_money(q.bid * c100) - fee
        realized = q_money(proceeds - Decimal(position.avg_premium) * c100)
        account.settlement_balance = Decimal(account.settlement_balance) + proceeds
        cash_effect = proceeds
        action = OptionAction.SELL_TO_CLOSE
        st, lt = _split_term(realized, position.opened_on, today, short_side=False)
        explanation = (
            f"Sold to close {contracts} contract(s) of {_contract_label(position)} at ${q.bid}/share "
            f"(${proceeds} after fees). Realized gains: ${realized}."
        )
    else:
        cost = q_money(q.ask * c100) + fee
        realized = q_money(Decimal(position.avg_premium) * c100 - cost)
        account.settlement_balance = Decimal(account.settlement_balance) - cost
        cash_effect = -cost
        action = OptionAction.BUY_TO_CLOSE
        st, lt = realized, ZERO
        if position.right == OptionRight.PUT:
            release = q_money(Decimal(position.strike) * c100)
            position.collateral = max(ZERO, Decimal(position.collateral) - release)
        explanation = (
            f"Bought to close {contracts} contract(s) of {_contract_label(position)} at ${q.ask}/share, "
            f"ending the obligation. Realized gains: ${realized}."
        )

    txn = _record_txn(
        db, account.id, underlying=position.underlying, right=position.right,
        strike=Decimal(position.strike), expiry=position.expiry, action=action,
        contracts=contracts, premium=q.bid if position.side == PositionSide.LONG else q.ask,
        cash_effect=cash_effect, fees=fee, realized=realized, realized_st=st, realized_lt=lt,
        underlying_price=spot, as_of=today,
    )
    position.contracts -= contracts
    if position.contracts <= 0:
        db.delete(position)
    db.commit()
    return txn, explanation


# ------------------------------------------------------------- exercise / expiry

def _stock_leg(db: Session, account_id: str, ticker: str, side: OrderSide,
               shares: Decimal, price: Decimal, as_of: date) -> tuple[Order, object]:
    order = Order(
        account_id=account_id, ticker=ticker, side=side, order_type=OrderType.MARKET,
        quantity_type=QuantityType.SHARES, quantity=shares, source=OrderSource.API,
    )
    db.add(order)
    db.flush()
    txn = execute_fill(db, order, price, as_of)
    return order, txn


def _newest_lot(db: Session, account_id: str, ticker: str) -> TaxLot | None:
    return db.execute(
        select(TaxLot).where(TaxLot.account_id == account_id, TaxLot.ticker == ticker)
        .order_by(TaxLot.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def exercise(db: Session, position: OptionPosition, contracts: int,
             now: datetime | None = None) -> tuple[OptionTransaction, str]:
    now = now or utcnow()
    if position.side != PositionSide.LONG:
        raise HTTPException(status_code=422, detail="Only long options can be exercised")
    if contracts > position.contracts:
        raise HTTPException(status_code=422, detail=f"Position holds {position.contracts} contract(s)")
    if position.expiry < now.date():
        raise HTTPException(status_code=422, detail="Contract has expired")
    c100 = Decimal(contracts) * HUNDRED
    strike = Decimal(position.strike)
    premium = Decimal(position.avg_premium)
    today = now.date()

    account = db.execute(
        for_update(select(Account).where(Account.id == position.account_id))
    ).scalar_one()

    if position.right == OptionRight.CALL:
        need = q_money(strike * c100)
        available = Decimal(account.settlement_balance) - reserved_cash(db, account.id)
        if available < need:
            raise HTTPException(status_code=422, detail=f"Exercising needs ${need} cash to buy the shares; available ${q_money(available)}")
        order, stock_txn = _stock_leg(db, account.id, position.underlying, OrderSide.BUY, c100, strike, today)
        if stock_txn is None:
            raise HTTPException(status_code=422, detail=f"Exercise failed: {order.reject_reason}")
        lot = _newest_lot(db, account.id, position.underlying)
        if lot is not None:  # premium paid becomes part of the stock's cost basis (IRS)
            lot.cost_per_share = q_price(Decimal(lot.cost_per_share) + premium)
        explanation = (
            f"Exercised {contracts} call contract(s): bought {int(c100)} shares of "
            f"{position.underlying} at the ${strike} strike. The ${q_money(premium * c100)} premium "
            f"paid was added to the shares' cost basis (${q_money(strike + premium)}/share)."
        )
    else:
        stock = db.execute(
            select(Position).where(Position.account_id == account.id, Position.ticker == position.underlying)
        ).scalar_one_or_none()
        if stock is None or Decimal(stock.shares) < c100:
            raise HTTPException(status_code=422, detail=f"Exercising this put requires {int(c100)} shares of {position.underlying} to sell")
        order, stock_txn = _stock_leg(db, account.id, position.underlying, OrderSide.SELL, c100, strike, today)
        if stock_txn is None:
            raise HTTPException(status_code=422, detail=f"Exercise failed: {order.reject_reason}")
        _reduce_stock_proceeds(stock_txn, q_money(premium * c100))
        explanation = (
            f"Exercised {contracts} put contract(s): sold {int(c100)} shares of {position.underlying} "
            f"at the ${strike} strike. The ${q_money(premium * c100)} premium paid reduced the sale "
            f"proceeds (IRS treatment)."
        )

    txn = _record_txn(
        db, account.id, underlying=position.underlying, right=position.right,
        strike=strike, expiry=position.expiry, action=OptionAction.EXERCISE,
        contracts=contracts, premium=premium, cash_effect=ZERO, fees=ZERO, as_of=today,
    )
    position.contracts -= contracts
    if position.contracts <= 0:
        db.delete(position)
    db.commit()
    return txn, explanation


def _reduce_stock_proceeds(stock_txn, amount: Decimal) -> None:
    """Fold an option premium into a stock sale's realized gains, proportional to
    its existing short/long-term split."""
    total = Decimal(stock_txn.realized_gains or 0)
    st = Decimal(stock_txn.realized_st or 0)
    frac_st = (st / total) if total not in (ZERO,) else Decimal(1)
    d_st = q_money(amount * frac_st)
    stock_txn.realized_gains = q_money(total - amount)
    stock_txn.realized_st = q_money(st - d_st)
    stock_txn.realized_lt = q_money(Decimal(stock_txn.realized_lt or 0) - (amount - d_st))


def _boost_stock_proceeds(stock_txn, amount: Decimal) -> None:
    _reduce_stock_proceeds(stock_txn, -amount)


def process_expirations(db: Session, now: datetime | None = None) -> int:
    """Settle every position whose contract has expired (runs after the close)."""
    now = now or utcnow()
    today = now.date()
    from app.services.scenarios import frozen_accounts

    positions = db.execute(
        for_update(
            select(OptionPosition)
            .where(OptionPosition.expiry <= today,
                   # a deleted scenario is frozen until it is restored or purged
                   OptionPosition.account_id.notin_(frozen_accounts(db))),
            skip_locked=True,
        )
    ).scalars().all()
    processed = 0
    for pos in positions:
        if pos.expiry == today and now < cal.market_close_at(today) + timedelta(minutes=30):
            continue  # wait for the closing print
        try:
            spot = market_data.close_on(pos.underlying, pos.expiry)
        except MarketDataError:
            spot = None
        if spot is None:
            continue  # retry next tick
        _settle_expired(db, pos, spot)
        processed += 1
    db.commit()
    return processed


def _settle_expired(db: Session, pos: OptionPosition, spot: Decimal) -> None:
    strike = Decimal(pos.strike)
    premium = Decimal(pos.avg_premium)
    c100 = Decimal(pos.contracts) * HUNDRED
    contracts = pos.contracts
    expiry = pos.expiry
    itm = spot > strike if pos.right == OptionRight.CALL else spot < strike
    intrinsic = (spot - strike) if pos.right == OptionRight.CALL else (strike - spot)

    account = db.execute(
        for_update(select(Account).where(Account.id == pos.account_id))
    ).scalar_one()

    def txn(action, *, cash=ZERO, realized=None, st=None, lt=None):
        _record_txn(
            db, account.id, underlying=pos.underlying, right=pos.right, strike=strike,
            expiry=expiry, action=action, contracts=contracts, premium=premium,
            cash_effect=cash, fees=ZERO, realized=realized, realized_st=st, realized_lt=lt,
            underlying_price=spot, as_of=expiry,
        )

    if pos.side == PositionSide.LONG:
        if not itm:
            realized = q_money(-premium * c100)
            st, lt = _split_term(realized, pos.opened_on, expiry, short_side=False)
            txn(OptionAction.EXPIRATION, realized=realized, st=st, lt=lt)
        elif pos.right == OptionRight.CALL:
            need = q_money(strike * c100)
            if Decimal(account.settlement_balance) - reserved_cash(db, account.id) >= need:
                order, stock_txn = _stock_leg(db, account.id, pos.underlying, OrderSide.BUY, c100, strike, expiry)
                if stock_txn is not None:
                    lot = _newest_lot(db, account.id, pos.underlying)
                    if lot is not None:
                        lot.cost_per_share = q_price(Decimal(lot.cost_per_share) + premium)
                    txn(OptionAction.EXERCISE)
                else:
                    _cash_settle(db, account, pos, spot, intrinsic, txn)
            else:
                _cash_settle(db, account, pos, spot, intrinsic, txn)
        else:  # long put ITM
            stock = db.execute(
                select(Position).where(Position.account_id == account.id, Position.ticker == pos.underlying)
            ).scalar_one_or_none()
            if stock is not None and Decimal(stock.shares) >= c100:
                order, stock_txn = _stock_leg(db, account.id, pos.underlying, OrderSide.SELL, c100, strike, expiry)
                if stock_txn is not None:
                    _reduce_stock_proceeds(stock_txn, q_money(premium * c100))
                    txn(OptionAction.EXERCISE)
                else:
                    _cash_settle(db, account, pos, spot, intrinsic, txn)
            else:
                _cash_settle(db, account, pos, spot, intrinsic, txn)
    else:  # SHORT
        if pos.right == OptionRight.PUT:
            pos.collateral = ZERO  # collateral released either way
        if not itm:
            realized = q_money(premium * c100)
            txn(OptionAction.EXPIRATION, realized=realized, st=realized, lt=ZERO)
        elif pos.right == OptionRight.CALL:
            order, stock_txn = _stock_leg(db, account.id, pos.underlying, OrderSide.SELL, c100, strike, expiry)
            if stock_txn is not None:
                _boost_stock_proceeds(stock_txn, q_money(premium * c100))
                txn(OptionAction.ASSIGNMENT)
            else:  # covering shares gone: settle the intrinsic loss in cash
                loss = q_money((premium - intrinsic) * c100)
                account.settlement_balance = Decimal(account.settlement_balance) - q_money(intrinsic * c100)
                txn(OptionAction.CASH_SETTLEMENT, cash=-q_money(intrinsic * c100),
                    realized=loss, st=loss, lt=ZERO)
        else:  # short put ITM: assignment — buy shares at the strike
            order, stock_txn = _stock_leg(db, account.id, pos.underlying, OrderSide.BUY, c100, strike, expiry)
            if stock_txn is not None:
                lot = _newest_lot(db, account.id, pos.underlying)
                if lot is not None:  # premium received lowers the new shares' basis (IRS)
                    lot.cost_per_share = q_price(max(Decimal("0.000001"), Decimal(lot.cost_per_share) - premium))
                txn(OptionAction.ASSIGNMENT)
            else:
                loss = q_money((premium - intrinsic) * c100)
                account.settlement_balance = Decimal(account.settlement_balance) - q_money(intrinsic * c100)
                txn(OptionAction.CASH_SETTLEMENT, cash=-q_money(intrinsic * c100),
                    realized=loss, st=loss, lt=ZERO)

    db.delete(pos)


def _cash_settle(db: Session, account: Account, pos: OptionPosition, spot: Decimal,
                 intrinsic: Decimal, txn) -> None:
    c100 = Decimal(pos.contracts) * HUNDRED
    credit = q_money(intrinsic * c100)
    realized = q_money(credit - Decimal(pos.avg_premium) * c100)
    account.settlement_balance = Decimal(account.settlement_balance) + credit
    st, lt = _split_term(realized, pos.opened_on, pos.expiry, short_side=False)
    txn(OptionAction.CASH_SETTLEMENT, cash=credit, realized=realized, st=st, lt=lt)


# ------------------------------------------------------------- valuation

def positions_value(db: Session, account_ids: list[str]) -> Decimal:
    """Signed mark value of open option positions (shorts are liabilities)."""
    if not account_ids:
        return ZERO
    rows = db.execute(
        select(OptionPosition).where(OptionPosition.account_id.in_(account_ids))
    ).scalars().all()
    total = ZERO
    for pos in rows:
        try:
            spot = market_data.quote(pos.underlying).price
        except MarketDataError:
            continue
        q = option_quote(pos.underlying, pos.right, Decimal(pos.strike), pos.expiry, spot)
        value = q.mid * Decimal(pos.contracts) * HUNDRED
        total += value if pos.side == PositionSide.LONG else -value
    return q_money(total)
