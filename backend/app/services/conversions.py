"""Roth conversions: moving money from a Traditional or Rollover IRA into a Roth.

Only that direction exists, and the refusals matter as much as the transfer:

  * **Roth → Traditional** is impossible. Recharacterising a *conversion* was
    eliminated by the Tax Cuts and Jobs Act for 2018 onwards. (Recharacterising
    a current-year *contribution* is still legal, and is a different feature.)
  * **Taxable → IRA** is not a conversion but a contribution — cash only, and
    against the annual limit.
  * **IRA → Taxable** is a distribution, not a conversion: taxable, and
    penalised before 59½.

A conversion has **no dollar limit and no income cap**, which is the single most
important rule here: modelling it as a contribution would have it consume IRA
room it does not consume. It is taxed as ordinary income to the extent it is
pre-tax, split by the Form 8606 pro-rata rule in `services.ira`.
"""

from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountType,
    CashFlowKind,
    Contribution,
    Conversion,
    Position,
    TaxLot,
    User,
    utcnow,
)
from app.services import ira
from app.services.market_data import MarketDataError, market_data

ZERO = Decimal("0")
CENT = Decimal("0.01")


def _q(v: Decimal) -> Decimal:
    return v.quantize(CENT)


def assert_convertible(source: Account, dest: Account) -> None:
    """Reject the pairs that are not conversions, saying which rule applies.

    Refusing with the reason is the point: someone reaching for "Roth →
    Traditional" has a real misconception, and a disabled dropdown entry
    teaches them nothing.
    """
    if source.id == dest.id:
        raise HTTPException(status_code=422, detail="Pick two different accounts")
    if source.scenario_id != dest.scenario_id:
        raise HTTPException(status_code=422, detail="Both accounts must be in the same scenario")

    # Order matters: the specific misconceptions are answered before the generic
    # "that is not a conversion", because the reason is the useful part. Someone
    # reaching for Roth -> Traditional needs to hear about the TCJA, not that
    # the destination is wrong.
    if source.account_type == AccountType.ROTH_IRA:
        raise HTTPException(
            status_code=422,
            detail=("Roth money cannot be converted back to a Traditional IRA. "
                    "Recharacterising a conversion was eliminated for 2018 and later by "
                    "the Tax Cuts and Jobs Act."),
        )
    if source.account_type == AccountType.TAXABLE:
        raise HTTPException(
            status_code=422,
            detail=("Money in a taxable account is not converted — it is contributed. "
                    "Deposit it into the Roth instead, where it counts against the annual "
                    "contribution limit."),
        )
    if dest.account_type == AccountType.TAXABLE:
        raise HTTPException(
            status_code=422,
            detail=("Moving money from an IRA to a taxable account is a distribution, "
                    "not a conversion. Withdraw from the IRA instead — it is ordinary "
                    "income, plus a 10% penalty before 59½."),
        )
    if source.account_type in ira.PRE_TAX_TYPES and dest.account_type in ira.PRE_TAX_TYPES:
        raise HTTPException(
            status_code=422,
            detail=("Moving money between Traditional and Rollover IRAs is a transfer, "
                    "not a conversion — both hold pre-tax money, so there is nothing to "
                    "convert and no tax to pay."),
        )
    if dest.account_type != AccountType.ROTH_IRA:
        raise HTTPException(status_code=422, detail="A conversion can only go into a Roth IRA")
    if source.account_type not in ira.PRE_TAX_TYPES:
        raise HTTPException(status_code=422,
                            detail="Only a Traditional or Rollover IRA can be converted")


def _price_on(ticker: str, on: date, today: date) -> Decimal:
    """The price a conversion is valued at. A past-dated conversion uses that
    day's close; today's uses the live quote."""
    if on < today:
        # a week back, so a conversion dated on a weekend or holiday still finds
        # the most recent close before it
        candles, _ = market_data.history(ticker, on - timedelta(days=7), on)
        for d, px in reversed(candles):
            if d <= on:
                return px
        raise HTTPException(status_code=422,
                            detail=f"No price for {ticker} on {on.isoformat()}")
    try:
        return market_data.quote(ticker).price
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


