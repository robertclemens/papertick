"""Dividend accrual by ledger reconciliation.

For every (account, ticker) with transaction history, the expected dividend for
each ex-date is `shares held as of that date × per-share amount`. Rows in the
`dividends` table are reconciled against that expectation and the account's
cash credited/debited by the difference — which makes the process idempotent
and automatically backfills dividends for backdated (backtest) positions and
corrects them if a backdated sell later changes history. Ex-date is used as
the credit date (payment-date lag is not modeled).
"""

import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, Dividend, OrderSide, Transaction
from app.services.market_data import MarketDataError, market_data

log = logging.getLogger("papertick.dividends")

CENT = Decimal("0.01")
ZERO = Decimal("0")


def reconcile_account_ticker(db: Session, account_id: str, ticker: str) -> Decimal:
    """Returns the net cash credited (negative = clawed back). Caller commits."""
    txns = list(db.execute(
        select(Transaction)
        .where(Transaction.account_id == account_id, Transaction.ticker == ticker)
        .order_by(Transaction.as_of, Transaction.executed_at)
    ).scalars())
    if not txns:
        return ZERO
    first = min(t.as_of for t in txns)
    try:
        events = market_data.dividends(ticker, first, date.today())
    except MarketDataError:
        return ZERO
    if not events:
        return ZERO

    existing = {
        d.event_date: d
        for d in db.execute(
            select(Dividend).where(Dividend.account_id == account_id, Dividend.ticker == ticker)
        ).scalars()
    }
    # A holding whose payments came from a brokerage export is already the
    # record of what was actually paid. Approximating it from ex-date data
    # would both restate those rows and invent payments for dates the export
    # deliberately does not contain, so the whole holding is left alone.
    if any(row.imported for row in existing.values()):
        return ZERO

    net = ZERO
    for ev_date, per_share in events:
        held = ZERO
        for t in txns:
            if t.as_of > ev_date:
                break
            delta = Decimal(t.shares_filled)
            held += delta if t.side == OrderSide.BUY else -delta
        expected = (held * per_share).quantize(CENT, ROUND_HALF_UP) if held > 0 else ZERO
        row = existing.get(ev_date)
        current = Decimal(row.amount) if row is not None else ZERO
        diff = expected - current
        if diff == 0:
            continue
        if row is None:
            db.add(Dividend(
                account_id=account_id, ticker=ticker, event_date=ev_date,
                per_share=per_share, shares=held, amount=expected,
            ))
        elif expected <= 0:
            db.delete(row)
        else:
            row.per_share = per_share
            row.shares = held
            row.amount = expected
        net += diff

    if net != 0:
        account = db.execute(
            select(Account).where(Account.id == account_id).with_for_update()
        ).scalar_one()
        account.settlement_balance = Decimal(account.settlement_balance) + net
        log.info("dividends reconciled %s/%s: %+.2f", account_id, ticker, net)
    return net


def reconcile_all(db: Session) -> Decimal:
    """Reconcile every account/ticker pair that has ever traded, skipping
    accounts in deleted scenarios. Caller commits."""
    from app.services.scenarios import frozen_accounts

    pairs = db.execute(
        select(Transaction.account_id, Transaction.ticker)
        .where(Transaction.account_id.notin_(frozen_accounts(db)))
        .distinct()
    ).all()
    total = ZERO
    for account_id, ticker in pairs:
        total += reconcile_account_ticker(db, account_id, ticker)
    return total