def resolve_amount(db: Session, source: Account, amount: Decimal | None,
                   ticker: str | None, shares: Decimal | None,
                   on: date, today: date) -> tuple[Decimal, str | None, Decimal, Decimal]:
    """Work out what is actually being converted.

    Returns (value, ticker, shares, price). `ticker` is None for a cash
    conversion. In kind, the value is the market value of the shares on the
    conversion date — a conversion is a distribution at fair market value.
    """
    if ticker:
        if shares is None or shares <= ZERO:
            raise HTTPException(status_code=422, detail="shares is required for an in-kind conversion")
        position = db.execute(
            select(Position).where(Position.account_id == source.id,
                                   Position.ticker == ticker)
        ).scalar_one_or_none()
        held = Decimal(position.shares) if position else ZERO
        if shares > held:
            raise HTTPException(
                status_code=422,
                detail=f"{source.name} holds {held} shares of {ticker}, not {shares}",
            )
        price = _price_on(ticker, on, today)
        return _q(shares * price), ticker, shares, price

    if amount is None or amount <= ZERO:
        raise HTTPException(status_code=422, detail="amount is required for a cash conversion")
    from app.services.trading import buying_power

    available = buying_power(db, source.id)
    if amount > available:
        raise HTTPException(
            status_code=422,
            detail=(f"{source.name} has ${available} available to convert "
                    f"(cash committed to open orders cannot be moved)"),
        )
    return _q(amount), None, ZERO, ZERO


def preview(db: Session, user: User, scenario_id: str | None, source: Account,
            dest: Account, *, amount: Decimal | None = None, ticker: str | None = None,
            shares: Decimal | None = None, on: date | None = None) -> dict:
    """What a conversion would cost, without doing it.

    The taxable split has to be shown before the button is pressed: a conversion
    is irreversible, and its whole cost is a tax bill that arrives months later.
    """
    today = utcnow().date()
    on = on or today
    assert_convertible(source, dest)
    value, tk, sh, price = resolve_amount(db, source, amount, ticker, shares, on, today)
    split = ira.pro_rata(db, user, scenario_id, value)

    notes = [
        "A conversion has no annual limit and no income cap — it does not use any of "
        "your IRA contribution room.",
        f"The taxable portion is ordinary income for {on.year}; nothing is withheld here, "
        "so in reality you would pay it from outside the IRA.",
        f"This conversion starts its own five-year clock on January 1, {on.year}. Drawing "
        "the converted amount back out before then and before 59½ costs a 10% penalty, "
        "even though it has already been taxed.",
    ]
    if split.total_basis > ZERO:
        notes.append(
            f"{split.basis_fraction * 100:.2f}% of it is after-tax basis, prorated across "
            f"every Traditional and Rollover IRA you hold (${_q(split.total_basis)} of basis "
            f"against ${_q(split.total_value)} of value) — Form 8606's pro-rata rule. Basis "
            "cannot be isolated by keeping it in its own account."
        )
    else:
        notes.append(
            "You have no after-tax basis on the pre-tax side, so the whole conversion is "
            "taxable. Mark a Traditional IRA contribution as nondeductible to build basis."
        )
    if not ira.is_penalty_free_age(user, on):
        notes.append(
            "There is no 10% early-distribution penalty on a conversion itself, whatever "
            "your age — only on drawing the converted money back out too soon."
        )

    return {
        "from_account_id": source.id,
        "to_account_id": dest.id,
        "conversion_date": on,
        "gross_amount": value,
        "taxable_amount": split.taxable,
        "nontaxable_amount": split.nontaxable,
        "basis_fraction_pct": float(split.basis_fraction * 100),
        "total_pre_tax_value": _q(split.total_value),
        "total_after_tax_basis": _q(split.total_basis),
        "ticker": tk,
        "shares": sh,
        "price": price,
        "in_kind": tk is not None,
        "five_year_clock_year": on.year,
        "notes": notes,
    }


def execute(db: Session, user: User, scenario_id: str | None, source: Account,
            dest: Account, *, amount: Decimal | None = None, ticker: str | None = None,
            shares: Decimal | None = None, on: date | None = None) -> Conversion:
    """Perform the conversion. Caller commits."""
    today = utcnow().date()
    on = on or today
    plan = preview(db, user, scenario_id, source, dest,
                   amount=amount, ticker=ticker, shares=shares, on=on)
    value: Decimal = plan["gross_amount"]
    taxable: Decimal = plan["taxable_amount"]
    nontaxable: Decimal = plan["nontaxable_amount"]

    if plan["in_kind"]:
        _move_shares(db, source, dest, plan["ticker"], plan["shares"], plan["price"], on)
    else:
        source.settlement_balance = _q(Decimal(source.settlement_balance) - value)
        dest.settlement_balance = _q(Decimal(dest.settlement_balance) + value)

    # after-tax dollars that left the pre-tax side stop being basis there
    ira.consume_basis(db, user, scenario_id, nontaxable)

    stamp = utcnow()
    memo = (f"Roth conversion — {plan['shares']} {plan['ticker']}"
            if plan["in_kind"] else "Roth conversion")
    # A signed pair, so a plain SUM over an account still gives net external
    # flow. Kind CONVERSION keeps it out of contribution-limit arithmetic.
    db.add(Contribution(account_id=source.id, tax_year=None, amount=-value,
                        kind=CashFlowKind.CONVERSION, memo=memo, timestamp=stamp))
    db.add(Contribution(account_id=dest.id, tax_year=None, amount=value,
                        kind=CashFlowKind.CONVERSION, memo=memo, timestamp=stamp))

    conversion = Conversion(
        from_account_id=source.id,
        to_account_id=dest.id,
        conversion_date=on,
        gross_amount=value,
        taxable_amount=taxable,
        nontaxable_amount=nontaxable,
        taxable_remaining=taxable,
        nontaxable_remaining=nontaxable,
        in_kind=plan["in_kind"],
    )
    db.add(conversion)
    return conversion


def _move_shares(db: Session, source: Account, dest: Account, ticker: str,
                 shares: Decimal, price: Decimal, on: date) -> None:
    """Move shares between accounts, consuming source lots oldest-first.

    The lots do not travel: a conversion is a distribution at fair market value,
    so the shares arrive in the Roth with a fresh lot at the conversion price.
    Carrying the old cost basis over would understate the Roth's value as a
    tax-free wrapper and misstate every unrealized-gain figure after it.
    """
    from app.services.trading import _sync_position

    lots = list(db.execute(
        select(TaxLot).where(TaxLot.account_id == source.id, TaxLot.ticker == ticker,
                             TaxLot.shares_open > 0)
        .order_by(TaxLot.acquired_on, TaxLot.created_at)
    ).scalars())
    remaining = shares
    for lot in lots:
        if remaining <= ZERO:
            break
        take = min(Decimal(lot.shares_open), remaining)
        lot.shares_open = Decimal(lot.shares_open) - take
        remaining -= take
    if remaining > ZERO:
        raise HTTPException(status_code=422,
                            detail=f"{source.name} does not hold {shares} shares of {ticker}")

    src_position = db.execute(
        select(Position).where(Position.account_id == source.id, Position.ticker == ticker)
    ).scalar_one_or_none()
    _sync_position(db, source.id, ticker, src_position, lots)

    arriving = TaxLot(account_id=dest.id, ticker=ticker, shares_open=shares,
                      cost_per_share=price, acquired_on=on)
    db.add(arriving)
    dest_lots = list(db.execute(
        select(TaxLot).where(TaxLot.account_id == dest.id, TaxLot.ticker == ticker,
                             TaxLot.shares_open > 0)
    ).scalars())
    # the row just added is not in that result until it is flushed, and the
    # position it feeds is an aggregate over exactly these lots
    if arriving not in dest_lots:
        dest_lots.append(arriving)
    dest_position = db.execute(
        select(Position).where(Position.account_id == dest.id, Position.ticker == ticker)
    ).scalar_one_or_none()
    _sync_position(db, dest.id, ticker, dest_position, dest_lots)
